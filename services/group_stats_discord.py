"""The ``/clan-stats`` Discord card (suggestion #224).

A compact read of the same payload the website's stats page shows: clan
totals for the period, then the leader in each standout category. The full
dissection (every member, skill, boss and item) lives on the website, which
the card links to; a Discord embed can only carry so much.

Pure: :func:`build_embed` takes the payload and returns a plain dict, so it is
unit-tested without a bot.
"""
from __future__ import annotations

from typing import Optional


def format_number(value) -> str:
    """1234 -> "1,234"; 2.4M -> "2.40M"; 3.1B -> "3.10B"."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "0"
    for size, suffix in ((1e9, "B"), (1e6, "M")):
        if abs(n) >= size:
            return f"{n / size:.2f}{suffix}"
    if abs(n) >= 1e4:
        return f"{n / 1e3:.1f}K"
    return f"{int(n):,}"

SITE = "https://www.droptracker.io"
_GOLD = 0xC8A24C
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
# Discord's embed limits we stay well inside.
_FIELD_MAX = 1024
_MAX_STANDOUTS = 12

_SMALL_WORDS = {"of", "the", "and", "a", "in", "on"}
# WOM slugs whose title-cased form is wrong or ugly.
_METRIC_NAMES = {
    "tzkal_zuk": "TzKal-Zuk", "tztok_jad": "TzTok-Jad", "kreearra": "Kree'arra",
    "kril_tsutsaroth": "K'ril Tsutsaroth", "kalphite_queen": "Kalphite Queen",
    "chambers_of_xeric_challenge_mode": "CoX: Challenge Mode",
    "theatre_of_blood_hard_mode": "ToB: Hard Mode",
    "tombs_of_amascut_expert": "ToA: Expert", "tombs_of_amascut": "Tombs of Amascut",
    "chambers_of_xeric": "Chambers of Xeric", "theatre_of_blood": "Theatre of Blood",
    "runecrafting": "Runecraft", "clue_scrolls_all": "Clues",
    "phantom_muspah": "Phantom Muspah", "dagannoth_rex": "Dagannoth Rex",
}


def metric_label(metric: str) -> str:
    if metric in _METRIC_NAMES:
        return _METRIC_NAMES[metric]
    words = str(metric).split("_")
    return " ".join(
        w if (i and w in _SMALL_WORDS) else w.capitalize() for i, w in enumerate(words)
    )


def period_label(period: str) -> str:
    if period == "all":
        return "All time"
    if len(period) == 4:
        return period
    try:
        year, month = period.split("-")
        return f"{_MONTHS[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return period


def stats_url(group_id: int, period: str) -> str:
    return f"{SITE}/groups/{int(group_id)}/stats?period={period}"


def format_value(value, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "gp":
        return f"{format_number(value)} gp"
    if unit == "xp":
        return f"{format_number(value)} xp"
    if unit == "short":
        return format_number(value)
    if unit == "hours":
        return f"{float(value):,.1f} h"
    return f"{int(value):,}"


def _totals_lines(totals: dict) -> list[str]:
    parts = []

    def add(label, key, unit="count"):
        value = totals.get(key)
        if value:
            parts.append(f"**{label}** {format_value(value, unit)}")

    add("Loot", "loot", "gp")
    add("XP", "xp", "short")
    add("Levels", "levels")
    add("Boss kills", "kills")
    add("EHB", "ehb", "hours")
    add("EHP", "ehp", "hours")
    add("Drops", "drops")
    add("Log slots", "clog_slots")
    add("Pets", "pets")
    add("PBs", "pbs")
    add("CAs", "cas")
    add("Slayer tasks", "slayer_tasks")
    add("Clues", "clues")
    add("Quests", "quests")
    add("Deaths", "deaths")
    # Three per line reads as a stat block rather than a paragraph.
    return [" · ".join(parts[i:i + 3]) for i in range(0, len(parts), 3)]


def build_embed(payload: dict) -> Optional[dict]:
    """An embed dict for the payload, or None when there's nothing to say."""
    if not payload:
        return None
    group = payload.get("group") or {}
    names = payload.get("names") or {}
    period = str(payload.get("period") or "")
    totals = payload.get("totals") or {}

    def who(pid) -> str:
        return names.get(str(pid)) or f"Player {pid}"

    lines = []
    active = totals.get("members_active")
    if active is not None:
        lines.append(f"{active:,} of {totals.get('members_total', 0):,} members active"
                     + (" so far" if payload.get("in_progress") else ""))
    lines.extend(_totals_lines(totals))

    fields = []
    standouts = (payload.get("standouts") or {}).get("categories") or []
    rows = []
    for category in standouts[:_MAX_STANDOUTS]:
        entries = category.get("entries") or []
        if not entries:
            continue
        top = entries[0]
        detail = f" ({top['detail']})" if top.get("detail") else ""
        rows.append(f"**{category['label']}**: {who(top['player_id'])}, "
                    f"{format_value(top['value'], category.get('unit', 'count'))}{detail}")
    if rows:
        value = ""
        for row in rows:
            if len(value) + len(row) + 1 > _FIELD_MAX:
                break
            value += row + "\n"
        fields.append({"name": "Standouts", "value": value.strip(), "inline": False})

    skills = (payload.get("standouts") or {}).get("skills") or []
    if skills:
        value = "\n".join(
            f"**{metric_label(s['metric'])}**: {who(s['entries'][0]['player_id'])}, "
            f"{format_value(s['entries'][0]['value'], 'xp')}"
            for s in skills[:5] if s.get("entries")
        )
        if value:
            fields.append({"name": "Top skills", "value": value[:_FIELD_MAX], "inline": True})
    bosses = (payload.get("standouts") or {}).get("bosses") or []
    if bosses:
        value = "\n".join(
            f"**{metric_label(b['metric'])}**: {who(b['entries'][0]['player_id'])}, "
            f"{int(b['entries'][0]['value']):,} kc"
            for b in bosses[:5] if b.get("entries")
        )
        if value:
            fields.append({"name": "Top bosses", "value": value[:_FIELD_MAX], "inline": True})

    if not lines and not fields:
        return None
    footer = "Full breakdown on the website"
    wom = (payload.get("coverage") or {}).get("wom")
    if wom in ("missing", "unlinked"):
        footer += " · XP and kill counts need a Wise Old Man group"
    return {
        "title": f"{group.get('name') or 'Clan'} · {period_label(period)}",
        "url": stats_url(group.get("id") or 0, period),
        "description": "\n".join(lines)[:4000],
        "color": _GOLD,
        "fields": fields,
        "footer": {"text": footer},
    }
