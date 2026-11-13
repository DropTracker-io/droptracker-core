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


# --------------------------------------------------------------------------- #
# Hiscores rows
# --------------------------------------------------------------------------- #

# A hiscores reading is only as fresh as the player's last logout, and the
# name on it can change hands. Before a reading becomes history it has to be
# believable next to the player's previous one.
#
# XP a player could not have earned: more than this per hour since the last
# reading, plus a flat allowance for lamps and banked turn-ins. Deliberately
# loose (the fastest sustained methods are around 1M to 1.5M an hour); it is
# there to catch a different account answering to the name, not to judge play.
MAX_XP_PER_HOUR = 2_500_000
XP_ALLOWANCE = 5_000_000

WRITTEN, UNCHANGED, FLAGGED, STALE = "written", "unchanged", "flagged", "stale"


def encode_metrics(values: Optional[Mapping[str, Any]]) -> Optional[str]:
    """JSON for ``ranks`` / hiscores ``skills``: only real, non-negative ints."""
    clean = {
        str(metric): value for metric, value in (values or {}).items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0 and metric
    }
    return json.dumps(dict(sorted(clean.items())), separators=(",", ":")) if clean else None


def plausibility_problem(previous_skills: Mapping[str, int], previous_activities: Mapping[str, int],
                         previous_at: datetime, skills: Mapping[str, int],
                         activities: Mapping[str, int], now: datetime) -> Optional[str]:
    """Why a hiscores reading cannot follow the previous one, or None.

    Compared metric by metric over what both readings know: a skill or boss
    missing from either side is unknown, not zero.
    """
    for skill, xp in skills.items():
        before = previous_skills.get(skill)
        if before is not None and xp < before:
            return f"{skill} xp went down ({before} -> {xp})"
    for metric, score in activities.items():
        before = previous_activities.get(metric)
        if before is not None and score < before:
            return f"{metric} went down ({before} -> {score})"
    gained = sum(xp - previous_skills[s] for s, xp in skills.items() if s in previous_skills)
    hours = max(0.0, (now - previous_at).total_seconds() / 3600.0)
    if gained > XP_ALLOWANCE + MAX_XP_PER_HOUR * hours:
        return f"{gained} xp in {hours:.1f}h"
    return None


def record_hiscores_snapshot(db_session, player_id: int, skills: Mapping[str, int],
                             activities: Mapping[str, int], ranks: Mapping[str, int], *,
                             total_xp: Optional[int] = None, total_level: Optional[int] = None,
                             account_type: Optional[int] = None, pinned: bool = False,
                             now: Optional[datetime] = None) -> str:
    """Store a hiscores reading. Returns WRITTEN, UNCHANGED, FLAGGED or STALE.

    - UNCHANGED: identical to the player's previous hiscores row. Nothing is
      written, unless ``pinned`` asks for a row at this moment regardless.
    - STALE: the plugin has already shown us more XP than this. The hiscores
      only catch up at logout, so the reading is old news; it is not written
      and, if ``pinned``, the plugin's latest numbers are repeated in a pinned
      row at this moment instead (`carry_forward`).
    - FLAGGED: it cannot follow the previous hiscores row (`plausibility_problem`).
      The row is kept, marked, and never read as a baseline.

    Unlike plugin rows, ``skills`` holds only what the hiscores ranked.
    The caller commits.
    """
    from db.models import PlayerStatSnapshot

    now = (now or datetime.now()).replace(microsecond=0)
    skills = {s: int(v) for s, v in skills.items() if s in SKILLS and isinstance(v, int) and v >= 0}
    if not skills:
        return UNCHANGED
    known_total = total_xp if isinstance(total_xp, int) and total_xp >= 0 else sum(skills.values())
    activities_json = encode_activities(activities)
    skills_json = encode_metrics(skills)

    def latest(*criteria):
        return (
            db_session.query(PlayerStatSnapshot)
            .filter(PlayerStatSnapshot.player_id == player_id, *criteria)
            .order_by(PlayerStatSnapshot.taken_at.desc())
            .first()
        )

    newest = latest(PlayerStatSnapshot.flagged.is_(False))
    if (newest is not None and newest.source == SOURCE_PLUGIN
            and int(newest.total_xp or 0) > known_total):
        if pinned:
            carry_forward(db_session, player_id, now)
        return STALE

    last = latest(PlayerStatSnapshot.source == SOURCE_HISCORES)
    same = (last is not None and decode_skills(last.skills) == skills
            and decode_activities(last.activities) == decode_activities(activities_json))
    if same and last.flagged:
        return FLAGGED
    if same and not pinned:
        return UNCHANGED
    if last is not None and last.taken_at >= now:
        return UNCHANGED

    problem = None
    if not same:
        trusted = latest(PlayerStatSnapshot.source == SOURCE_HISCORES,
                         PlayerStatSnapshot.flagged.is_(False))
        if trusted is not None:
            problem = plausibility_problem(
                decode_skills(trusted.skills), decode_activities(trusted.activities),
                trusted.taken_at, skills, decode_activities(activities_json), now)

    # A hiscores reading is complete (every skill and boss the player is
    # ranked in), so it can carry the player's EHP and EHB with it.
    scored = efficiency_for(skills, decode_activities(activities_json),
                            account_type if isinstance(account_type, int) else None) or {}
    db_session.add(PlayerStatSnapshot(
        player_id=player_id, taken_at=now, source=SOURCE_HISCORES,
        account_type=account_type if isinstance(account_type, int) else None,
        total_xp=known_total,
        total_level=total_level if isinstance(total_level, int) and total_level > 0 else 0,
        skills=skills_json, activities=activities_json, ranks=encode_metrics(ranks),
        ehp=scored.get("ehp"), ehb=scored.get("ehb"),
        flagged=problem is not None, pinned=bool(pinned) and problem is None,
    ))
    db_session.flush()
    return FLAGGED if problem else WRITTEN


def pin_latest(db_session, player_id: int) -> bool:
    """Pin the player's newest usable row, so downsampling keeps it. True if
    there was one."""
    from db.models import PlayerStatSnapshot

    row = _usable(db_session, player_id).order_by(PlayerStatSnapshot.taken_at.desc()).first()
    if row is None:
        return False
    if not row.pinned:
        row.pinned = True
        db_session.flush()
    return True


# --------------------------------------------------------------------------- #
# What a player's stats were at a moment
# --------------------------------------------------------------------------- #

def values_at(db_session, player_id: int, when: datetime) -> Optional[dict]:
    """A player's stats at ``when``, from every source we have:
    ``{"skills", "activities", "taken_at", "source"}``, or None with no rows.

    The plugin and the hiscores each know things the other does not: the
    plugin has exact XP but only the bosses it has seen killed; the hiscores
    have every boss but lag until logout. XP and kill counts only ever go up,
    so the two newest rows (one per source) are merged by taking the higher
    value of each metric. ``source`` names whichever row is newer.
    """
    from db.models import PlayerStatSnapshot

    rows = []
    for name in (SOURCE_PLUGIN, SOURCE_HISCORES, SOURCE_WOM):
        row = (
            _usable(db_session, player_id)
            .filter(PlayerStatSnapshot.source == name, PlayerStatSnapshot.taken_at <= when)
            .order_by(PlayerStatSnapshot.taken_at.desc())
            .first()
        )
        if row is not None:
            rows.append(row)
    if not rows:
        return None
    skills: Dict[str, int] = {}
    activities: Dict[str, int] = {}
    for row in rows:
        for skill, xp in decode_skills(row.skills).items():
            # A plugin row lists every skill, with 0 for one it could not
            # read: 0 is "unknown" unless nothing better exists.
            if xp > skills.get(skill, -1):
                skills[skill] = xp
        for metric, score in decode_activities(row.activities).items():
            if score > activities.get(metric, 0):
                activities[metric] = score
    newest = max(rows, key=lambda r: r.taken_at)
    return {"skills": skills, "activities": activities,
            "taken_at": newest.taken_at, "source": newest.source}


def first_values_in_window(db_session, player_id: int, start: datetime, end: datetime) -> Optional[dict]:
    """A player's stats at the start of a window, the way Wise Old Man reads
    it: from the first reading taken INSIDE ``[start, end]``, shaped like
    `values_at` (``taken_at`` is that first reading's time).

    Nothing from before the window is used. A reading from before it cannot
    say what happened between then and the window opening, and counting that
    would let XP earned before an event be credited to it. The cost is the
    other way round and small: what a player gains between the window opening
    and their first reading in it is not counted. The poller keeps that gap
    short by taking a reading for every participant as an event starts.

    Each source's first reading in the window is used, and for each metric
    the earlier of them that knows it: a plugin row has exact XP but only
    some bosses, a hiscores row has every boss.
    """
    from db.models import PlayerStatSnapshot

    firsts = []
    for name in (SOURCE_PLUGIN, SOURCE_HISCORES, SOURCE_WOM):
        row = (
            _usable(db_session, player_id)
            .filter(PlayerStatSnapshot.source == name, PlayerStatSnapshot.taken_at >= start,
                    PlayerStatSnapshot.taken_at <= end)
            .order_by(PlayerStatSnapshot.taken_at.asc())
            .first()
        )
        if row is not None:
            firsts.append(row)
    if not firsts:
        return None
    firsts.sort(key=lambda r: r.taken_at)
    skills: Dict[str, int] = {}
    activities: Dict[str, int] = {}
    for row in firsts:
        for skill, xp in decode_skills(row.skills).items():
            # A plugin row lists every skill and writes 0 for one it could
            # not read; let a later reading that does know it fill that in.
            if skill not in skills or (skills[skill] == 0 and xp > 0):
                skills[skill] = xp
        for metric, score in decode_activities(row.activities).items():
            activities.setdefault(metric, score)
    return {"skills": skills, "activities": activities,
            "taken_at": firsts[0].taken_at, "source": firsts[0].source}


def carry_forward(db_session, player_id: int, now: datetime, *, pinned: bool = True) -> bool:
    """Write a row at ``now`` repeating the player's newest plugin reading.

    For the moment an event starts or ends: the plugin already has this
    player's exact numbers, so no hiscores request is needed, but the window
    still needs a reading of its own inside it (`first_values_in_window`).
    True if a row was written. The caller commits.
    """
    from db.models import PlayerStatSnapshot

    now = now.replace(microsecond=0)
    latest = (
        _usable(db_session, player_id)
        .filter(PlayerStatSnapshot.source == SOURCE_PLUGIN)
        .order_by(PlayerStatSnapshot.taken_at.desc())
        .first()
    )
    if latest is None or latest.taken_at >= now:
        if latest is not None and pinned and not latest.pinned:
            latest.pinned = True
            db_session.flush()
        return latest is not None
    db_session.add(PlayerStatSnapshot(
        player_id=player_id, taken_at=now, source=SOURCE_PLUGIN,
        account_type=latest.account_type, total_xp=latest.total_xp, total_level=latest.total_level,
        skills=latest.skills, activities=latest.activities, ranks=latest.ranks,
        ehp=latest.ehp, ehb=latest.ehb, flagged=False, pinned=bool(pinned)))
    db_session.flush()
    return True


def efficiency_for(skills: Mapping[str, int], activities: Mapping[str, int],
                   account_varbit: Optional[int] = None) -> Optional[dict]:
    """EHP and EHB for a set of stats (`utils.efficiency`), or None if the
    rate tables cannot be read. ``account_varbit`` is ``player_state.account_type``."""
    try:
        from utils import efficiency
        from utils.account_types import account_type_from_varbit

        return efficiency.player_efficiency(
            efficiency.from_our_skills(skills), activities,
            account_type=account_type_from_varbit(account_varbit))
    except Exception:
        return None
