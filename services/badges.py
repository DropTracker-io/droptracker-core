"""Badge award engine (see db/models/badge.py for the schema/dedupe design).

Sync, session-based functions (same style as services/points.py) — safe to run
from the bot via ``asyncio.to_thread`` or from CLI scripts.

Automatic badge behavior is code-owned: a badge row's ``criteria`` JSON has a
``type`` field that maps to an evaluator here (``daily_champion``,
``loot_streak``, ``boss_record``). Definitions (name/tone/icon/active) stay
admin-editable in the DB.

All evaluators are idempotent — re-running any day converges to the same
awards — so the hourly ``run_badge_cycle`` can safely catch up after downtime.
Data sources are Redis daily boards and the small ``personal_best`` table
only; the 160M-row ``drops`` table is never touched.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from db import Badge, NpcList, PlayerBadge, Session
from utils.partitions import day_token
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
                    only: Optional[str] = None) -> dict:
    """Evaluate all automatic badges. Called hourly from the bot; the day
    marker makes runs after the first of the day a single Redis GET.

    ``days`` overrides the marker-derived day list (used by scripts);
    ``only`` restricts to 'daily' | 'streaks' | 'records'.
    """
    yesterday = day_token(datetime.now() - timedelta(days=1))
    day_list = days if days is not None else _days_to_process(yesterday)
    stats = {"days": day_list, "daily": 0, "streaks": 0, "records": {}}

    # Hourly no-op fast path: day already processed and no explicit request —
    # skip everything (including record convergence, which rides the daily run).
    if not day_list and days is None and only is None:
        return stats

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
                stats["records"][badge.key] = evaluate_boss_records(session, badge, dry_run)

        if not dry_run and days is None and day_list:
            redis_client.client.set(LAST_COMPLETED_DAY_KEY, day_list[-1])
    finally:
        session.close()
    return stats
