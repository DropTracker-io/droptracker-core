"""The player stat history store: ``player_stat_snapshots``.

One table holds every observation of a player's stats, whichever source it
came from, and this module is the only thing that writes or reads it. The
plugin's state sync is the first writer (`record_plugin_snapshot`); the
hiscores poller and the WOM-shaped read API build on the same rows (dev
tracker project 40).

Readers ask for "the row at or before a time" (`snapshot_at`). That is the
XP at that time because a row is only written when the values changed.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from utils.xp_history import SKILLS, period_starts, skill_summary

SOURCE_PLUGIN = "plugin"
SOURCE_HISCORES = "hiscores"
SOURCE_WOM = "wom"

# Syncs inside one wall-clock window of this length share a row, so a player
# who syncs every minute costs at most 96 rows a day, not 1,440.
PLUGIN_BUCKET_MINUTES = 15


def encode_skills(xp: Mapping[str, Any]) -> str:
    """Canonical JSON for the ``skills`` column: known skills, panel order."""
    return json.dumps({skill: max(0, int(xp.get(skill, 0) or 0)) for skill in SKILLS},
                      separators=(",", ":"))


def decode_skills(raw: Any) -> Dict[str, int]:
    """``{skill: xp}`` from a ``skills`` column; ``{}`` if it is unreadable."""
    try:
        loaded = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    out = {}
    for skill in SKILLS:
        value = loaded.get(skill)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[skill] = value
    return out


def encode_activities(scores: Optional[Mapping[str, Any]]) -> Optional[str]:
    """JSON for the ``activities`` column: metric -> score, non-zero only.

    A metric that is absent is unknown, never zero. None when there is
    nothing to store.
    """
    clean = {}
    for metric, score in (scores or {}).items():
        if isinstance(score, int) and not isinstance(score, bool) and score > 0 and metric:
            clean[str(metric)] = score
    if not clean:
        return None
    return json.dumps(dict(sorted(clean.items())), separators=(",", ":"))


def decode_activities(raw: Any) -> Dict[str, int]:
    """``{metric: score}`` from an ``activities`` column; ``{}`` if absent."""
    try:
        loaded = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        str(metric): score for metric, score in loaded.items()
        if isinstance(score, int) and not isinstance(score, bool) and score > 0
    }


def plugin_boss_scores(db_session, player_id: int,
                       metric_for: Callable[[str], Optional[str]]) -> Dict[str, int]:
    """Boss kill counts the plugin has reported, keyed by hiscores metric.

    Read from the ``player_npc_kc`` watermarks. A partial set by nature: only
    bosses this player has killed with the plugin running. ``metric_for``
    maps an NPC name to its metric slug (`utils.wiseoldman.wom_boss_metric`)
    or None for an NPC the hiscores do not track. Two NPCs sharing a metric
    keep the higher count.
    """
    from db.models import NpcList, PlayerNpcKc

    rows = (
        db_session.query(NpcList.npc_name, PlayerNpcKc.kill_count)
        .join(NpcList, NpcList.npc_id == PlayerNpcKc.npc_id)
        .filter(PlayerNpcKc.player_id == player_id)
        .all()
    )
    out: Dict[str, int] = {}
    for name, kill_count in rows:
        metric = metric_for(name)
        if metric and kill_count and kill_count > out.get(metric, 0):
            out[metric] = int(kill_count)
    return out


def same_bucket(a: datetime, b: datetime) -> bool:
    """Whether two times fall in the same plugin snapshot window."""
    def bucket(t: datetime) -> Tuple:
        return (t.date(), (t.hour * 60 + t.minute) // PLUGIN_BUCKET_MINUTES)
    return bucket(a) == bucket(b)


def record_plugin_snapshot(db_session, player_id: int, xp: Mapping[str, Any], *,
                           account_type: Optional[int] = None,
                           activities: Optional[Callable[[], Mapping[str, int]]] = None,
                           now: Optional[datetime] = None) -> bool:
    """Store the XP a state sync reported, if it is news. True if written.

    ``activities`` is called only when a row is about to be written, and its
    scores are stored with it; XP is what decides whether there is news.

    Nothing is written when XP equals the player's previous plugin row. A row
    from the current window is brought up to date in place (and re-stamped),
    so it ends as the window's closing values; a pinned row is left alone and
    a new one written beside it.

    Runs inside the caller's transaction, in a SAVEPOINT: a failure here (the
    table not migrated yet, a deadlock) must not cost the sync its commit.
    """
    from db.models import PlayerStatSnapshot

    now = (now or datetime.now()).replace(microsecond=0)
    try:
        with db_session.begin_nested():
            summary = skill_summary(xp)
            # All zeroes is a row that never carried XP, not an account.
            if summary["total_xp"] <= 0:
                return False
            payload = encode_skills(xp)
            last = (
                db_session.query(PlayerStatSnapshot)
                .filter(PlayerStatSnapshot.player_id == player_id,
                        PlayerStatSnapshot.source == SOURCE_PLUGIN)
                .order_by(PlayerStatSnapshot.taken_at.desc())
                .first()
            )
            if last is not None:
                if decode_skills(last.skills) == decode_skills(payload):
                    return False
                # A clock that stepped back must not write history out of order.
                if last.taken_at >= now and (last.pinned or last.taken_at > now):
                    return False
            if last is not None and not last.pinned and same_bucket(last.taken_at, now):
                row = last
                row.taken_at = now
            else:
                row = PlayerStatSnapshot(player_id=player_id, taken_at=now,
                                         source=SOURCE_PLUGIN)
                db_session.add(row)
            row.skills = payload
            row.total_xp = summary["total_xp"]
            row.total_level = summary["total_level"]
            if activities is not None:
                try:
                    with db_session.begin_nested():
                        scores = encode_activities(activities())
                except Exception:
                    scores = None
                # Never blank scores an earlier sync in this window stored.
                if scores is not None:
                    row.activities = scores
            if isinstance(account_type, int) and not isinstance(account_type, bool):
                row.account_type = account_type
            db_session.flush()
        return True
    except Exception:
        return False


def _usable(db_session, player_id: int):
    from db.models import PlayerStatSnapshot

    return (
        db_session.query(PlayerStatSnapshot)
        .filter(PlayerStatSnapshot.player_id == player_id,
                PlayerStatSnapshot.flagged.is_(False))
    )


def snapshot_at(db_session, player_id: int, when: datetime):
    """The latest usable row at or before ``when``, or None."""
    from db.models import PlayerStatSnapshot

    return (
        _usable(db_session, player_id)
        .filter(PlayerStatSnapshot.taken_at <= when)
        .order_by(PlayerStatSnapshot.taken_at.desc())
        .first()
    )


def earliest_snapshot(db_session, player_id: int):
    """The player's first usable row, or None."""
    from db.models import PlayerStatSnapshot

    return _usable(db_session, player_id).order_by(PlayerStatSnapshot.taken_at.asc()).first()


def _baseline(row):
    if row is None:
        return None
    xp = decode_skills(row.skills)
    return (row.taken_at, xp) if xp else None


def xp_baselines(db_session, player_id: int, now: datetime):
    """``(baselines, earliest)`` for `utils.xp_history.gains_since`.

    One indexed lookup per period plus one for the first row, rather than
    loading a month of rows to pick four out of.
    """
    baselines = {
        period: _baseline(snapshot_at(db_session, player_id, start))
        for period, start in period_starts(now).items()
    }
    earliest = None
    if any(value is None for value in baselines.values()):
        earliest = _baseline(earliest_snapshot(db_session, player_id))
    return baselines, earliest
