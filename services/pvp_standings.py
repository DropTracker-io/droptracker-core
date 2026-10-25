"""PvP standings in Redis: who has taken the most loot, kills and deaths.

The PvP counterpart of the drop leaderboards in ``services/redis_updates.py``,
kept in its own ``pvp:`` namespace so nothing PvP can leak onto a loot board.
The database (``pvp_kills`` / ``pvp_kill_groups``, ``player_deaths``) is the
record; everything here is a cache that ``rebuild()`` regenerates from it.

Keys, for every period token (``utils.partitions``: ``YYYYMM``, ``YYYYWww``,
``YYYYMMDD``, ``all``):

``pvp:loot:{token}[:group:{gid}]``    zset player_id -> GP taken from kills
``pvp:kills:{token}[:group:{gid}]``   zset player_id -> kills
``pvp:deaths:{token}[:group:{gid}]``  zset player_id -> deaths to a player
``pvp:gloot:{token}``                 zset group_id  -> the group's GP total
``pvp:recent[:group:{gid}]``          list of recent kills (JSON), newest first

The unscoped (global) keys follow the global group's verdict: a kill counts
globally exactly when its ``pvp_kill_groups`` row for group 2 is counted.

Every writer here is best-effort. A Redis fault must never cost a kill its
database row; the rebuild puts the cache right afterwards.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from utils.partitions import ALL, all_tokens, month_token

GLOBAL_GROUP_ID = 2

NS = "pvp"
METRICS = ("loot", "kills", "deaths")

# Same retention as the drop boards: daily sets 90 days, weekly and monthly
# about 13 months, all-time forever.
_DAILY_TTL = 90 * 24 * 3600
_LONG_TTL = 400 * 24 * 3600
RECENT_LIMIT = 50


def counts_as_kill(kill) -> bool:
    """A ``loot_key`` row (a loot key matched to no kill) adds loot, never a
    kill: the kill it came from is already on the boards."""
    return (getattr(kill, "kind", None) or "kill") != "loot_key"


def _client():
    from utils.redis import redis_client

    return redis_client.client


def _ttl_for(token: str) -> Optional[int]:
    if token == ALL:
        return None
    if len(token) == 8 and token.isdigit():
        return _DAILY_TTL
    return _LONG_TTL


def board_key(metric: str, token: str, group_id: Optional[int] = None) -> str:
    """The sorted set for one metric/period, global or scoped to a group."""
    if metric not in METRICS:
        raise ValueError(f"unknown PvP metric {metric!r}")
    key = f"{NS}:{metric}:{token}"
    if group_id is not None and int(group_id) != GLOBAL_GROUP_ID:
        key += f":group:{int(group_id)}"
    return key


def group_total_key(token: str) -> str:
    return f"{NS}:gloot:{token}"


def recent_key(group_id: Optional[int] = None) -> str:
    if group_id is None or int(group_id) == GLOBAL_GROUP_ID:
        return f"{NS}:recent"
    return f"{NS}:recent:group:{int(group_id)}"


def _scopes(group_ids: Iterable[int]) -> List[Optional[int]]:
    """Group ids -> the key scopes they write: None (global) for group 2."""
    scopes: List[Optional[int]] = []
    for gid in group_ids:
        scope = None if int(gid) == GLOBAL_GROUP_ID else int(gid)
        if scope not in scopes:
            scopes.append(scope)
    return scopes


def recent_entry(kill, player_name: str = "") -> dict:
    """The JSON a recent-kills list holds for one kill."""
    items = list(getattr(kill, "items", None) or [])
    top = max(
        items, key=lambda i: (getattr(i, "value", 0) or 0) * (getattr(i, "quantity", 1) or 1)
    ) if items else None
    return {
        "kill_id": int(kill.id) if getattr(kill, "id", None) is not None else None,
        "player_id": int(kill.player_id),
        "player_name": player_name,
        "victim_name": kill.victim_name,
        "victim_player_id": kill.victim_player_id,
        "total_value": int(kill.total_value or 0),
        "item_count": int(kill.item_count or 0),
        "top_item_id": int(top.item_id) if top is not None else None,
        "top_item_name": getattr(top, "item_name", None) if top is not None else None,
        "wilderness_level": kill.wilderness_level,
        "world": kill.world,
        "date": int(kill.date_added.timestamp()) if getattr(kill, "date_added", None) else None,
        "kind": getattr(kill, "kind", None) or "kill",
    }


def _increment(pipe, metric: str, amount: float, player_id: int,
               scopes: Sequence[Optional[int]], when: datetime) -> None:
    for token in all_tokens(when):
        ttl = _ttl_for(token)
        for scope in scopes:
            key = board_key(metric, token, scope)
            pipe.zincrby(key, amount, int(player_id))
            if ttl:
                pipe.expire(key, ttl)


def record_kill(kill, counted_group_ids: Iterable[int], *, player_name: str = "",
                client=None) -> bool:
    """Add one kill to every board it counts toward.

    ``counted_group_ids`` is the set of groups whose ``pvp_kill_groups`` row is
    counted (group 2 meaning the global boards). Returns False when Redis
    could not be written; the caller carries on regardless.
    """
    scopes = _scopes(counted_group_ids)
    if not scopes:
        return True
    try:
        r = client or _client()
        when = kill.date_added or datetime.now()
        value = int(kill.total_value or 0)
        pipe = r.pipeline(transaction=False)
        _increment(pipe, "loot", value, kill.player_id, scopes, when)
        if counts_as_kill(kill):
            _increment(pipe, "kills", 1, kill.player_id, scopes, when)
        for token in all_tokens(when):
            ttl = _ttl_for(token)
            for scope in scopes:
                if scope is None:
                    continue
                pipe.zincrby(group_total_key(token), value, scope)
                if ttl:
                    pipe.expire(group_total_key(token), ttl)
        entry = json.dumps(recent_entry(kill, player_name))
        for scope in scopes:
            pipe.lpush(recent_key(scope), entry)
            pipe.ltrim(recent_key(scope), 0, RECENT_LIMIT - 1)
        pipe.execute()
        return True
    except Exception as e:
        print(f"[PvpStandings] Couldn't record kill {getattr(kill, 'id', '?')}: {e}")
        return False


def add_loot(kill, amount: int, counted_group_ids: Iterable[int], *, client=None) -> bool:
    """Add loot to a kill already on the boards (a loot key matched to it),
    under the kill's own date. The kill count and recent lists are untouched;
    ``rebuild()`` reads the kill's new total either way."""
    scopes = _scopes(counted_group_ids)
    if not scopes or not amount:
        return True
    try:
        r = client or _client()
        when = kill.date_added or datetime.now()
        pipe = r.pipeline(transaction=False)
        _increment(pipe, "loot", int(amount), kill.player_id, scopes, when)
        for token in all_tokens(when):
            ttl = _ttl_for(token)
            for scope in scopes:
                if scope is None:
                    continue
                pipe.zincrby(group_total_key(token), int(amount), scope)
                if ttl:
                    pipe.expire(group_total_key(token), ttl)
        pipe.execute()
        return True
    except Exception as e:
        print(f"[PvpStandings] Couldn't add loot to kill {getattr(kill, 'id', '?')}: {e}")
        return False


def remove_kill(kill, counted_group_ids: Iterable[int], *, client=None) -> bool:
    """Take a kill back off its boards (hidden by staff, or deleted).

    Recent-kill lists are left alone: they age out within a few kills and are
    rebuilt wholesale by ``rebuild()``.
    """
    scopes = _scopes(counted_group_ids)
    if not scopes:
        return True
    try:
        r = client or _client()
        when = kill.date_added or datetime.now()
        value = int(kill.total_value or 0)
        pipe = r.pipeline(transaction=False)
        _increment(pipe, "loot", -value, kill.player_id, scopes, when)
        if counts_as_kill(kill):
            _increment(pipe, "kills", -1, kill.player_id, scopes, when)
        for token in all_tokens(when):
            for scope in scopes:
                if scope is not None:
                    pipe.zincrby(group_total_key(token), -value, scope)
        pipe.execute()
        return True
    except Exception as e:
        print(f"[PvpStandings] Couldn't remove kill {getattr(kill, 'id', '?')}: {e}")
        return False


def record_death(player_id: int, group_ids: Iterable[int], when: Optional[datetime] = None,
                 *, client=None) -> bool:
    """Count one death to another player for the player and their groups."""
    scopes = _scopes(list(group_ids) + [GLOBAL_GROUP_ID])
    try:
        r = client or _client()
        pipe = r.pipeline(transaction=False)
        _increment(pipe, "deaths", 1, player_id, scopes, when or datetime.now())
        pipe.execute()
        return True
    except Exception as e:
        print(f"[PvpStandings] Couldn't record a death for player {player_id}: {e}")
        return False


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def resolve_token(period: Optional[str]) -> str:
    """``day|week|month|all`` or a raw token -> a partition token (the same
    resolver the drop leaderboards use, so the two never disagree)."""
    from utils.partitions import resolve_period

    return resolve_period(period)


def _int(raw) -> int:
    if raw is None:
        return 0
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def top(metric: str, token: str, group_id: Optional[int] = None, *, limit: int = 25,
        offset: int = 0, client=None) -> Tuple[List[Tuple[int, int]], int]:
    """``([(player_id, score), ...], total_entries)``, highest first.

    Zero and negative scores (a kill removed again) are left out.
    """
    r = client or _client()
    key = board_key(metric, token, group_id)
    rows = r.zrevrangebyscore(key, "+inf", "(0", start=offset, num=limit, withscores=True)
    total = r.zcount(key, "(0", "+inf")
    return [(_int(member), _int(score)) for member, score in rows], int(total or 0)


def player_line(player_id: int, token: str, group_id: Optional[int] = None,
                *, client=None) -> Dict[str, Optional[int]]:
    """One player's loot, kills, deaths and loot rank for a period/scope."""
    r = client or _client()
    out: Dict[str, Optional[int]] = {}
    pipe = r.pipeline(transaction=False)
    for metric in METRICS:
        pipe.zscore(board_key(metric, token, group_id), int(player_id))
    pipe.zrevrank(board_key("loot", token, group_id), int(player_id))
    loot, kills, deaths, rank = pipe.execute()
    out["loot"] = _int(loot)
    out["kills"] = _int(kills)
    out["deaths"] = _int(deaths)
    out["rank"] = (int(rank) + 1) if rank is not None and _int(loot) > 0 else None
    return out


def scores_for(metric: str, token: str, player_ids: Sequence[int],
               group_id: Optional[int] = None, *, client=None) -> Dict[int, int]:
    """``{player_id: score}`` for a page of players (missing -> 0)."""
    if not player_ids:
        return {}
    r = client or _client()
    raw = r.zmscore(board_key(metric, token, group_id), [int(p) for p in player_ids])
    return {int(pid): _int(score) for pid, score in zip(player_ids, raw)}


def board_sum(metric: str, token: str, group_id: Optional[int] = None, *, client=None) -> int:
    """Every player's score on one board added up (a group's kill count)."""
    r = client or _client()
    total = 0
    for _member, score in r.zscan_iter(board_key(metric, token, group_id), count=1000):
        total += max(_int(score), 0)
    return total


def group_total(group_id: int, token: str, *, client=None) -> int:
    """GP a group's members took in PvP this period."""
    r = client or _client()
    if int(group_id) == GLOBAL_GROUP_ID:
        # The global group's total is every counted kill's value.
        return board_sum("loot", token, None, client=r)
    return _int(r.zscore(group_total_key(token), int(group_id)))


def recent(group_id: Optional[int] = None, limit: int = 20, *, client=None) -> List[dict]:
    r = client or _client()
    out = []
    for raw in r.lrange(recent_key(group_id), 0, max(limit, 1) - 1):
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# Rebuild
# --------------------------------------------------------------------------- #
def _wipe(r) -> int:
    removed = 0
    batch = []
    for key in r.scan_iter(match=f"{NS}:*", count=1000):
        batch.append(key)
        if len(batch) >= 500:
            removed += r.delete(*batch)
            batch = []
    if batch:
        removed += r.delete(*batch)
    return removed


def rebuild(session, *, client=None, chunk: int = 5000, log=print) -> Dict[str, int]:
    """Regenerate every ``pvp:`` key from the database.

    Reads counted, unhidden kills in id order (one indexed range scan per
    chunk) plus PvP deaths from ``player_deaths`` and works out every board in
    memory first, so a failed read leaves the live keys untouched. Only then
    are the old keys dropped and the new ones written (a few seconds in which
    the boards read low).
    """
    from sqlalchemy import text

    from db.models import PvpKill, PvpKillGroup

    r = client or _client()
    stats = {"kills": 0, "deaths": 0, "keys_removed": 0}

    # Build in memory first (PvP volume is small), then swap in one pipeline.
    boards: Dict[str, Dict[int, float]] = {}
    totals: Dict[str, Dict[int, float]] = {}
    recents: Dict[str, List[str]] = {}

    def add(key, member, amount):
        boards.setdefault(key, {})
        boards[key][member] = boards[key].get(member, 0) + amount

    last_id = 0
    while True:
        kills = (
            session.query(PvpKill)
            .filter(PvpKill.id > last_id, PvpKill.hidden.is_(False),
                    PvpKill.excluded_reason.is_(None))
            .order_by(PvpKill.id)
            .limit(chunk)
            .all()
        )
        if not kills:
            break
        ids = [k.id for k in kills]
        counted: Dict[int, List[int]] = {}
        for kill_id, group_id in (
            session.query(PvpKillGroup.kill_id, PvpKillGroup.group_id)
            .filter(PvpKillGroup.kill_id.in_(ids), PvpKillGroup.counted.is_(True))
            .all()
        ):
            counted.setdefault(int(kill_id), []).append(int(group_id))
        for kill in kills:
            scopes = _scopes(counted.get(int(kill.id), []))
            if not scopes:
                continue
            is_kill = counts_as_kill(kill)
            stats["kills"] += 1 if is_kill else 0
            value = int(kill.total_value or 0)
            for token in all_tokens(kill.date_added):
                for scope in scopes:
                    add(board_key("loot", token, scope), int(kill.player_id), value)
                    if is_kill:
                        add(board_key("kills", token, scope), int(kill.player_id), 1)
                    if scope is not None:
                        totals.setdefault(group_total_key(token), {})
                        bucket = totals[group_total_key(token)]
                        bucket[scope] = bucket.get(scope, 0) + value
            entry = json.dumps(recent_entry(kill))
            for scope in scopes:
                recents.setdefault(recent_key(scope), []).append(entry)
        last_id = ids[-1]

    # Deaths to another player, by the dead player's current groups.
    death_rows = session.execute(text(
        "SELECT d.player_id, d.date_added, d.region_id FROM player_deaths d "
        "WHERE d.killer_type = 'player' AND d.world_type = 'main'"
    )).all()
    memberships: Dict[int, List[int]] = {}
    if death_rows:
        for pid, gid in session.execute(text(
            "SELECT a.player_id, a.group_id FROM user_group_association a "
            "WHERE a.player_id IN (SELECT DISTINCT player_id FROM player_deaths "
            "WHERE killer_type = 'player')"
        )).all():
            memberships.setdefault(int(pid), []).append(int(gid))
    from utils.death_regions import pvp_minigame_for_region

    for pid, when, region_id in death_rows:
        if pvp_minigame_for_region(region_id):
            continue
        stats["deaths"] += 1
        scopes = _scopes(memberships.get(int(pid), []) + [GLOBAL_GROUP_ID])
        for token in all_tokens(when):
            for scope in scopes:
                add(board_key("deaths", token, scope), int(pid), 1)

    stats["keys_removed"] = _wipe(r)
    pipe = r.pipeline(transaction=False)
    written = 0
    for key, members in list(boards.items()) + list(totals.items()):
        token = key.split(":")[2]
        pipe.zadd(key, {m: s for m, s in members.items()})
        ttl = _ttl_for(token)
        if ttl:
            pipe.expire(key, ttl)
        written += 1
        if written % 500 == 0:
            pipe.execute()
    for key, entries in recents.items():
        # Kills were read oldest first; the list is newest first.
        newest = list(reversed(entries))[:RECENT_LIMIT]
        if newest:
            pipe.rpush(key, *newest)
    pipe.execute()
    stats["keys_written"] = written + len(recents)
    log(f"[PvpStandings] Rebuilt: {stats}")
    return stats
