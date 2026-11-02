"""Recap designs: what a clan's monthly recap card shows (suggestion #224).

A design is a small JSON document an admin builds in the website's recap
designer. The snapshot (``recap_snapshots``) holds every number the card
*could* show; the design decides which of them it *does*, in what order and
in which column, how many rows each list gets, the accent colour, and an
optional message from the clan. Because the design is applied when the card
is drawn, editing it changes the card without recomputing anything, and an
edit after posting can be pushed to the Discord message
(``services.recap_delivery.refresh_posted_card``).

Storage is ``recap_designs``: ``period = ''`` is the clan's default for every
month, a ``'YYYY-MM'`` row overrides it for that month only. :func:`resolve`
picks the effective one.

:func:`normalize_design` is the one validator. Everything stored has been
through it, and anything it doesn't recognise is dropped rather than refused,
so an older client can't make a design unsaveable and a newer field never
breaks an older reader.

The TypeScript mirror is ``packages/api-types/src/recap-design.ts`` (keys,
limits, defaults). ``tests/unit/test_recap_design.py`` pins the defaults.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import text

DESIGN_VERSION = 1
DEFAULT_PERIOD = ""

# Every panel the card can draw: (key, default column, on by default).
# "drop" is the full-width Drop-of-the-month strip under the body and has no
# column. The defaults reproduce the card as it was before designs existed.
SECTIONS = (
    ("gallery", "main", True),
    ("sources", "main", True),
    ("skills", "main", False),
    ("bosses", "main", False),
    ("note", "main", False),
    ("members", "side", True),
    ("standouts", "side", True),
    ("milestones", "side", True),
    ("pets", "side", False),
    ("drop", None, True),
)
SECTION_KEYS = tuple(k for k, _, _ in SECTIONS)
COLUMNS = ("main", "side")
LAYOUTS = ("auto", "columns", "stacked")

# Mirrors services.group_stats.STANDOUTS (asserted equal in the tests).
STANDOUT_KEYS = (
    "loot", "biggest_drop", "xp", "levels", "ehb", "ehp", "kills", "drops",
    "clog_slots", "pets", "pbs", "cas", "clues", "slayer_tasks", "quests",
    "diaries", "pvp_kills", "deaths",
)
# The five the card showed before designs existed.
DEFAULT_STANDOUTS = ("pbs", "clog_slots", "cas", "pets", "deaths")
MAX_STANDOUTS = 10

# The small stat plaques beside the total. Empty = the card's own choice.
HERO_STATS = (
    "drops", "unique_items", "rank", "members", "ehb", "xp", "levels", "kills",
    "ehp", "prime_time", "sources", "clog_slots", "pets", "pbs", "cas",
)
MAX_HERO_STATS = 4

# Row counts per list: (min, max, default).
COUNTS = {
    "gallery": (4, 10, 10),
    "members": (3, 10, 5),
    "sources": (3, 10, 5),
    "skills": (3, 8, 6),
    "bosses": (3, 8, 6),
    "pets": (3, 12, 8),
}

ACCENTS = ("gold", "ember", "frost", "verdant", "amethyst", "silver")

TITLE_MAX = 60
HEADLINE_MAX = 90
NOTE_TITLE_MAX = 40
NOTE_BODY_MAX = 400
MAX_HIDDEN_PLAYERS = 50


def default_design() -> dict:
    return {
        "version": DESIGN_VERSION,
        "layout": "auto",
        "accent": "gold",
        "sections": [
            {"key": key, "enabled": enabled, **({"column": column} if column else {})}
            for key, column, enabled in SECTIONS
        ],
        "standouts": list(DEFAULT_STANDOUTS),
        "standouts_depth": 1,
        "hero_stats": [],
        "counts": {key: spec[2] for key, spec in COUNTS.items()},
        "title": "",
        "headline": "",
        "note": {"title": "", "body": ""},
        "hidden_players": [],
    }


def _clean_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Single spaces, no control characters; newlines only where a body allows.
    cleaned = "".join(ch for ch in value if ch == "\n" or ch >= " ")
    return cleaned.strip()[:limit]


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _unique(values, allowed, limit: int) -> list:
    out = []
    for value in values if isinstance(values, (list, tuple)) else ():
        if isinstance(value, str) and value in allowed and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def normalize_design(raw) -> dict:
    """A valid, complete design from whatever was sent.

    Unknown keys are dropped, out-of-range numbers clamped, text trimmed to its
    limit, and a section missing from the list is appended with its defaults,
    so a design saved before a panel existed still shows that panel in the
    designer (switched off unless it is on by default).
    """
    base = default_design()
    if not isinstance(raw, dict):
        return base
    out = dict(base)

    layout = raw.get("layout")
    out["layout"] = layout if layout in LAYOUTS else "auto"
    accent = raw.get("accent")
    out["accent"] = accent if accent in ACCENTS else "gold"

    defaults = {key: (column, enabled) for key, column, enabled in SECTIONS}
    sections = []
    seen = set()
    for entry in raw.get("sections") if isinstance(raw.get("sections"), list) else ():
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if key not in defaults or key in seen:
            continue
        seen.add(key)
        column, enabled = defaults[key]
        section = {"key": key, "enabled": bool(entry.get("enabled", enabled))}
        if column is not None:
            section["column"] = entry.get("column") if entry.get("column") in COLUMNS else column
        sections.append(section)
    for key, column, enabled in SECTIONS:
        if key not in seen:
            sections.append({"key": key, "enabled": enabled, **({"column": column} if column else {})})
    out["sections"] = sections

    if "standouts" in raw:
        out["standouts"] = _unique(raw.get("standouts"), STANDOUT_KEYS, MAX_STANDOUTS)
    out["standouts_depth"] = _clamp(raw.get("standouts_depth"), 1, 3, 1)
    out["hero_stats"] = _unique(raw.get("hero_stats"), HERO_STATS, MAX_HERO_STATS)

    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    out["counts"] = {
        key: _clamp(counts.get(key), lo, hi, default)
        for key, (lo, hi, default) in COUNTS.items()
    }

    out["title"] = _clean_text(raw.get("title"), TITLE_MAX).replace("\n", " ")
    out["headline"] = _clean_text(raw.get("headline"), HEADLINE_MAX).replace("\n", " ")
    note = raw.get("note") if isinstance(raw.get("note"), dict) else {}
    out["note"] = {
        "title": _clean_text(note.get("title"), NOTE_TITLE_MAX).replace("\n", " "),
        "body": _clean_text(note.get("body"), NOTE_BODY_MAX),
    }

    hidden = []
    for pid in raw.get("hidden_players") if isinstance(raw.get("hidden_players"), list) else ():
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid not in hidden:
            hidden.append(pid)
        if len(hidden) >= MAX_HIDDEN_PLAYERS:
            break
    out["hidden_players"] = hidden
    return out


def is_valid_period(period: str) -> bool:
    return period == DEFAULT_PERIOD or (
        isinstance(period, str) and len(period) == 7 and period[4] == "-"
        and period[:4].isdigit() and period[5:].isdigit() and 1 <= int(period[5:]) <= 12
    )


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _row(session, group_id: int, period: str):
    return session.execute(
        text(
            "SELECT design, updated_at, updated_by, approved_at, approved_by "
            "FROM recap_designs WHERE group_id = :gid AND period = :period"
        ),
        {"gid": int(group_id), "period": period},
    ).first()


def _parse(raw) -> Optional[dict]:
    try:
        return normalize_design(json.loads(raw)) if raw else None
    except (TypeError, ValueError):
        return None


def resolve(session, group_id: int, period: Optional[str]) -> dict:
    """The effective design for a clan's month and where it came from.

    ``{"design", "source": "month"|"default"|"builtin", "updated_at",
    "approved_at", "approved_by", "has_month", "has_default"}``. Never raises:
    a design that can't be read (or a table that doesn't exist yet) falls back
    to the built-in default, which is the card as it always was.
    """
    out = {
        "design": default_design(), "source": "builtin", "updated_at": None,
        "approved_at": None, "approved_by": None, "has_month": False,
        "has_default": False,
    }
    try:
        month = _row(session, group_id, period) if period and period != DEFAULT_PERIOD else None
        clan = _row(session, group_id, DEFAULT_PERIOD)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return out
    if month is not None:
        out["approved_at"] = month[3].isoformat() if month[3] else None
        out["approved_by"] = month[4]
    month_design = _parse(month[0]) if month is not None else None
    clan_design = _parse(clan[0]) if clan is not None else None
    out["has_month"] = month_design is not None
    out["has_default"] = clan_design is not None
    if month_design is not None:
        out.update(design=month_design, source="month",
                   updated_at=month[1].isoformat() if month[1] else None)
    elif clan_design is not None:
        out.update(design=clan_design, source="default",
                   updated_at=clan[1].isoformat() if clan[1] else None)
    return out


def design_stamp(session, group_id: int, period: str) -> str:
    """A string that changes whenever the effective design changes — folded
    into the rendered PNG's cache key so an edit re-renders the card."""
    resolved = resolve(session, group_id, period)
    return f"{resolved['source']}:{resolved['updated_at'] or ''}"


def save(session, group_id: int, period: str, design: dict, user_id: Optional[int]) -> dict:
    """Upsert one design row (normalised). Keeps an existing approval."""
    if not is_valid_period(period):
        raise ValueError(f"bad design period: {period!r}")
    blob = json.dumps(normalize_design(design), separators=(",", ":"))
    session.execute(
        text(
            "INSERT INTO recap_designs (group_id, period, design, updated_by, updated_at) "
            "VALUES (:gid, :period, :design, :uid, :now) "
            "ON DUPLICATE KEY UPDATE design = VALUES(design), "
            "  updated_by = VALUES(updated_by), updated_at = VALUES(updated_at)"
        ),
        {"gid": int(group_id), "period": period, "design": blob,
         "uid": user_id, "now": datetime.now().replace(microsecond=0)},
    )
    return resolve(session, group_id, period or None)


def delete(session, group_id: int, period: str) -> None:
    """Remove one row: a month falls back to the clan default, the default to
    the built-in one. A month row carrying an approval keeps the approval (the
    design column resets to the default it now follows)."""
    if period == DEFAULT_PERIOD:
        session.execute(
            text("DELETE FROM recap_designs WHERE group_id = :gid AND period = ''"),
            {"gid": int(group_id)},
        )
        return
    session.execute(
        text(
            "DELETE FROM recap_designs WHERE group_id = :gid AND period = :period "
            "AND approved_at IS NULL"
        ),
        {"gid": int(group_id), "period": period},
    )
    session.execute(
        text(
            "UPDATE recap_designs SET design = '' WHERE group_id = :gid AND period = :period"
        ),
        {"gid": int(group_id), "period": period},
    )


def set_approval(session, group_id: int, period: str, user_id: Optional[int],
                 approved: bool = True) -> None:
    """Record (or withdraw) an admin's approval of one month's card. Creates the
    month row if needed, with an empty design so it keeps following the
    clan default."""
    now = datetime.now().replace(microsecond=0)
    session.execute(
        text(
            "INSERT INTO recap_designs (group_id, period, design, updated_at, approved_at, approved_by) "
            "VALUES (:gid, :period, '', :now, :at, :uid) "
            "ON DUPLICATE KEY UPDATE approved_at = VALUES(approved_at), "
            "  approved_by = VALUES(approved_by)"
        ),
        {"gid": int(group_id), "period": period, "now": now,
         "at": now if approved else None, "uid": user_id if approved else None},
    )


def approved_group_ids(session, period: str) -> set:
    """Clans whose card for ``period`` an admin has approved."""
    try:
        rows = session.execute(
            text(
                "SELECT group_id FROM recap_designs "
                "WHERE period = :period AND approved_at IS NOT NULL"
            ),
            {"period": period},
        ).fetchall()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return set()
    return {int(r[0]) for r in rows}
