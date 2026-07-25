"""Badge award engine (see db/models/badge.py for the schema/dedupe design).

Sync, session-based functions (same style as services/points.py) — safe to run
from the bot via ``asyncio.to_thread`` or from CLI scripts.

Automatic badge behavior is code-owned: a badge row's ``criteria`` JSON has a
``type`` field that maps to an evaluator here (``daily_champion``,
``loot_streak``, ``boss_record``, ``global_champion``). Definitions
(name/tone/icon/active) stay admin-editable in the DB.

All evaluators are idempotent — re-running any day converges to the same
awards — so the hourly ``run_badge_cycle`` can safely catch up after downtime.
Data sources are Redis leaderboards (daily boards, plus the persistent
monthly/all-time boards) and the small ``personal_best`` table only; the
160M-row ``drops`` table is never touched.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from db import Badge, NpcList, PlayerBadge, Session
from utils.partitions import ALL, day_token, is_valid_token, month_token
from utils.redis import redis_client

# Redis marker: the most recent day token fully processed by run_badge_cycle.
LAST_COMPLETED_DAY_KEY = "badges:last_completed_day"

# Don't catch up further back than the daily boards' 90-day TTL can support.
MAX_CATCHUP_DAYS = 85

# Daily boards are only trustworthy from the day the incremental daily/weekly
# leaderboards shipped (see scripts/reconcile_period_leaderboards.py). Older
# boards exist only for players who ran force_update, so their "champions"
# would be winners of nearly-empty boards. Never award daily champions for
# days before this.
DAILY_BOARDS_EPOCH = "20260701"

# Clean team_size values ("Solo", "5", "11-15", "6+", "24+"). The column is
# free text and also contains junk like "(2", "5 s", "0" — those slots are
# skipped, not errors.
_TEAM_SIZE_RE = re.compile(r"^(Solo|[1-9]\d?|[1-9]\d?-\d{1,2}|[1-9]\d?\+)$")

# Cap SQL IN() clauses / Redis pipelines when filtering streak candidates.
_CHUNK = 5000

# How deep to scan a loot board for an eligible leader. The public leaderboard
# omits hidden players and ids whose ``players`` row is gone, so the leader
# badges follow the highest *visible* entry rather than naming someone the site
# refuses to show. 25 is far more headroom than the handful of hidden accounts
# near the top of a board could ever consume.
_LEADER_SCAN = 25


def _log(msg: str) -> None:
    print(f"[badges] {msg}")


# ----------------------------
# Award primitives
# ----------------------------

def award_badge(
    session,
    badge: Badge,
    player_id: int,
    slot_key: str,
    context: Optional[dict] = None,
    group_id: Optional[int] = None,
    awarded_by: Optional[int] = None,
) -> Optional[PlayerBadge]:
    """Idempotently award ``badge`` to ``player_id`` for ``slot_key``.

    Returns the new PlayerBadge, or None if the slot is already taken. For
    automatic (``awarded_by is None``) awards, *any* existing row for the slot
    blocks re-award — so an admin revocation of an automatic award is sticky.
    Manual awards only collide with an *active* row (revoke frees the slot).
    """
    group_key = group_id or 0
    q = session.query(PlayerBadge).filter(
        PlayerBadge.badge_id == badge.badge_id,
        PlayerBadge.group_key == group_key,
        PlayerBadge.slot_key == slot_key,
    )
    if awarded_by is not None:
        q = q.filter(PlayerBadge.status == "active")
    if q.first() is not None:
        return None

    award = PlayerBadge(
        badge_id=badge.badge_id,
        player_id=player_id,
        group_id=group_id,
        group_key=group_key,
        status="active",
        slot_key=slot_key,
        active_key=slot_key,
        awarded_by=awarded_by,
        context=json.dumps(context) if context else None,
    )
    session.add(award)
    try:
        session.flush()
    except IntegrityError:
        # Lost a race on the unique index — someone else awarded the slot.
        session.rollback()
        return None
    return award


def transfer_held_badge(
    session,
    badge: Badge,
    slot_key: str,
    new_player_id: int,
    context: Optional[dict] = None,
    group_id: Optional[int] = None,
) -> str:
    """Ensure ``new_player_id`` is the active holder of a held badge slot.

    Returns 'retained' (same holder; context refreshed), 'transferred'
    (previous holder marked lost, new active row inserted) or 'awarded'
    (slot had no holder).
    """
    group_key = group_id or 0
    current = (
        session.query(PlayerBadge)
        .filter(
            PlayerBadge.badge_id == badge.badge_id,
            PlayerBadge.group_key == group_key,
            PlayerBadge.active_key == slot_key,
        )
        .first()
    )
    ctx_json = json.dumps(context) if context else None
    if current is not None and current.player_id == new_player_id:
        if ctx_json != current.context:
            current.context = ctx_json
            session.flush()
        return "retained"

    outcome = "awarded"
    if current is not None:
        current.status = "lost"
        current.active_key = None
        current.lost_at = datetime.now()
        session.flush()
        outcome = "transferred"

    award = PlayerBadge(
        badge_id=badge.badge_id,
        player_id=new_player_id,
        group_id=group_id,
        group_key=group_key,
        status="active",
        slot_key=slot_key,
        active_key=slot_key,
        context=ctx_json,
    )
    session.add(award)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return "retained"
    return outcome


def revoke_badge(session, award: PlayerBadge) -> None:
    """Revoke an award (kept as history; frees the active slot)."""
    award.status = "revoked"
    award.active_key = None
    award.lost_at = datetime.now()
    session.flush()


# ----------------------------
# Automatic evaluators
# ----------------------------

def evaluate_daily_champion(session, badge: Badge, day: str, dry_run: bool = False) -> int:
    """Award the top scorer of ``leaderboard:{day}`` (global). Returns 1/0."""
    if day < DAILY_BOARDS_EPOCH:
        return 0
    top = redis_client.client.zrevrange(f"leaderboard:{day}", 0, 0, withscores=True)
    if not top:
        return 0
    member, score = top[0]
    loot = int(float(score))
    if loot <= 0:
        return 0
    try:
        player_id = int(member.decode() if isinstance(member, bytes) else member)
    except (ValueError, AttributeError):
        return 0

    if dry_run:
        _log(f"DRY-RUN daily_champion {day}: player {player_id} with {loot:,} gp")
        return 1
    award = award_badge(
        session, badge, player_id, slot_key=day, context={"day": day, "loot": loot}
    )
    if award is not None:
        session.commit()
        _log(f"daily_champion {day}: awarded to player {player_id} ({loot:,} gp)")
        return 1
    return 0

def _visible_player_ids(session, player_ids: List[int]) -> set:
    """Subset of ``player_ids`` a public board would actually show.

    Mirrors ``web_api.common.hidden_player_ids`` (``Player.hidden`` or the
    owning ``User.hidden``) so a leader badge never names an account the site
    hides, and drops ids with no ``players`` row at all — merged or deleted
    players can linger in a sorted set. Both flags are nullable; NULL means
    "not hidden", same as the web filter.
    """
    if not player_ids:
        return set()
    from db import Player, User

    rows = (
        session.query(Player.player_id, Player.hidden, User.hidden)
        .outerjoin(User, User.user_id == Player.user_id)
        .filter(Player.player_id.in_(player_ids))
        .all()
    )
    return {int(pid) for pid, p_hidden, u_hidden in rows if not p_hidden and not u_hidden}


def _held_by(session, badge: Badge, slot_key: str) -> Optional[int]:
    """Player id currently holding ``slot_key`` for ``badge``, if any."""
    row = (
        session.query(PlayerBadge.player_id)
        .filter(
            PlayerBadge.badge_id == badge.badge_id,
            PlayerBadge.group_key == 0,
            PlayerBadge.active_key == slot_key,
        )
        .first()
    )
    return int(row[0]) if row else None


def _period_closed(period: str, now: Optional[datetime] = None) -> bool:
    """True when ``period``'s board can no longer change (a past month).

    The all-time board never closes, so an all-time leader badge only makes
    sense with the ``held`` semantic.
    """
    if period == ALL:
        return False
    return period < month_token(now or datetime.now())


def evaluate_global_champion(session, badge: Badge, dry_run: bool = False,
                             period: str = ALL) -> int:
    """Award/converge the "top of the loot board" badge for one ``period``.

    ``period`` is a partition token — ``"all"`` for the all-time board or a
    ``YYYYMM`` month token — so the badge reads exactly the key the site's
    leaderboard does (``leaderboard:all`` / ``leaderboard:202607``; both are
    persistent, unlike the 90-day daily boards). One slot per period, so each
    month gets its own award.

    ``badge.semantic`` (admin-editable) picks the behavior:

    ``held``      the current #1 holds it live. Losing the lead marks the old
                  award ``lost`` — kept as history ("Held until ...") — and
                  the new leader gets an active row. A month that has ended
                  keeps its winner forever: nothing writes to that board again.
    ``permanent`` a trophy for a *finished* period, like the daily champion:
                  the in-progress month is skipped entirely (its winner isn't
                  decided yet) and the award, once made, is never taken back.

    Returns 1 when an award was made or moved, else 0.
    """
    held = badge.semantic == "held"
    if not held and not _period_closed(period):
        # 'permanent' + a period still being written to would freeze whoever
        # happens to lead right now. Wait for the month to close.
        return 0
    top = redis_client.client.zrevrange(f"leaderboard:{period}", 0, _LEADER_SCAN - 1,
                                        withscores=True)
    if not top:
        return 0

    ranked: List[Tuple[int, int]] = []
    for member, score in top:
        try:
            player_id = int(member.decode() if isinstance(member, bytes) else member)
        except (ValueError, AttributeError):
            continue
        loot = int(float(score))
        if loot <= 0:
            break  # scores descend: nothing below this one is positive either
        ranked.append((player_id, loot))
    if not ranked:
        return 0

    visible = _visible_player_ids(session, [pid for pid, _ in ranked])
    leader = next(((pid, loot) for pid, loot in ranked if pid in visible), None)
    if leader is None:
        return 0
    player_id, loot = leader

    context = {"period": period, "loot": loot}
    if dry_run:
        holder = _held_by(session, badge, period)
        if holder == player_id or (not held and holder is not None):
            return 0
        _log(f"DRY-RUN {badge.key} [{period}]: player {player_id} takes it with "
             f"{loot:,} gp (held by {holder if holder is not None else 'nobody'})")
        return 1

    if held:
        outcome = transfer_held_badge(session, badge, period, player_id, context)
        session.commit()  # no-op when the holder and context are unchanged
        if outcome == "retained":
            return 0
    else:
        if award_badge(session, badge, player_id, slot_key=period, context=context) is None:
            return 0  # slot already awarded (or revoked — revocation is sticky)
        session.commit()
        outcome = "awarded"
    _log(f"{badge.key} [{period}]: {outcome} to player {player_id} ({loot:,} gp)")
    return 1

def evaluate_streaks(session, badge: Badge, day: str, days: int, dry_run: bool = False) -> int:
    """Award ``badge`` to players present on ``leaderboard:{day}`` and every
    one of the previous ``days - 1`` daily boards. Returns award count."""
    conn = redis_client.client
    members = conn.zrange(f"leaderboard:{day}", 0, -1)
    candidates: List[int] = []
    for m in members:
        try:
            candidates.append(int(m.decode() if isinstance(m, bytes) else m))
        except (ValueError, AttributeError):
            continue
    if not candidates:
        return 0

    # One indexed query (chunked) removes players who already hold the badge.
    holders: set = set()
    for i in range(0, len(candidates), _CHUNK):
        chunk = candidates[i:i + _CHUNK]
        rows = (
            session.query(PlayerBadge.player_id)
            .filter(
                PlayerBadge.badge_id == badge.badge_id,
                PlayerBadge.player_id.in_(chunk),
            )
            .all()
        )
        holders.update(pid for (pid,) in rows)
    survivors = [pid for pid in candidates if pid not in holders]
    if not survivors:
        return 0

    # Pipeline ZSCOREs across the prior days; drop anyone missing a day.
    day_dt = datetime.strptime(day, "%Y%m%d")
    prior_keys = [
        f"leaderboard:{day_token(day_dt - timedelta(days=offset))}"
        for offset in range(1, days)
    ]
    for key in prior_keys:
        if not survivors:
            return 0
        still: List[int] = []
        for i in range(0, len(survivors), _CHUNK):
            chunk = survivors[i:i + _CHUNK]
            pipe = conn.pipeline(transaction=False)
            for pid in chunk:
                pipe.zscore(key, pid)
            scores = pipe.execute()
            still.extend(
                pid for pid, s in zip(chunk, scores) if s is not None and float(s) > 0
            )
        survivors = still

    awarded = 0
    for pid in survivors:
        if dry_run:
            _log(f"DRY-RUN loot_streak_{days} ending {day}: player {pid}")
            awarded += 1
            continue
        award = award_badge(
            session, badge, pid, slot_key=f"p:{pid}", context={"day": day, "days": days}
        )
        if award is not None:
            awarded += 1
    if not dry_run and awarded:
        session.commit()
        _log(f"loot_streak_{days} ending {day}: awarded to {awarded} player(s)")
    return awarded


def evaluate_boss_records(session, badge: Badge, dry_run: bool = False) -> Dict[str, int]:
    """Converge the held boss-record badge across every clean (npc, team_size)
    slot in ``personal_best``. Returns outcome counts."""
    from sqlalchemy import text

    slots: List[Tuple[int, str]] = [
        (npc_id, team_size)
        for npc_id, team_size in session.execute(
            text("SELECT DISTINCT npc_id, team_size FROM personal_best WHERE npc_id IS NOT NULL")
        )
        if team_size and _TEAM_SIZE_RE.match(team_size)
    ]
    npc_ids = sorted({npc_id for npc_id, _ in slots})
    npc_names: Dict[int, str] = {}
    for i in range(0, len(npc_ids), _CHUNK):
        rows = (
            session.query(NpcList.npc_id, NpcList.npc_name)
            .filter(NpcList.npc_id.in_(npc_ids[i:i + _CHUNK]))
            .all()
        )
        npc_names.update({nid: name for nid, name in rows})

    counts = {"retained": 0, "transferred": 0, "awarded": 0, "skipped": 0}
    for npc_id, team_size in slots:
        row = session.execute(
            text(
                "SELECT player_id, personal_best FROM personal_best "
                "WHERE npc_id = :npc AND team_size = :team AND personal_best > 0 "
                "AND player_id IS NOT NULL "
                "ORDER BY personal_best ASC, date_added ASC, id ASC LIMIT 1"
            ),
            {"npc": npc_id, "team": team_size},
        ).first()
        if row is None:
            counts["skipped"] += 1
            continue
        player_id, pb_ms = int(row[0]), int(row[1])
        slot_key = f"npc:{npc_id}:{team_size}"
        context = {
            "npc_id": npc_id,
            "npc_name": npc_names.get(npc_id),
            "team_size": team_size,
            "pb_ms": pb_ms,
        }
        if dry_run:
            _log(
                f"DRY-RUN boss_record {slot_key}: player {player_id} "
                f"({npc_names.get(npc_id)}, {team_size}, {pb_ms}ms)"
            )
            counts["awarded"] += 1
            continue
        outcome = transfer_held_badge(session, badge, slot_key, player_id, context)
        counts[outcome] += 1
    if not dry_run:
        session.commit()
        _log(f"boss_record: {counts}")
    return counts


# ----------------------------
# Orchestrator
# ----------------------------

def _load_automatic_badges(session) -> List[Tuple[Badge, dict]]:
    out = []
    rows = (
        session.query(Badge)
        .filter(Badge.active == True, Badge.criteria.isnot(None))  # noqa: E712
        .all()
    )
    for b in rows:
        try:
            crit = json.loads(b.criteria)
        except (TypeError, ValueError):
            _log(f"badge {b.key}: unparseable criteria, skipping")
            continue
        if isinstance(crit, dict) and crit.get("type"):
            out.append((b, crit))
    return out


def _months_to_process(day_list: List[str], now: Optional[datetime] = None) -> List[str]:
    """Month tokens the monthly leader badge converges this cycle.

    Always the current month, plus the month of every day being processed — so
    the first run after a rollover re-converges the month that just ended
    against its final board before opening the new month's slot. (Without it,
    a month's winner would freeze at whatever the last cycle *inside* that
    month saw, missing everything logged after it.)
    """
    tokens = [month_token(now or datetime.now())]
    for day in day_list:
        try:
            token = month_token(datetime.strptime(day, "%Y%m%d"))
        except ValueError:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def _leader_periods(crit: dict, month_list: List[str]) -> List[str]:
    """Partition tokens a ``global_champion`` badge's criteria asks for.

    ``{"period": "all"}`` (the default) → the all-time board; ``"month"`` →
    every month token due this cycle; a literal token (``"202606"``) pins the
    badge to one board.
    """
    period = str(crit.get("period") or ALL).strip().lower()
    if period in ("month", "monthly"):
        return list(month_list)
    if period in (ALL, "alltime", "all_time"):
        return [ALL]
    if is_valid_token(period):
        return [period]
    return []


def _days_to_process(yesterday_token: str) -> List[str]:
    """Day tokens after the completion marker, up through yesterday."""
    y_dt = datetime.strptime(yesterday_token, "%Y%m%d")
    marker = None
    try:
        raw = redis_client.client.get(LAST_COMPLETED_DAY_KEY)
        if raw:
            marker = raw.decode() if isinstance(raw, bytes) else str(raw)
    except Exception:
        marker = None

    if marker:
        try:
            start_dt = datetime.strptime(marker, "%Y%m%d") + timedelta(days=1)
        except ValueError:
            start_dt = y_dt
    else:
        start_dt = y_dt  # first run: just yesterday (backfill script covers history)
    floor = y_dt - timedelta(days=MAX_CATCHUP_DAYS)
    if start_dt < floor:
        start_dt = floor

    days = []
    cur = start_dt
    while cur <= y_dt:
        days.append(day_token(cur))
        cur += timedelta(days=1)
    return days


def run_badge_cycle(dry_run: bool = False, days: Optional[List[str]] = None,
                    only: Optional[str] = None,
                    months: Optional[List[str]] = None) -> dict:
    """Evaluate all automatic badges. Called hourly from the bot; the day
    marker makes the day-scoped families a single Redis GET after the first
    run of the day.

    ``days`` overrides the marker-derived day list (used by scripts);
    ``months`` does the same for the monthly leader badge; ``only`` restricts
    to 'daily' | 'streaks' | 'records' | 'leaders'.
    """
    yesterday = day_token(datetime.now() - timedelta(days=1))
    day_list = days if days is not None else _days_to_process(yesterday)
    month_list = months if months is not None else _months_to_process(day_list)
    stats = {"days": day_list, "months": month_list,
             "daily": 0, "streaks": 0, "records": {}, "leaders": {}}

    # The day-scoped families (and record convergence, which rides the daily
    # run) no-op once the day's marker is set. The held leader badges are the
    # exception: they track a live board, so they converge every cycle — two
    # Redis reads and a couple of indexed queries — and the site's #1 chip is
    # never more than an hour stale.
    explicit = days is not None or only is not None
    run_daily_families = bool(day_list) or explicit

    session = Session()
    try:
        badges = _load_automatic_badges(session)
        for badge, crit in badges:
            ctype = crit.get("type")
            if ctype == "daily_champion" and only in (None, "daily"):
                for day in day_list:
                    stats["daily"] += evaluate_daily_champion(session, badge, day, dry_run)
            elif ctype == "loot_streak" and only in (None, "streaks"):
                n = int(crit.get("days", 0))
                if n < 2:
                    continue
                for day in day_list:
                    stats["streaks"] += evaluate_streaks(session, badge, day, n, dry_run)
            elif ctype == "boss_record" and only in (None, "records"):
                if not run_daily_families:
                    continue
                stats["records"][badge.key] = evaluate_boss_records(session, badge, dry_run)
            elif ctype == "global_champion" and only in (None, "leaders"):
                periods = _leader_periods(crit, month_list)
                if not periods:
                    _log(f"badge {badge.key}: unknown criteria period "
                         f"{crit.get('period')!r}, skipping")
                    continue
                for token in periods:
                    stats["leaders"][f"{badge.key}:{token}"] = evaluate_global_champion(
                        session, badge, dry_run, period=token
                    )

        if not dry_run and days is None and day_list:
            redis_client.client.set(LAST_COMPLETED_DAY_KEY, day_list[-1])
    finally:
        session.close()
    return stats
