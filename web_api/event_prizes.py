"""Prize-pot configuration for events v2 (web52a).

An event can enable a **prize pot** (``web_events.buyins_enabled`` +
``web_events.prize_config``): participants record GP **buy-ins** and
**donations** (``web_event_buyins``) that sum into an advertised pot, and clan
leaders configure who is advertised as taking it and whether/where it is
posted on Discord. The tool tracks/advertises GP only — payouts are traded
in-game by the clan (like split-tracking); nothing here moves real GP.

Lives in web_api (not services/) for the same reason as
:mod:`web_api.event_leadership`: route modules import it directly, and the
unit-test conftest stubs the whole ``services`` package, so a pure config
helper must not sit under ``services``. Everything here is stdlib-only and
unit-testable; db imports (if ever needed) stay function-local.
"""
from __future__ import annotations

import json
from typing import Optional

# Re-declared locally (not imported from db.models) so this module stays
# stdlib-only and importable under the services-stubbing unit-test conftest —
# the exact idiom event_leadership.py uses for LEADER_SELECTION_MODES. Keep in
# sync with db.models.events.EVENT_PRIZE_DISTRIBUTIONS.
PRIZE_DISTRIBUTIONS = ("first_only", "top_n", "custom_split")

# GP amounts are clamped to this half-open range: 0 <= amount < 10^15. A pot
# far below signed-BIGINT's ceiling, but comfortably above any real OSRS pot,
# so a fat-fingered value can't poison the ledger. Routes import this cap.
MAX_BUYIN_AMOUNT = 10 ** 15

DEFAULT_PRIZE_CONFIG = {
    "default_buyin": 0,           # expected buy-in per participant (GP); 0 = no fixed stake
    "distribution": "first_only",  # PRIZE_DISTRIBUTIONS
    "top_n": 1,                   # clamped 1..team_count at read (top_n distribution)
    "splits": [100],              # percentages by place (custom_split); must sum to 100
    "advertise": False,           # post the pot on the Discord board + started/ended lines
    "show_contributors": True,    # list RSN+amounts publicly, vs a total-only headline
    "allow_leader_mark": False,   # team leaders may tick their OWN team's buy-ins
}


def _clean_splits(value) -> Optional[list]:
    """A custom-split percentage list, or None when invalid. Must be a
    non-empty list of positive integers summing to exactly 100."""
    if not isinstance(value, list) or not value:
        return None
    out = []
    for v in value:
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            return None
        out.append(v)
    if sum(out) != 100:
        return None
    return out


def effective_prize_config(raw_json, team_count: Optional[int] = None) -> dict:
    """Full prize config for one event: defaults overlaid with the stored
    ``web_events.prize_config`` JSON. Corrupt/unknown data is ignored — callers
    always get every key of :data:`DEFAULT_PRIZE_CONFIG` back.

    ``team_count`` (the live number of teams) clamps ``top_n`` to ``1..count``
    at read, so a config authored against more teams than remain can never
    advertise a bigger split than exists. Invalid ``splits`` fall back to the
    default ``[100]``."""
    config = dict(DEFAULT_PRIZE_CONFIG)
    data = raw_json
    if data and not isinstance(data, dict):
        try:
            data = json.loads(raw_json)
        except (ValueError, TypeError):
            return _clamp_top_n(config, team_count)
    if not isinstance(data, dict):
        return _clamp_top_n(config, team_count)

    db = data.get("default_buyin")
    if isinstance(db, int) and not isinstance(db, bool) and db >= 0:
        config["default_buyin"] = min(db, MAX_BUYIN_AMOUNT - 1)
    if data.get("distribution") in PRIZE_DISTRIBUTIONS:
        config["distribution"] = data["distribution"]
    tn = data.get("top_n")
    if isinstance(tn, int) and not isinstance(tn, bool) and tn >= 1:
        config["top_n"] = tn
    splits = _clean_splits(data.get("splits"))
    if splits is not None:
        config["splits"] = splits
    for key in ("advertise", "show_contributors", "allow_leader_mark"):
        if key in data:
            config[key] = bool(data[key])
    return _clamp_top_n(config, team_count)


def _clamp_top_n(config: dict, team_count: Optional[int]) -> dict:
    """Clamp ``top_n`` to ``1..team_count`` (a positive lower bound always;
    the team-count upper bound only when a count is supplied)."""
    tn = config.get("top_n", 1)
    tn = max(1, tn)
    if team_count is not None:
        tn = min(tn, max(1, team_count))
    config["top_n"] = tn
    return config


def pot_summary(session, ev, team_count: Optional[int] = None) -> dict:
    """Lightweight pot block for the event-detail / team reads — the headline
    figure that rides the existing event-detail SSE refresh, so the standings
    banner updates live without a second fetch. The full contributor list is
    the on-demand ``GET /events/{id}/pot``.

    Returns raw ints (callers wrap them in the money envelope). Only ``paid``
    rows count. Short-circuits when the pot is disabled — a disabled event
    hides the pot, so the public detail read skips the buy-ins query entirely
    (the admin Prize-Pot tab still sees every record via the full pot read,
    which never short-circuits)."""
    cfg = effective_prize_config(getattr(ev, "prize_config", None), team_count=team_count)
    if not getattr(ev, "buyins_enabled", False):
        return {
            "enabled": False,
            "total": 0,
            "advertise": False,
            "distribution": cfg["distribution"],
            "top_n": cfg["top_n"],
            "per_team": {},
        }
    from db.models import EventBuyin  # function-local (see module docstring)

    rows = (
        session.query(EventBuyin.team_id, EventBuyin.amount)
        .filter(EventBuyin.event_id == ev.id, EventBuyin.status == "paid")
        .all()
    )
    total = 0
    per_team: dict = {}
    for team_id, amount in rows:
        amt = int(amount or 0)
        total += amt
        if team_id is not None:
            per_team[team_id] = per_team.get(team_id, 0) + amt
    return {
        "enabled": True,
        "total": total,
        "advertise": bool(cfg["advertise"]),
        "distribution": cfg["distribution"],
        "top_n": cfg["top_n"],
        "per_team": per_team,
    }


def pot_line(total_formatted: str, distribution: str, top_n: int, *,
             ended: bool = False, winner: Optional[str] = None) -> str:
    """One-line pot advertisement for a Discord board/lifecycle message.
    ``total_formatted`` is already GP-abbreviated by the caller (format_gp),
    keeping this module free of any bot/formatting imports. web52a."""
    if ended and distribution == "first_only" and winner:
        return f"\U0001F3C6 **{winner}** takes the **{total_formatted}** pot"
    if distribution == "top_n" and top_n > 1:
        tail = f"top {top_n} teams split it"
    elif distribution == "custom_split":
        tail = "split among the top teams"
    else:
        tail = "winner takes all"
    verb = "Final prize pot" if ended else "Prize pot"
    return f"\U0001F4B0 {verb}: **{total_formatted}** — {tail}"


def normalize_prize_input(body) -> Optional[dict]:
    """Validate a PATCH payload's ``prize_config`` object into the stored JSON
    shape, or None when invalid. Accepts partial objects (missing keys keep
    their defaults on read) — mirrors ``normalize_leadership_input``."""
    if not isinstance(body, dict):
        return None
    out: dict = {}
    if "default_buyin" in body:
        val = body["default_buyin"]
        if not isinstance(val, int) or isinstance(val, bool) or not (0 <= val < MAX_BUYIN_AMOUNT):
            return None
        out["default_buyin"] = val
    if "distribution" in body:
        if body["distribution"] not in PRIZE_DISTRIBUTIONS:
            return None
        out["distribution"] = body["distribution"]
    if "top_n" in body:
        val = body["top_n"]
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            return None
        out["top_n"] = val
    if "splits" in body:
        splits = _clean_splits(body["splits"])
        if splits is None:
            return None
        out["splits"] = splits
    for key in ("advertise", "show_contributors", "allow_leader_mark"):
        if key in body:
            if not isinstance(body[key], bool):
                return None
            out[key] = body[key]
    return out
