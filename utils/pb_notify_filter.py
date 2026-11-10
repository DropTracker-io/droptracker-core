"""Which personal bests a group posts (suggestion #239).

A group that tracks hundreds of members does not want every PB in Discord. It
can narrow them two ways, both optional:

**Conditions**, of which a PB must meet any one (or every one, with
``pb_notify_match = all``) of those the group turned on:

* ``pb_notify_top_ranks`` -- in the clan's top N for that boss and team size,
  ranked among the group's members;
* ``pb_notify_global_top_ranks`` -- in the top N of everyone tracked;
* ``pb_notify_speed_tier`` -- at or under that combat achievement tier's speed
  target for the boss and team size (``utils.ca_speed``). A boss with no speed
  task at that tier or harder never meets it.

With no condition turned on, every PB qualifies.

**Limits**, which always apply:

* ``pb_notify_bosses`` -- only these bosses (empty = all);
* ``pb_notify_team`` -- ``solo`` or ``group`` boards only;
* ``pb_notify_min_improvement`` -- seconds a PB must beat the member's previous
  time by (a member's first time has nothing to beat and is not held back);
* ``pb_notify_skip_first`` -- not a member's first recorded time at a boss.

The PB is recorded, ranked and scored whatever is decided here; this only
chooses what is announced. Ranks are looked up lazily (most groups set none),
and an unknown rank -- the player is off the board, or the lookup failed --
counts as met: a missed announcement is worse than an extra one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Mapping, Optional

from utils.ca_speed import TIER_LABELS, TIERS, meets_tier

CLAN_TOP_KEY = "pb_notify_top_ranks"
GLOBAL_TOP_KEY = "pb_notify_global_top_ranks"
SPEED_TIER_KEY = "pb_notify_speed_tier"
MATCH_KEY = "pb_notify_match"
BOSSES_KEY = "pb_notify_bosses"
TEAM_KEY = "pb_notify_team"
MIN_IMPROVEMENT_KEY = "pb_notify_min_improvement"
SKIP_FIRST_KEY = "pb_notify_skip_first"

KEYS = (
    CLAN_TOP_KEY, GLOBAL_TOP_KEY, SPEED_TIER_KEY, MATCH_KEY,
    BOSSES_KEY, TEAM_KEY, MIN_IMPROVEMENT_KEY, SKIP_FIRST_KEY,
)

TEAM_CHOICES = ("any", "solo", "group")
MATCH_CHOICES = ("any", "all")

RankLookup = Callable[[], Optional[int]]


def _int(value) -> int:
    try:
        return max(int(str(value).strip()), 0)
    except (TypeError, ValueError):
        return 0


def _choice(value, choices, default):
    text = str(value or "").strip().lower()
    return text if text in choices else default


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def parse_bosses(value) -> frozenset:
    """Match keys of a stored boss list (``"Zulrah, Vorkath"`` or the legacy
    ``["Zulrah", "Vorkath"]``)."""
    from utils.npc_names import npc_match_key

    raw = str(value or "").replace("[", "").replace("]", "")
    names = (part.strip().strip('"').strip("'").strip() for part in raw.split(","))
    return frozenset(npc_match_key(name) for name in names if name)


@dataclass(frozen=True)
class PbNotifyRules:
    clan_top: int = 0
    global_top: int = 0
    speed_tier: Optional[str] = None
    match_all: bool = False
    bosses: frozenset = frozenset()
    team: str = "any"
    min_improvement_ms: int = 0
    skip_first: bool = False

    @classmethod
    def from_config(cls, values: Mapping[str, object]) -> "PbNotifyRules":
        tier = _choice(values.get(SPEED_TIER_KEY), TIERS, None)
        return cls(
            clan_top=_int(values.get(CLAN_TOP_KEY)),
            global_top=_int(values.get(GLOBAL_TOP_KEY)),
            speed_tier=tier,
            match_all=_choice(values.get(MATCH_KEY), MATCH_CHOICES, "any") == "all",
            bosses=parse_bosses(values.get(BOSSES_KEY)),
            team=_choice(values.get(TEAM_KEY), TEAM_CHOICES, "any"),
            min_improvement_ms=_int(values.get(MIN_IMPROVEMENT_KEY)) * 1000,
            skip_first=_truthy(values.get(SKIP_FIRST_KEY)),
        )

    @property
    def active(self) -> bool:
        return self != PbNotifyRules()


def withheld_reason(
    rules: PbNotifyRules,
    *,
    npc_name,
    team_size,
    time_ms: int,
    old_time_ms: Optional[int],
    clan_rank: RankLookup,
    global_rank: RankLookup,
) -> Optional[str]:
    """Why this PB is not posted for the group, or None to post it."""
    if not rules.active:
        return None
    from utils.npc_names import npc_match_key
    from utils.pb_rank import normalize_team_size

    if rules.bosses and npc_match_key(str(npc_name or "")) not in rules.bosses:
        return "not one of the group's bosses"
    if rules.team != "any":
        solo = normalize_team_size(team_size) == "Solo"
        if solo != (rules.team == "solo"):
            return f"not a {rules.team} time"
    first = not old_time_ms or int(old_time_ms) <= 0
    if first and rules.skip_first:
        return "the member's first time at this boss"
    if not first and rules.min_improvement_ms and int(old_time_ms) - int(time_ms) < rules.min_improvement_ms:
        return "improved by less than the group's minimum"

    # Cheapest first: the speed check reads a table, the ranks query the
    # board, and any/all both stop at the first answer that settles it.
    checks = []
    if rules.speed_tier:
        checks.append((
            f"a {TIER_LABELS[rules.speed_tier]} time",
            lambda: meets_tier(npc_name, team_size, time_ms, rules.speed_tier),
        ))
    if rules.clan_top:
        checks.append(("in the clan's top", lambda: _within(clan_rank, rules.clan_top)))
    if rules.global_top:
        checks.append(("in the global top", lambda: _within(global_rank, rules.global_top)))
    if not checks:
        return None
    if rules.match_all:
        for label, check in checks:
            if not check():
                return f"not {label}"
        return None
    if any(check() for _label, check in checks):
        return None
    return "met none of the group's conditions"


def _within(lookup: RankLookup, top: int) -> bool:
    rank = lookup()
    return rank is None or rank <= top


def load_rules(session, group_ids: Iterable[int]) -> Dict[int, PbNotifyRules]:
    """``{group_id: rules}`` in one query. A boss list too long for
    ``config_value`` lives in ``long_value`` (web_api/routes/config.py
    LONG_VALUE_KEYS), which ``utils.group_config.get_bulk`` does not read."""
    from db.models import GroupConfiguration

    gids = sorted({int(g) for g in group_ids})
    if not gids:
        return {}
    rows = (
        session.query(
            GroupConfiguration.group_id,
            GroupConfiguration.config_key,
            GroupConfiguration.config_value,
            GroupConfiguration.long_value,
        )
        .filter(GroupConfiguration.group_id.in_(gids), GroupConfiguration.config_key.in_(KEYS))
        .all()
    )
    values: Dict[int, Dict[str, object]] = {gid: {} for gid in gids}
    for gid, key, value, long_value in rows:
        if key == BOSSES_KEY and (value is None or len(str(value).strip()) < 10) and long_value:
            value = long_value
        values[int(gid)][key] = value
    return {gid: PbNotifyRules.from_config(vals) for gid, vals in values.items()}


def tier_label(tier: Optional[str]) -> str:
    """"Grandmaster" for the ``{speed_tier}`` placeholder; "" for none."""
    return TIER_LABELS.get(tier or "", "")
