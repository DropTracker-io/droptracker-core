"""Hiscores rank milestones — "entered the top 10,000/5,000/1,000" announcements.

Fed from WiseOldMan's bulk-hiscores endpoint (one request returns every group
member's latest snapshot: rank + value for every skill, boss and activity), so
no per-player API traffic. Driven by data/player_total_updater.py's
rank_milestones_loop on a fixed cycle; each enabled group costs one WOM call
per cycle.

Crossing detection keeps a per-(group, player) **best-rank-ever** watermark in
Redis (``rank_watch:{group_id}:{player_id}`` — hash of metric slug → lowest
rank seen). Best-rank rather than best-band, for two reasons:

* WOM jitter cannot re-announce: 9,950 → 10,050 → 9,900 never re-crosses
  10,000 because the stored best stays 9,950;
* threshold-list edits are safe: a group adding 7,500 later does not flood
  members whose best rank is already past it.

A metric with **no stored value seeds silently** — the first cycle for a
group, and every newly-joined member, establishes baselines without
announcing (a member joining at rank 800 did not just "enter the top 1,000").
Unranked metrics (rank 0/absent) are skipped and never stored. The watermark
is written BEFORE the notification is enqueued, so a crash between the two
loses one announcement rather than duplicating it next cycle. Losing the Redis
state entirely is also safe by construction: everything silently re-seeds.

Only the DEEPEST newly-entered threshold announces per (player, metric) per
cycle — rocketing from 20,000 to 800 is one "top 1,000" message, not three.
"""

import asyncio
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Redis key for one (group, player)'s per-metric best ranks.
def rank_watch_key(group_id: int, player_id: int) -> str:
    return f"rank_watch:{group_id}:{player_id}"


#: Watermark TTL, refreshed on every cycle that still sees the player — so
#: departed members and disabled groups clean themselves up.
STATE_TTL_SECONDS = 90 * 24 * 3600

#: Config keys (mirrored in web_api/config_registry.py and the TS registry).
CONFIG_MASTER_KEY = "notify_rank_milestones"
CONFIG_THRESHOLDS_KEY = "rank_milestone_thresholds"
CONFIG_SCOPE_KEYS = {
    "boss": "rank_milestone_bosses",
    "skill": "rank_milestone_skills",
    "clue": "rank_milestone_clues",
}
DEFAULT_THRESHOLDS = (10000, 5000, 1000)

_CLUE_PREFIX = "clue_scrolls"


def parse_thresholds(raw) -> List[int]:
    """A config value ("10000,5000,1000", JSON list, or list) → sorted ints.

    Sorted ascending so the first threshold satisfying the crossing test is
    the deepest one. Invalid/empty input falls back to the defaults.
    """
    values: Iterable
    if raw is None:
        return sorted(DEFAULT_THRESHOLDS)
    if isinstance(raw, (list, tuple)):
        values = raw
    else:
        text = str(raw).strip()
        if not text:
            return sorted(DEFAULT_THRESHOLDS)
        text = text.strip("[]")
        values = text.split(",")
    out = set()
    for value in values:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if number > 0:
            out.add(number)
    return sorted(out) if out else sorted(DEFAULT_THRESHOLDS)


def crossed_threshold(prev_best: Optional[int], new_rank: int, thresholds: List[int]) -> Optional[int]:
    """The deepest threshold newly entered, or None.

    Rank improves DOWNWARD: entering the top-T bracket means
    ``prev_best > T >= new_rank``. ``prev_best is None`` is the silent-seed
    case and never announces.
    """
    if prev_best is None or new_rank <= 0 or new_rank >= prev_best:
        return None
    for threshold in thresholds:  # ascending: first hit is the deepest
        if prev_best > threshold >= new_rank:
            return threshold
    return None


def metric_kind(section: str, metric: str) -> Optional[str]:
    """boss / skill / clue, or None for metrics outside the feature's scope."""
    if section == "bosses":
        return "boss"
    if section == "skills":
        return "skill"
    if section == "activities" and str(metric).startswith(_CLUE_PREFIX):
        return "clue"
    return None


def metric_label(kind: str, metric: str) -> str:
    """Human-readable label for a WOM metric slug."""
    slug = str(metric)
    if kind == "clue":
        tier = slug[len(_CLUE_PREFIX):].strip("_")
        if not tier or tier == "all":
            return "Clue Scrolls (all)"
        return f"{tier.title()} Clue Scrolls"
    return slug.replace("_", " ").title()


def _metric_value(kind: str, entry: Dict[str, Any]) -> Optional[int]:
    """The metric's own value (KC / XP / completions) for display."""
    key = {"boss": "kills", "skill": "experience", "clue": "score"}.get(kind)
    try:
        value = int(entry.get(key))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def evaluate_member_snapshot(
    snapshot_data: Dict[str, Any],
    stored: Dict[str, int],
    thresholds: List[int],
    enabled_kinds: Iterable[str],
) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    """One member's snapshot vs their stored best ranks.

    Pure — this is the piece the unit tests drive. Returns
    ``(watermark_updates, crossings)``: every improved (or newly seen) ranked
    metric goes into the updates map; crossings only for stored metrics that
    newly entered a threshold.
    """
    enabled = set(enabled_kinds)
    updates: Dict[str, int] = {}
    crossings: List[Dict[str, Any]] = []

    for section in ("bosses", "skills", "activities"):
        entries = snapshot_data.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for metric, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            kind = metric_kind(section, metric)
            if kind is None or kind not in enabled:
                continue
            try:
                rank = int(entry.get("rank"))
            except (TypeError, ValueError):
                continue
            if rank <= 0:  # unranked — skip, never store
                continue
            metric_slug = str(metric)
            prev_best = stored.get(metric_slug)
            if prev_best is not None and rank >= prev_best:
                continue
            updates[metric_slug] = rank
            threshold = crossed_threshold(prev_best, rank, thresholds)
            if threshold is not None:
                crossings.append({
                    "metric": metric_slug,
                    "metric_kind": kind,
                    "metric_label": metric_label(kind, metric_slug),
                    "rank": rank,
                    "threshold": threshold,
                    "previous_best_rank": prev_best,
                    "value": _metric_value(kind, entry),
                })
    return updates, crossings


def _decode_stored(raw: Dict) -> Dict[str, int]:
    """A raw HGETALL result (bytes keys/values) → {metric: best_rank}."""
    out: Dict[str, int] = {}
    for key, value in (raw or {}).items():
        try:
            name = key.decode() if isinstance(key, bytes) else str(key)
            out[name] = int(value)
        except (TypeError, ValueError, UnicodeDecodeError):
            continue
    return out


async def process_group(session, group_id: int, wom_id: int, redis_conn) -> Dict[str, int]:
    """Run one cycle for one enabled group. Returns counters for the log line."""
    from data.submissions.common import create_notification
    from db.models import Player
    from utils import group_config as gc
    from utils.wiseoldman import get_group_bulk_hiscores

    stats = {"members": 0, "seeded": 0, "announced": 0}

    rows = await get_group_bulk_hiscores(wom_id)
    if not rows:
        return stats

    thresholds = parse_thresholds(gc.get(session, group_id, CONFIG_THRESHOLDS_KEY, None))
    enabled_kinds = [
        kind for kind, key in CONFIG_SCOPE_KEYS.items()
        if str(gc.get(session, group_id, key, "1")).strip().lower() not in ("0", "false", "no", "")
    ]
    if not enabled_kinds:
        return stats

    # WOM player id → our Player, one query for the whole roster.
    wom_ids = []
    for row in rows:
        wom_player = (row or {}).get("player") or {}
        if wom_player.get("id") is not None:
            wom_ids.append(int(wom_player["id"]))
    if not wom_ids:
        return stats
    players_by_wom_id = {
        p.wom_id: p
        for p in session.query(Player).filter(Player.wom_id.in_(wom_ids)).all()
    }

    for row in rows:
        await asyncio.sleep(0)
        wom_player = (row or {}).get("player") or {}
        player = players_by_wom_id.get(wom_player.get("id"))
        if player is None:
            continue
        snapshot = ((row.get("data") or {}).get("data")) or {}
        if not isinstance(snapshot, dict):
            continue
        stats["members"] += 1

        key = rank_watch_key(group_id, player.player_id)
        stored = _decode_stored(redis_conn.hgetall(key))
        updates, crossings = evaluate_member_snapshot(
            snapshot, stored, thresholds, enabled_kinds
        )
        if not stored and updates:
            stats["seeded"] += 1
        if updates:
            # Watermark FIRST: a crash between here and the enqueue loses an
            # announcement (missed), never duplicates one next cycle.
            redis_conn.hset(key, mapping={m: str(r) for m, r in updates.items()})
        redis_conn.expire(key, STATE_TTL_SECONDS)

        # A brand-new watermark seeds silently — no stored metrics, so
        # evaluate_member_snapshot produced no crossings anyway.
        for crossing in crossings:
            payload = {
                "player_name": player.player_name,
                "player_id": player.player_id,
                **crossing,
            }
            if crossing["metric_kind"] == "boss":
                # So per-group NPC blacklist entries match rank posts too.
                payload["npc_name"] = crossing["metric_label"]
            await create_notification(
                "rank_milestone",
                player.player_id,
                payload,
                group_id,
                existing_session=session,
            )
            stats["announced"] += 1

    return stats


async def run_rank_milestone_cycle(session_factory=None) -> Dict[str, int]:
    """One full sweep over every enabled, WOM-linked group."""
    from db.models import Group, GroupConfiguration, Session
    from utils.redis import redis_client

    factory = session_factory or Session
    totals = {"groups": 0, "members": 0, "seeded": 0, "announced": 0}

    with factory() as session:
        enabled_rows = (
            session.query(GroupConfiguration.group_id)
            .filter(
                GroupConfiguration.config_key == CONFIG_MASTER_KEY,
                GroupConfiguration.config_value.in_(("1", "true", "True")),
            )
            .all()
        )
        enabled_ids = [row[0] for row in enabled_rows]
        if not enabled_ids:
            return totals
        # Scalars, not ORM instances — the groups outlive this session.
        groups = (
            session.query(Group.group_id, Group.wom_id)
            .filter(Group.group_id.in_(enabled_ids), Group.wom_id.isnot(None))
            .all()
        )

    for group_id, wom_id in groups:
        with factory() as session:
            try:
                stats = await process_group(session, group_id, wom_id, redis_client.client)
                session.commit()
            except Exception as e:
                session.rollback()
                print(f"[RankMilestones] Cycle failed for group {group_id}: {e}")
                continue
        totals["groups"] += 1
        for key in ("members", "seeded", "announced"):
            totals[key] += stats[key]

    return totals
