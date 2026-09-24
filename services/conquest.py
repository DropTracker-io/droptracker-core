"""Conquest (the ``conquest`` event kind) — the pure rules core.

Conquest is a territory game inspired by the board game Risk and by the
territory events clans used to run by hand. The board is a map split into
**regions**; each region holds **tiles**, and every tile is a boss or an
activity. Teams take and hold tiles by playing them:

- A tile carries one or more **rules**, each an ordinary event task ("35
  Zulrah kills", "any Zulrah unique"). Every time a team's running progress on
  a rule crosses another multiple of the task's target, the team earns that
  rule's **troops** on the tile (:func:`troops_for_progress`). Tasks never
  "complete" in this kind — they keep paying troops for the whole event.
- A troop resolves immediately (:func:`resolve_troop`):

  * on an unowned tile with no defense it **claims** the tile;
  * on the team's own tile it adds one **defense** (up to ``max_defense``);
  * on a rival's tile with no defense left it **captures** the tile;
  * on a defended rival (or a neutral garrison) it **attacks**: the attacker
    rolls ``attack_dice`` d6, the defender ``min(defense, defense_dice)`` d6,
    the highest dice are compared in pairs, ties go to the defender, and each
    pair the attacker wins knocks one point of defense off. Knocking the last
    point off is a **breach**: the tile falls to the NEXT enemy troop, which
    gives the owner a window to reinforce. ``battle_mode = "attrition"``
    swaps the dice for a flat one point per troop (no luck at all).

  The troop that takes a tile stays to guard it (``capture_defense``), so a
  tile cannot flip straight back and forth.
- Holding every tile of a region is **region control**, worth the region's
  bonus.

Scoring (:func:`compute_standings`) has two modes:

- ``hold_time`` (default): a tile pays its ``value`` in points for every hour
  a team holds it, and a region its ``bonus`` per hour. Points are
  proportional to time held, so taking a tile just before a daily cut-off
  earns minutes of points, not a day's worth. Scores are derived from the
  ownership history (:class:`Hold` intervals), never accumulated
  incrementally, so a recompute is idempotent.
- ``final``: only the map at the end counts. Standings during the event show
  the result "if it ended now".

Why troops come from tasks: the task engine already matches drops, kills, XP,
pets and so on, folds absolute kill counts across the plugin and WiseOldMan,
dedupes echoes and runs the manual-review policy. A tile is just a set of
tasks with a troop yield, so every submission path works unchanged. The map
generator sizes each tile's kill target from EHB kill rates so one troop costs
roughly the same time at every boss.

This module is PURE: stdlib only, no DB, Redis or clock reads (callers pass
``now`` and an ``rng``), so the unit tests load it by path. The DB side lives
in :mod:`services.conquest_engine`.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
SCORING_MODES = ("hold_time", "final")
BATTLE_MODES = ("dice", "attrition")
# How tiles are owned when the event starts: all unowned (a land grab) or
# dealt out evenly between the teams, like the opening of a game of Risk.
START_MODES = ("neutral", "dealt")
# "respawn" tiles can never be owned. They are placeholders for the fronts
# rule (attack only next to your own tiles), where a team that lost every tile
# re-enters the map next to one. They carry no rules and never count for a
# region.
TILE_KINDS = ("normal", "respawn")
# Hours between the periodic "map update" Discord post (0 = never).
SUMMARY_HOURS_CHOICES = (0, 6, 12, 24, 48)
DICE_SIDES = 6

# What a troop did (web_conquest_battles.outcome). "adjust" is an admin
# correction (set owner / defense by hand), never a troop.
OUTCOMES = ("claim", "capture", "fortify", "full", "attack", "breach", "repelled",
            "adjust")
CAPTURE_OUTCOMES = ("claim", "capture")

# Task types a tile rule may use: the ADDITIVE ones, where every qualifying
# submission adds to a running count, so "every N" is well defined. Distinct-
# item sets (all_of / assembly / any_of_distinct), grouped and any_path lists,
# PB times and level targets have no meaningful "the Nth time".
RULE_TASK_TYPES = (
    "kc_target", "item_collection", "xp_target", "loot_value",
    "pet_collection", "ca_target", "slayer_target", "custom",
)
# item_collection list kinds that stay additive (None = a single item).
RULE_ITEM_LIST_KINDS = (None, "any_of", "point_collection")

# Bounds shared by the write validator and the designer.
MAX_REGIONS = 24
MAX_TILES = 120
MAX_RULES_PER_TILE = 4
MAX_TROOPS_PER_RULE = 10
MAX_TILE_VALUE = 100
MAX_REGION_BONUS = 1000
MAX_LABEL_LEN = 80
MAX_REGION_NAME_LEN = 60

DEFAULT_SETTINGS = {
    "scoring_mode": "hold_time",
    "summary_hours": 24,
    "battle_mode": "dice",
    "attack_dice": 2,
    "defense_dice": 2,
    "max_defense": 5,
    "capture_defense": 1,
    "start_mode": "neutral",
    "start_defense": 1,
    "neutral_defense": 0,
}

# Inclusive integer bounds per numeric setting.
SETTING_BOUNDS = {
    "attack_dice": (1, 3),
    "defense_dice": (1, 3),
    "max_defense": (1, 20),
    "capture_defense": (0, 20),
    "start_defense": (0, 20),
    "neutral_defense": (0, 20),
}
_ENUM_SETTINGS = {
    "scoring_mode": SCORING_MODES,
    "battle_mode": BATTLE_MODES,
    "start_mode": START_MODES,
}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)) and raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _int_or(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def conquest_settings(raw) -> dict:
    """The effective settings for one map: defaults overlaid with the stored
    JSON (a dict or a JSON string). Unknown keys are dropped, bad values fall
    back to the default and numbers are clamped — a corrupt document must
    never break an apply. ``capture_defense``/``start_defense``/
    ``neutral_defense`` are also capped at ``max_defense``."""
    stored = _as_dict(raw)
    out = dict(DEFAULT_SETTINGS)
    for key, choices in _ENUM_SETTINGS.items():
        if stored.get(key) in choices:
            out[key] = stored[key]
    if _int_or(stored.get("summary_hours"), -1) in SUMMARY_HOURS_CHOICES:
        out["summary_hours"] = int(stored["summary_hours"])
    for key, (lo, hi) in SETTING_BOUNDS.items():
        if key in stored:
            out[key] = min(max(_int_or(stored[key], out[key]), lo), hi)
    cap = out["max_defense"]
    for key in ("capture_defense", "start_defense", "neutral_defense"):
        out[key] = min(out[key], cap)
    return out


def clean_settings_patch(body) -> tuple[dict, list]:
    """Validate a partial settings document from the API. Returns ``(patch,
    errors)``: known keys with valid values go into ``patch``; anything else
    is an error string (the route 422s on any). Cross-field caps are applied
    later by :func:`conquest_settings` on the merged document."""
    patch: dict = {}
    errors: list = []
    if not isinstance(body, dict):
        return patch, ["Settings must be an object."]
    for key, value in body.items():
        if key in _ENUM_SETTINGS:
            if value in _ENUM_SETTINGS[key]:
                patch[key] = value
            else:
                errors.append(f"{key} must be one of {list(_ENUM_SETTINGS[key])}.")
        elif key == "summary_hours":
            if (not isinstance(value, bool)
                    and _int_or(value, -1) in SUMMARY_HOURS_CHOICES):
                patch[key] = int(value)
            else:
                errors.append(f"summary_hours must be one of {list(SUMMARY_HOURS_CHOICES)}.")
        elif key in SETTING_BOUNDS:
            lo, hi = SETTING_BOUNDS[key]
            n = _int_or(value, lo - 1)
            if isinstance(value, bool) or not lo <= n <= hi:
                errors.append(f"{key} must be a whole number from {lo} to {hi}.")
            else:
                patch[key] = n
        else:
            errors.append(f"Unknown setting: {key}.")
    return patch, errors


# --------------------------------------------------------------------------- #
# Troops
# --------------------------------------------------------------------------- #
def troops_for_progress(previous, current, threshold, troops_per: int = 1) -> int:
    """Troops earned when a rule's running progress moves ``previous`` ->
    ``current``: one batch of ``troops_per`` per new multiple of
    ``threshold`` crossed. Negative when progress falls back across a
    multiple (a revoke), which the caller books as troop debt."""
    t = max(_int_or(threshold, 1), 1)
    per = max(_int_or(troops_per, 1), 1)
    return (max(_int_or(current, 0), 0) // t - max(_int_or(previous, 0), 0) // t) * per


def progress_to_next(progress, threshold) -> tuple[int, int]:
    """``(have, need)`` toward the NEXT troop — what the map shows as
    "23/35 kills"."""
    t = max(_int_or(threshold, 1), 1)
    return max(_int_or(progress, 0), 0) % t, t


# --------------------------------------------------------------------------- #
# Battles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TroopOutcome:
    """What one troop did to one tile. ``owner_*`` are team ids (None =
    unowned); dice faces are sorted highest first."""

    outcome: str
    owner_before: Optional[int]
    owner_after: Optional[int]
    defense_before: int
    defense_after: int
    attack_dice: tuple = ()
    defense_dice: tuple = ()

    @property
    def captured(self) -> bool:
        return self.outcome in CAPTURE_OUTCOMES


def roll_dice(count: int, rng) -> list:
    """``count`` d6 faces, highest first."""
    return sorted((rng.randint(1, DICE_SIDES) for _ in range(max(int(count), 0))),
                  reverse=True)


def battle_losses(attack: Iterable[int], defense: Iterable[int]) -> int:
    """Defense points the defender loses: compare the highest dice in pairs
    (as many pairs as the smaller side has dice); the attacker wins a pair
    only with a strictly higher face — ties go to the defender."""
    a = sorted(attack, reverse=True)
    d = sorted(defense, reverse=True)
    return sum(1 for x, y in zip(a, d) if x > y)


def resolve_troop(owner, defense, team_id: int, settings: dict,
                  rng) -> TroopOutcome:
    """Resolve one troop of ``team_id`` against a tile owned by ``owner``
    (None = unowned) with ``defense`` points. ``settings`` is a
    :func:`conquest_settings` dict. See the module docstring for the rules."""
    max_def = settings["max_defense"]
    capture_def = min(settings["capture_defense"], max_def)
    defense = max(_int_or(defense, 0), 0)
    if owner is not None and owner == team_id:
        if defense >= max_def:
            return TroopOutcome("full", owner, owner, defense, defense)
        return TroopOutcome("fortify", owner, owner, defense, defense + 1)
    if defense <= 0:
        kind = "claim" if owner is None else "capture"
        return TroopOutcome(kind, owner, team_id, defense, capture_def)
    if settings["battle_mode"] == "attrition":
        after = defense - 1
        return TroopOutcome("breach" if after == 0 else "attack",
                            owner, owner, defense, after)
    attack = roll_dice(settings["attack_dice"], rng)
    defend = roll_dice(min(defense, settings["defense_dice"]), rng)
    after = max(defense - battle_losses(attack, defend), 0)
    if after == defense:
        kind = "repelled"
    else:
        kind = "breach" if after == 0 else "attack"
    return TroopOutcome(kind, owner, owner, defense, after,
                        tuple(attack), tuple(defend))


def resolve_troops(owner, defense, team_id: int, count: int, settings: dict,
                   rng) -> list:
    """Resolve ``count`` troops one after another (each sees the tile the
    previous one left). Troops after a capture fortify the new holding."""
    out = []
    for _ in range(max(_int_or(count, 0), 0)):
        result = resolve_troop(owner, defense, team_id, settings, rng)
        out.append(result)
        owner, defense = result.owner_after, result.defense_after
    return out


def make_rng(seed=None):
    """The battle RNG: unpredictable (OS entropy) in production, seeded in
    tests."""
    if seed is None:
        return random.SystemRandom()
    return random.Random(seed)


# --------------------------------------------------------------------------- #
# Regions
# --------------------------------------------------------------------------- #
def region_owner(owners: Iterable) -> Optional[int]:
    """The team controlling a region: the one team that owns EVERY tile in
    it, else None. An empty region is never controlled."""
    owners = list(owners)
    if not owners or owners[0] is None:
        return None
    first = owners[0]
    return first if all(o == first for o in owners) else None


def region_tile_map(tiles: Iterable[dict]) -> dict:
    """{region_id: [tile ids]} over the ownable (normal) tiles only."""
    out: dict = {}
    for t in tiles:
        if t.get("kind", "normal") != "normal" or t.get("region_id") is None:
            continue
        out.setdefault(t["region_id"], []).append(t["id"])
    return out


def region_owners(tiles: Iterable[dict]) -> dict:
    """{region_id: controlling team id or None} from current tile owners."""
    tiles = list(tiles)
    owner_of = {t["id"]: t.get("owner_team_id") for t in tiles}
    return {rid: region_owner(owner_of[tid] for tid in tids)
            for rid, tids in region_tile_map(tiles).items()}


# --------------------------------------------------------------------------- #
# Ownership history + scoring
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Hold:
    """One stretch of one team owning one tile. ``end`` None = still held."""

    tile_id: int
    team_id: int
    start: datetime
    end: Optional[datetime] = None


def _clip(start: datetime, end: Optional[datetime], w0: datetime,
          w1: datetime) -> Optional[tuple]:
    """``(start, end)`` clipped to ``[w0, w1]`` as float seconds from ``w0``,
    or None when nothing is left."""
    s = max(start, w0)
    e = min(end or w1, w1)
    if e <= s:
        return None
    return ((s - w0).total_seconds(), (e - w0).total_seconds())


def _merge(intervals: list) -> list:
    out: list = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _intersect(a: list, b: list) -> list:
    """Intersection of two sorted, merged interval lists."""
    out = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _total(intervals: list) -> float:
    return sum(e - s for s, e in intervals)


@dataclass
class TeamStanding:
    team_id: int
    score: float = 0.0
    tiles: int = 0
    regions: int = 0
    # Value of what the team holds right now: points per hour in hold_time
    # mode, the points it would finish with in final mode.
    holding: float = 0.0
    region_ids: list = field(default_factory=list)


def compute_standings(mode: str, tiles: Iterable[dict], regions: Iterable[dict],
                      holds: Iterable[Hold], team_ids: Iterable[int],
                      window_start: Optional[datetime],
                      window_end: Optional[datetime]) -> dict:
    """Every team's :class:`TeamStanding` for one map.

    ``tiles``: dicts with ``id``, ``value``, ``region_id``, ``kind``,
    ``owner_team_id`` (current). ``regions``: dicts with ``id``, ``bonus``.
    ``holds``: the ownership history. ``window_start``/``window_end`` bound the
    scoring window (hold_time); either None means nothing has accrued yet.

    Current tiles/regions/holding always come from the CURRENT owners; the
    score is either that holding (final) or the time-weighted history
    (hold_time)."""
    tiles = [t for t in tiles if t.get("kind", "normal") == "normal"]
    regions = list(regions)
    teams = {int(t): TeamStanding(int(t)) for t in team_ids}
    value_of = {t["id"]: float(t.get("value") or 0) for t in tiles}
    bonus_of = {r["id"]: float(r.get("bonus") or 0) for r in regions}

    for t in tiles:
        owner = t.get("owner_team_id")
        if owner in teams:
            teams[owner].tiles += 1
            teams[owner].holding += value_of[t["id"]]
    for rid, owner in region_owners(tiles).items():
        if owner in teams and rid in bonus_of:
            teams[owner].regions += 1
            teams[owner].region_ids.append(rid)
            teams[owner].holding += bonus_of[rid]

    if mode == "final":
        for st in teams.values():
            st.score = round(st.holding, 2)
        return teams

    if window_start is None or window_end is None or window_end <= window_start:
        return teams
    # (tile, team) -> merged clipped intervals.
    per: dict = {}
    for h in holds:
        if h.team_id not in teams or h.tile_id not in value_of:
            continue
        clipped = _clip(h.start, h.end, window_start, window_end)
        if clipped:
            per.setdefault((h.tile_id, h.team_id), []).append(clipped)
    per = {k: _merge(v) for k, v in per.items()}
    for (tile_id, team_id), ivs in per.items():
        teams[team_id].score += value_of[tile_id] * _total(ivs) / 3600.0
    for rid, tile_ids in region_tile_map(tiles).items():
        bonus = bonus_of.get(rid, 0.0)
        if not bonus:
            continue
        for team_id, st in teams.items():
            common = per.get((tile_ids[0], team_id), [])
            for tid in tile_ids[1:]:
                if not common:
                    break
                common = _intersect(common, per.get((tid, team_id), []))
            if common:
                st.score += bonus * _total(common) / 3600.0
    for st in teams.values():
        st.score = round(st.score, 2)
    return teams


def rank_teams(standings: dict) -> list:
    """Team ids best first: score, then tiles held, then regions held, then
    the lower team id (stable)."""
    return sorted(standings, key=lambda tid: (-standings[tid].score,
                                              -standings[tid].tiles,
                                              -standings[tid].regions, tid))


# --------------------------------------------------------------------------- #
# Starting deal
# --------------------------------------------------------------------------- #
def deal_tiles(tiles: Iterable[tuple], team_ids: Iterable[int], rng) -> dict:
    """Deal every tile out between the teams, Risk-style. ``tiles`` is
    ``(tile_id, value)`` pairs. A snake draft over the tiles sorted by value
    (random among equal values) keeps both the count (within one) and the
    total value balanced. Returns ``{tile_id: team_id}``."""
    teams = [int(t) for t in team_ids]
    if not teams:
        return {}
    order = list(tiles)
    rng.shuffle(order)
    order.sort(key=lambda t: -float(t[1] or 0))  # stable: ties stay shuffled
    rng.shuffle(teams)
    n = len(teams)
    out = {}
    for k, (tile_id, _value) in enumerate(order):
        rnd, pos = divmod(k, n)
        out[tile_id] = teams[pos] if rnd % 2 == 0 else teams[n - 1 - pos]
    return out


# --------------------------------------------------------------------------- #
# Map validation (the designer's save)
# --------------------------------------------------------------------------- #
def _clean_color(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) == 7 and v[0] == "#":
        try:
            int(v[1:], 16)
        except ValueError:
            return None
        return v.lower()
    return None


def _frac(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f < 0 or f > 1:  # NaN or out of range
        return None
    return f


def validate_map(body) -> tuple[dict, list]:
    """Structural validation of a whole-map save. Returns ``(clean, errors)``
    where ``clean`` = ``{"regions": [...], "tiles": [...]}`` with client keys
    preserved for the route to resolve. DB checks (task ownership, task
    types) happen in the route; this only needs the payload."""
    errors: list = []
    if not isinstance(body, dict):
        return {}, ["The map must be an object."]
    regions_in = body.get("regions") or []
    tiles_in = body.get("tiles") or []
    if not isinstance(regions_in, list) or not isinstance(tiles_in, list):
        return {}, ["regions and tiles must be lists."]
    if len(regions_in) > MAX_REGIONS:
        errors.append(f"A map can have at most {MAX_REGIONS} regions.")
    if len(tiles_in) > MAX_TILES:
        errors.append(f"A map can have at most {MAX_TILES} tiles.")

    regions = []
    region_keys = set()
    for i, r in enumerate(regions_in):
        if not isinstance(r, dict):
            errors.append(f"Region {i + 1} is not an object.")
            continue
        key = str(r.get("key") or "").strip()
        name = str(r.get("name") or "").strip()
        if not key:
            errors.append(f"Region {i + 1} needs a key.")
            continue
        if key in region_keys:
            errors.append(f"Region key {key!r} is used twice.")
            continue
        region_keys.add(key)
        if not name or len(name) > MAX_REGION_NAME_LEN:
            errors.append(f"Region {i + 1} needs a name of 1 to "
                          f"{MAX_REGION_NAME_LEN} characters.")
            continue
        bonus = r.get("bonus", 0)
        try:
            bonus = float(bonus)
        except (TypeError, ValueError):
            bonus = -1
        if bonus < 0 or bonus > MAX_REGION_BONUS:
            errors.append(f"Region {name!r}: bonus must be 0 to {MAX_REGION_BONUS}.")
            continue
        regions.append({
            "key": key, "name": name, "bonus": round(bonus, 2),
            "color": _clean_color(r.get("color")),
            "label_x": _frac(r.get("label_x")), "label_y": _frac(r.get("label_y")),
        })

    tiles = []
    tile_keys = set()
    for i, t in enumerate(tiles_in):
        if not isinstance(t, dict):
            errors.append(f"Tile {i + 1} is not an object.")
            continue
        key = str(t.get("key") or "").strip()
        label = str(t.get("label") or "").strip()
        where = f"Tile {label!r}" if label else f"Tile {i + 1}"
        if not key or key in tile_keys:
            errors.append(f"{where} needs a unique key.")
            continue
        tile_keys.add(key)
        if not label or len(label) > MAX_LABEL_LEN:
            errors.append(f"{where} needs a label of 1 to {MAX_LABEL_LEN} characters.")
            continue
        x, y = _frac(t.get("x")), _frac(t.get("y"))
        if x is None or y is None:
            errors.append(f"{where} needs x and y between 0 and 1.")
            continue
        kind = t.get("kind") or "normal"
        if kind not in TILE_KINDS:
            errors.append(f"{where}: kind must be one of {list(TILE_KINDS)}.")
            continue
        value = t.get("value", 1)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = -1
        if value < 0 or value > MAX_TILE_VALUE:
            errors.append(f"{where}: value must be 0 to {MAX_TILE_VALUE}.")
            continue
        region_key = t.get("region_key")
        region_key = str(region_key).strip() if region_key not in (None, "") else None
        if region_key is not None and region_key not in region_keys:
            errors.append(f"{where} names an unknown region {region_key!r}.")
            continue
        rules_in = t.get("rules") or []
        if not isinstance(rules_in, list):
            errors.append(f"{where}: rules must be a list.")
            continue
        if kind == "respawn" and rules_in:
            errors.append(f"{where} is a respawn tile, which can't have rules.")
            continue
        if len(rules_in) > MAX_RULES_PER_TILE:
            errors.append(f"{where} can have at most {MAX_RULES_PER_TILE} rules.")
            continue
        rules = []
        for j, rule in enumerate(rules_in):
            if not isinstance(rule, dict):
                errors.append(f"{where}: rule {j + 1} is not an object.")
                continue
            troops = rule.get("troops", 1)
            if (isinstance(troops, bool) or _int_or(troops, 0) < 1
                    or _int_or(troops, 0) > MAX_TROOPS_PER_RULE):
                errors.append(f"{where}: troops must be 1 to {MAX_TROOPS_PER_RULE}.")
                continue
            task_id = rule.get("task_id")
            new_task = rule.get("new_task")
            if (task_id is None) == (new_task is None):
                errors.append(f"{where}: each rule needs exactly one of task_id "
                              "or new_task.")
                continue
            if task_id is not None and _int_or(task_id, 0) <= 0:
                errors.append(f"{where}: task_id must be a positive id.")
                continue
            if new_task is not None and not isinstance(new_task, dict):
                errors.append(f"{where}: new_task must be an object.")
                continue
            rules.append({
                "task_id": _int_or(task_id, 0) or None,
                "new_task": new_task,
                "troops": int(troops),
            })
        icon_item_id = _int_or(t.get("icon_item_id"), 0) or None
        icon_npc_id = _int_or(t.get("icon_npc_id"), 0) or None
        tiles.append({
            "key": key, "label": label, "x": x, "y": y, "kind": kind,
            "value": round(value, 2), "region_key": region_key,
            "icon_item_id": icon_item_id, "icon_npc_id": icon_npc_id,
            "rules": rules,
        })
    return {"regions": regions, "tiles": tiles}, errors


def rule_task_problem(task_type: str, config: Optional[dict]) -> Optional[str]:
    """Why a task can't back a tile rule (None = fine)."""
    if task_type not in RULE_TASK_TYPES:
        return (f"'{task_type}' tasks can't drive a Conquest tile: tiles need "
                "a task that counts up (kills, items, XP, loot value, pets, "
                "combat achievements, slayer tasks or a manual task).")
    if task_type == "item_collection":
        kind = (config or {}).get("kind") if isinstance(config, dict) else None
        if kind not in RULE_ITEM_LIST_KINDS:
            return ("Item sets that need each item once (all of / assembly / "
                    "distinct) can't drive a Conquest tile. Use a single item, "
                    "'any of' or a points list.")
    return None


# --------------------------------------------------------------------------- #
# Display helpers (Discord lines; the site renders from structured data)
# --------------------------------------------------------------------------- #
def dice_text(faces: Iterable[int]) -> str:
    return " · ".join(str(int(f)) for f in faces)


def fmt_points(value) -> str:
    """1234.5 -> "1,234.5"; 12.0 -> "12"."""
    try:
        v = round(float(value), 1)
    except (TypeError, ValueError):
        return "0"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.1f}"


def outcome_headline(outcome: str, *, team: str, tile: str,
                     owner: Optional[str] = None) -> str:
    """One-line Discord headline for a troop outcome. No em-dashes: this is
    user-facing copy."""
    team_b, tile_b = f"**{team}**", f"**{tile}**"
    owner_b = f"**{owner}**" if owner else "the defenders"
    if outcome == "claim":
        return f"\U0001F6A9 {team_b} claimed {tile_b}"
    if outcome == "capture":
        return f"⚔️ {team_b} captured {tile_b} from {owner_b}"
    if outcome == "breach":
        return f"\U0001F4A5 {team_b} broke through the defenses of {tile_b}"
    if outcome == "attack":
        return f"\U0001F3B2 {team_b} attacked {tile_b}"
    if outcome == "repelled":
        return f"\U0001F6E1️ {owner_b} held {tile_b} against {team_b}"
    if outcome == "fortify":
        return f"\U0001F9F1 {team_b} fortified {tile_b}"
    if outcome == "adjust":
        return f"\U0001F6E0️ An organiser set {tile_b} to {team_b}"
    return f"{team_b} reinforced {tile_b}"


def battle_detail(result: TroopOutcome, *, defender: Optional[str] = None) -> Optional[str]:
    """The dice line under an attack: "🎲 6 · 3 vs 5 · 4, defense 2 → 1".
    None for outcomes without dice."""
    if not result.attack_dice:
        if result.outcome in ("attack", "breach"):
            return (f"Defense {result.defense_before} → {result.defense_after}")
        return None
    who = f" ({defender})" if defender else ""
    return (f"\U0001F3B2 `{dice_text(result.attack_dice)}` vs "
            f"`{dice_text(result.defense_dice)}`{who}, defense "
            f"{result.defense_before} → {result.defense_after}")
