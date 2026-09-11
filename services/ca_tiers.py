"""Combat Achievement tier thresholds, and the progress maths built on them.

The CA notification renders two fields off the player's running point total
(varbit 14815, sent by the plugin as ``total_points``)::

    Current tier            {current_tier} ({total_points} pts)
    Progress to {next_tier} `{progress}%` ({points_left} pts)

Both need the per-tier point thresholds, which Jagex raises every time a batch
of tasks ships, so they are read from the wiki's ``Globals`` template rather
than pinned in code.

That lookup used to happen inline in the notification handler with no cache, no
timeout and no sane failure path: two fresh API clients per notification, seven
wiki requests each, and if the requests came back empty the handler fell through
to ``next_tier_points = 38`` (a long-stale Easy threshold) while the tier itself
degraded to ``None``. A player sitting just short of Grandmaster was then told
they were "-2,412 pts" from **Easy**. So:

* thresholds are fetched once per process and cached for ``_TTL_SECONDS``;
* a fetch is only accepted if all six tiers parsed *and* ascend — a partial
  answer mis-ranks the player just as badly as no answer;
* anything else falls back to `FALLBACK_TIER_POINTS`, which is stale by at most
  one game update instead of being nonsense;
* the maths is a pure function, so the edge cases (below Easy, Grandmaster
  already done, total unknown) are testable without touching the network.

The Data API's ``combat_achievements`` section serves the same maths over each
player's stored total (``player_ca_varps.points``, see ``db/ca_points.py``) as
typed fields — `ca_tier_summary` — and reads the thresholds through
`tier_thresholds_nowait`, so an API request never waits on the wiki.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Mapping, Optional

CA_TIER_ORDER = ["Easy", "Medium", "Hard", "Elite", "Master", "Grandmaster"]

# The wiki's Globals variable behind each tier's cumulative point requirement.
WIKI_GLOBALS = {
    "Easy": "ca easy points",
    "Medium": "ca medium points",
    "Hard": "ca hard points",
    "Elite": "ca elite points",
    "Master": "ca master points",
    "Grandmaster": "ca gm points",
}

# Read off the wiki on 2026-09-10, after the 2026-08-26 batch (4 Medium, 3
# Hard and 2 Elite tasks) moved every threshold above Easy. Only reached when
# the live lookup fails, or before it has answered once in this process (the
# Data API serves these rather than wait on the wiki) — refresh it whenever a
# CA batch ships.
FALLBACK_TIER_POINTS: Dict[str, int] = {
    "Easy": 41,
    "Medium": 169,
    "Hard": 436,
    "Elite": 1100,
    "Master": 1965,
    "Grandmaster": 2697,
}

# Shown wherever a number would otherwise be invented. Web/Discord manual CA
# submissions have no varbit to read, so webhook.py sends total_points=0; the
# player has *some* tier, we just don't know it, and "None (0 pts)" reads as a
# statement about their account rather than about our data.
UNKNOWN_TIER = "Unknown"
UNKNOWN_NEXT_TIER = "next tier"
UNKNOWN_VALUE = "?"

# Below Easy the player genuinely has no tier; osrs_wiki.py already spells that
# "None", so keep the same word in front of users.
NO_TIER = "None"

_TTL_SECONDS = 6 * 60 * 60
_RETRY_SECONDS = 10 * 60
_FETCH_TIMEOUT = 15

_cache: Optional[Dict[str, int]] = None
_cached_at: float = 0.0
_last_attempt: Optional[float] = None
_refresh_task: Optional["asyncio.Task"] = None


def parse_threshold(raw: Any) -> Optional[int]:
    """Coerce one Globals value (``"2,672\\n"``) to a positive int, else None."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("&nbsp;", "").replace(" ", "")
    if not text:
        return None
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def build_threshold_table(raw_values: Mapping[str, Any]) -> Optional[Dict[str, int]]:
    """All six tiers, parsed and strictly ascending — or None for the caller to
    fall back on.

    Partial success is rejected deliberately: the old code kept whatever subset
    came back, so three good tiers and three blanks silently ranked a
    Grandmaster player as unranked instead of admitting the lookup failed.
    """
    table: Dict[str, int] = {}
    for tier in CA_TIER_ORDER:
        value = parse_threshold(raw_values.get(tier))
        if value is None:
            return None
        table[tier] = value
    ordered = [table[tier] for tier in CA_TIER_ORDER]
    if any(nxt <= prev for prev, nxt in zip(ordered, ordered[1:])):
        return None
    return table


async def _fetch_from_wiki(semantic=None) -> Optional[Dict[str, int]]:
    """Six Globals lookups. Returns a validated table, or None."""
    client = None
    if semantic is None:
        import osrs_api

        client = osrs_api.create_client()
        semantic = client.semantic
    try:
        raw = {}
        for tier, variable in WIKI_GLOBALS.items():
            raw[tier] = await semantic.get_global_value(variable)
        return build_threshold_table(raw)
    finally:
        if client is not None:
            await client.close()


def _needs_fetch(now: float) -> bool:
    """Whether the wiki should be asked: the cache is stale and no recent
    failure has backed us off."""
    if _cache is not None and now - _cached_at < _TTL_SECONDS:
        return False
    if _last_attempt is not None and now - _last_attempt < _RETRY_SECONDS:
        return False
    return True


async def get_tier_thresholds(semantic=None) -> Dict[str, int]:
    """Cumulative points per tier, cached process-wide.

    Never raises and never returns a partial table: on any failure the caller
    gets the last good table, or `FALLBACK_TIER_POINTS`.
    """
    global _cache, _cached_at, _last_attempt

    now = time.monotonic()
    if not _needs_fetch(now):
        # Either fresh, or a failure just backed us off; don't re-hammer the
        # wiki per notification.
        return _cache if _cache is not None else dict(FALLBACK_TIER_POINTS)

    _last_attempt = now
    table = None
    try:
        table = await asyncio.wait_for(_fetch_from_wiki(semantic), timeout=_FETCH_TIMEOUT)
    except Exception as e:
        print(f"[CA tiers] wiki threshold lookup failed ({e}); using cached/fallback tiers")

    if table is None:
        return _cache if _cache is not None else dict(FALLBACK_TIER_POINTS)

    _cache = table
    _cached_at = now
    return table


def current_tier_thresholds() -> Dict[str, int]:
    """The best table this process holds right now, without asking the wiki."""
    return dict(_cache) if _cache is not None else dict(FALLBACK_TIER_POINTS)


def tier_thresholds_nowait() -> Dict[str, int]:
    """`current_tier_thresholds`, refreshing a stale cache in the background.

    For request paths that must answer now. Awaiting `get_tier_thresholds`
    would make one Data API request every six hours wait on six wiki lookups,
    and every request for up to `_FETCH_TIMEOUT` while the wiki is down; the
    pinned table is right about the tier in the meantime. The refresh needs a
    running event loop, so called from anywhere else this only returns what the
    process already has.
    """
    global _refresh_task
    if _needs_fetch(time.monotonic()) and (_refresh_task is None or _refresh_task.done()):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # Held so the task is not collected mid-flight; get_tier_thresholds
            # never raises, so nothing is left unobserved.
            _refresh_task = loop.create_task(get_tier_thresholds())
    return current_tier_thresholds()


def reset_cache() -> None:
    """Drop the cached thresholds (tests, and anything that wants a refetch)."""
    global _cache, _cached_at, _last_attempt, _refresh_task
    _cache = None
    _cached_at = 0.0
    _last_attempt = None
    _refresh_task = None


def _format_percent(value: float) -> str:
    """69.53 -> "69.53", 100.0 -> "100" (templates append their own '%')."""
    rounded = round(value, 2)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)


def ca_progress(points_total: Any, thresholds: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """Everything the CA notification's tier fields need, in display form.

    ``points_total`` is the player's cumulative CA points. Anything missing or
    non-positive is treated as unknown rather than as zero — see UNKNOWN_TIER.
    """
    table = dict(thresholds) if thresholds else dict(FALLBACK_TIER_POINTS)

    try:
        points = int(points_total)
    except (TypeError, ValueError):
        points = 0

    if points <= 0:
        return {
            "known": False,
            "current_tier": UNKNOWN_TIER,
            "next_tier": UNKNOWN_NEXT_TIER,
            "next_tier_points": UNKNOWN_VALUE,
            "points_left": UNKNOWN_VALUE,
            "progress": UNKNOWN_VALUE,
            "total_points": UNKNOWN_VALUE,
        }

    current_tier, current_tier_points, next_tier, next_tier_points = _tier_position(points, table)

    if next_tier is None:
        # Grandmaster is done. The old code reached for tier_order[index - 1] on
        # a descending list, which wrapped past the end and announced "next
        # tier: Easy" to the most decorated players on the board.
        grandmaster_points = table.get("Grandmaster") or current_tier_points or points
        return {
            "known": True,
            "current_tier": current_tier or CA_TIER_ORDER[-1],
            "next_tier": CA_TIER_ORDER[-1],
            "next_tier_points": grandmaster_points,
            "points_left": 0,
            "progress": "100",
            "total_points": points,
        }

    return {
        "known": True,
        "current_tier": current_tier or NO_TIER,
        "next_tier": next_tier,
        "next_tier_points": next_tier_points,
        "points_left": max(0, next_tier_points - points),
        "progress": _format_percent(
            _span_progress(points, current_tier_points, next_tier_points)
        ),
        "total_points": points,
    }


def ca_tier_summary(points_total: Any,
                    thresholds: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """The tier fields of an API response: typed, and None where unknown.

    The same maths as `ca_progress`, shaped for programs rather than a Discord
    template — numbers where that returns display strings, None where it
    returns "?". One difference is deliberate: a total of 0 is a real answer
    here. The notification only ever sees 0 from a manual submission that
    could not read the varbit, but a stored 0 is an account with no tasks done,
    which has no tier yet and is `Easy` points from its first.

    ``progress`` is the percentage of the way from the current tier's
    threshold to the next one's, as the notification shows it.
    """
    unknown = {
        "tier": None,
        "next_tier": None,
        "next_tier_points": None,
        "points_to_next": None,
        "progress": None,
    }
    if points_total is None or isinstance(points_total, bool):
        return unknown
    try:
        points = int(points_total)
    except (TypeError, ValueError):
        return unknown
    if points < 0:
        return unknown

    table = dict(thresholds) if thresholds else dict(FALLBACK_TIER_POINTS)
    current_tier, current_tier_points, next_tier, next_tier_points = _tier_position(points, table)

    if next_tier is None:
        return {
            "tier": current_tier or CA_TIER_ORDER[-1],
            "next_tier": None,
            "next_tier_points": None,
            "points_to_next": 0,
            "progress": 100.0,
        }

    return {
        "tier": current_tier,
        "next_tier": next_tier,
        "next_tier_points": next_tier_points,
        "points_to_next": max(0, next_tier_points - points),
        "progress": round(_span_progress(points, current_tier_points, next_tier_points), 2),
    }


def _tier_position(points: int, table: Mapping[str, int]):
    """``(current tier, its threshold, next tier, its threshold)``.

    The current tier is None below Easy (threshold 0); the next tier is None
    once Grandmaster is reached.
    """
    current_tier = None
    current_tier_points = 0
    next_tier = None
    next_tier_points = 0
    for tier in CA_TIER_ORDER:
        tier_points = table.get(tier)
        if not tier_points:
            continue
        if points >= tier_points:
            current_tier = tier
            current_tier_points = tier_points
        elif next_tier is None:
            next_tier = tier
            next_tier_points = tier_points
    return current_tier, current_tier_points, next_tier, next_tier_points


def _span_progress(points: int, current_tier_points: int, next_tier_points: int) -> float:
    """How far through the current tier's span, as a percentage in 0..100."""
    span = next_tier_points - current_tier_points
    progress = 100.0 if span <= 0 else ((points - current_tier_points) / span) * 100
    return max(0.0, min(100.0, progress))
