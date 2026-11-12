"""EHP and EHB: efficient hours played and bossed, computed here.

A port of Wise Old Man's efficiency algorithm
(``server/src/api/modules/efficiency/EfficiencyAlgorithm.ts`` and
``efficiency.utils.ts``), with their rate tables in
``data/efficiency/rates.json`` (kept current by
``scripts/sync_wom_efficiency.py``).

    Wise Old Man is MIT licensed, Copyright (c) 2020 Ruben Amendoeira.
    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to
    the following conditions: The above copyright notice and this permission
    notice shall be included in all copies or substantial portions of the
    Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

The port is deliberately literal: same loops, same order, same rounding, so
that our numbers equal theirs for the same stats (``tests/unit/test_efficiency.py``
holds players' real WOM figures as fixtures). Resist tidying it.

Skill keys here are WOM's (``runecrafting``); `from_our_skills` converts from
ours (``runecraft``). Boss keys are the hiscores metric slugs both sides use.
"""
from __future__ import annotations

import json
import math
import os
import threading
from typing import Any, Dict, List, Mapping, Optional

MAX_SKILL_EXP = 200_000_000
SKILL_EXP_AT_99 = 13_034_431
OVERALL = "overall"

RATES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "efficiency", "rates.json")

# Algorithm types (WOM's EfficiencyAlgorithmType).
MAIN, IRONMAN, ULTIMATE, LVL3, F2P = "main", "ironman", "ultimate", "lvl3", "f2p"
F2P_LVL3, F2P_IRONMAN, F2P_LVL3_IRONMAN, DEF1 = "f2p_lvl3", "f2p_ironman", "f2p_lvl3_ironman", "def1"

_OUR_SKILL_NAMES = {"runecraft": "runecrafting"}


def round_number(num: float, cases: int) -> float:
    """JavaScript's ``Math.round(num * 10^cases) / 10^cases`` (half rounds up)."""
    scale = 10 ** cases
    return math.floor(num * scale + 0.5) / scale


def _level(xp: float) -> int:
    """Real level (1..99) for an XP total, as WOM's ``getLevel``."""
    from utils.xp_history import level_for_xp

    return level_for_xp(xp)


class EfficiencyAlgorithm:
    def __init__(self, kind: str, skill_metas: List[dict], boss_metas: Optional[List[dict]],
                 skills: List[str], bosses: List[str]):
        self.type = kind
        self.skill_metas = skill_metas
        self.boss_metas = boss_metas or []
        self.skills = skills
        self.real_skills = [s for s in skills if s != OVERALL]
        self.bosses = bosses
        self._methods = {meta["skill"]: meta.get("methods") for meta in skill_metas}

        self.start_bonuses = self._bonuses(skill_metas, end=False)
        self.end_bonuses = self._bonuses(skill_metas, end=True)
        # Which skills each skill hands bonus XP to.
        self.bonus_direction = self._direction_map(self.start_bonuses + self.end_bonuses)
        self.maximum_ehp = self._maximum_ehp_map()

    # -- public ------------------------------------------------------------ #

    def calculate_ehb(self, killcounts: Mapping[str, float]) -> float:
        return sum(self.calculate_ehb_map(killcounts).values())

    def calculate_ehp(self, stats: Mapping[str, float]) -> float:
        return self.calculate_ehp_map(stats)[OVERALL]

    def calculate_ehp_map(self, stats: Mapping[str, float]) -> Dict[str, float]:
        fixed = dict(stats)
        for skill in self.real_skills:
            fixed[skill] = max(0, stats.get(skill) or 0)

        result = {skill: 0.0 for skill in self.skills}
        start_bonus_exp = self._bonus_exp(fixed, end=False)
        end_bonus_exp = self._bonus_exp(fixed, end=True)

        for origin in self.real_skills:
            time_sum = 0.0
            # A JavaScript Set, iterated while it grows: a skill's bonus
            # skills, plus theirs, and so on (wc -> fm -> cooking).
            bonus_skills = list(dict.fromkeys(self.bonus_direction.get(origin) or []))
            index = 0
            while index < len(bonus_skills):
                for dependant in self.bonus_direction.get(bonus_skills[index]) or []:
                    if dependant not in bonus_skills:
                        bonus_skills.append(dependant)
                index += 1

            for bonus_skill in bonus_skills + [origin]:
                start_exp = fixed[bonus_skill] + start_bonus_exp[bonus_skill]
                end_exp = MAX_SKILL_EXP - end_bonus_exp[bonus_skill]
                if end_exp - start_exp <= 0 and bonus_skill != origin:
                    continue

                reset = dict(fixed)
                reset[origin] = 0
                start_reset_bonus = self._bonus_exp(reset, end=False)
                end_reset_bonus = self._bonus_exp(reset, end=True)
                start_exp_reset = reset[bonus_skill] + start_reset_bonus[bonus_skill]
                end_exp_reset = MAX_SKILL_EXP - end_reset_bonus[bonus_skill]

                diff = (self._skill_time(bonus_skill, start_exp_reset, end_exp_reset)
                        - self._skill_time(bonus_skill, start_exp, end_exp))
                if end_exp - start_exp <= 0:
                    time_sum += min(self.maximum_ehp[bonus_skill], diff)
                else:
                    time_sum += diff

            result[origin] = round_number(time_sum, 5)

        result[OVERALL] = sum(result.values())
        return result

    def calculate_ehb_map(self, killcounts: Mapping[str, float]) -> Dict[str, float]:
        fixed = {boss: max(0, killcounts.get(boss) or 0) for boss in self.bosses}
        result = {boss: 0.0 for boss in self.bosses}
        for meta in self.boss_metas:
            if not meta or meta["rate"] <= 0:
                continue
            result[meta["boss"]] = round_number(fixed.get(meta["boss"], 0) / meta["rate"], 5)
        return result

    # -- internals ---------------------------------------------------------- #

    @staticmethod
    def _bonuses(metas: List[dict], *, end: bool) -> List[dict]:
        out = []
        for meta in metas:
            for bonus in meta.get("bonuses") or []:
                if bonus and bonus.get("end") is end:
                    out.append(bonus)
        return out

    @staticmethod
    def _direction_map(bonuses: List[dict]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for bonus in bonuses:
            targets = out.setdefault(bonus["originSkill"], [])
            if bonus["bonusSkill"] not in targets:
                targets.append(bonus["bonusSkill"])
        return out

    def _meta(self, skill: str) -> Optional[dict]:
        for meta in self.skill_metas:
            if meta["skill"] == skill:
                return meta
        return None

    def _bonus_exp(self, stats: Mapping[str, float], *, end: bool) -> Dict[str, float]:
        is_start = not end
        bonuses = self.start_bonuses if is_start else self.end_bonuses
        result = {skill: 0.0 for skill in self.real_skills}

        # Skills that receive no bonus XP themselves are applied first. The
        # sort is stable and in place, exactly as upstream's.
        bonuses.sort(key=lambda b: -len(self.bonus_direction.get(b["bonusSkill"]) or []))
        for bonus in bonuses:
            origin, target = bonus["originSkill"], bonus["bonusSkill"]
            if not is_start:
                scaled = None
                if origin == "hunter" and target == "fishing":
                    scaled = self._scaled_max_bonus(stats, "hunter", "fishing")
                elif origin == "thieving" and target == "agility":
                    scaled = self._scaled_max_bonus(stats, "thieving", "agility")
                elif origin == "firemaking" and target == "thieving":
                    scaled = self._scaled_max_bonus(stats, "firemaking", "thieving")
                if scaled:
                    result[target] = result[target] + scaled
                    continue

            exp_cap = min(bonus["endExp"], MAX_SKILL_EXP)
            origin_start = max(stats[origin], bonus["startExp"]) + (result[origin] if is_start else 0)
            origin_end = exp_cap - result[origin] if not is_start else exp_cap
            bonus_to_apply = max(0, origin_end - origin_start) * bonus["ratio"]
            result[target] = min(MAX_SKILL_EXP, result[target] + bonus_to_apply)
        return result

    def _scaled_max_bonus(self, stats: Mapping[str, float], origin: str, target: str) -> float:
        """The drift net / swimming / firefact special cases: a bonus whose
        real rate differs from the rate the table credits it at."""
        origin_meta, target_meta = self._meta(origin), self._meta(target)
        origin_method = next((m for m in (origin_meta or {}).get("methods") or []
                              if m.get("realRate")), None)
        target_methods = (target_meta or {}).get("methods") or []
        target_method = target_methods[-1] if target_methods else None
        origin_bonuses = (origin_meta or {}).get("bonuses") or []
        ratio = origin_bonuses[0].get("ratio") if origin_bonuses else None
        if not origin_method or not target_method or not ratio:
            return 0

        origin_start = max(origin_method["startExp"], stats[origin])
        origin_left = MAX_SKILL_EXP - origin_start
        real_time = (self._skill_time(origin, origin_start, MAX_SKILL_EXP, True)
                     + self._skill_time(target, stats[target], MAX_SKILL_EXP, True))
        fake_time = (self._skill_time(origin, origin_start, MAX_SKILL_EXP, False)
                     + self._skill_time(target, stats[target], MAX_SKILL_EXP, False))
        excess = (real_time - fake_time) * target_method["rate"]
        return origin_left * ratio - excess

    def _skill_time(self, skill: str, start_exp: float, end_exp: float,
                    use_real_rates: bool = False) -> float:
        methods = self._methods.get(skill)
        # Zero-time skills (Hitpoints, Magic, Fletching).
        if not methods or (len(methods) == 1 and methods[0]["rate"] == 0):
            return (end_exp - start_exp) / MAX_SKILL_EXP

        total = 0.0
        for i, current in enumerate(methods):
            nxt = methods[i + 1] if i + 1 < len(methods) else None
            if current["rate"] == 0:
                continue
            rate = current["realRate"] if use_real_rates and current.get("realRate") else current["rate"]
            if nxt and nxt["startExp"] > start_exp and current["startExp"] < end_exp:
                gained = min(nxt["startExp"], end_exp) - max(start_exp, current["startExp"])
                total += gained / rate
            if not nxt and end_exp > current["startExp"]:
                gained = end_exp - max(current["startExp"], start_exp)
                total += gained / rate
        return total

    def _maximum_ehp_map(self) -> Dict[str, float]:
        result = {skill: 0.0 for skill in self.skills}
        zero = {skill: 0 for skill in self.real_skills}
        start_bonus = self._bonus_exp(zero, end=False)
        end_bonus = self._bonus_exp(zero, end=True)
        for skill in self.real_skills:
            result[skill] = self._skill_time(skill, zero[skill] + start_bonus[skill],
                                             MAX_SKILL_EXP - end_bonus[skill])
        result[OVERALL] = sum(result.values())
        return result


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_loaded: Dict[str, Any] = {"mtime": None, "tables": None, "algorithms": {}}


def tables() -> dict:
    """The rates document, reloaded when the file changes on disk (the sync
    job replaces it without restarting anything)."""
    mtime = os.path.getmtime(RATES_PATH)
    with _lock:
        if _loaded["mtime"] != mtime:
            with open(RATES_PATH) as handle:
                _loaded["tables"] = json.load(handle)
            _loaded["mtime"] = mtime
            _loaded["algorithms"] = {}
        return _loaded["tables"]


def algorithm(kind: str = MAIN) -> EfficiencyAlgorithm:
    doc = tables()
    with _lock:
        cached = _loaded["algorithms"].get(kind)
        if cached is None:
            if kind not in doc["ehp"]:
                raise ValueError(f"unknown algorithm type {kind!r}")
            cached = EfficiencyAlgorithm(kind, doc["ehp"][kind], doc["ehb"].get(kind),
                                         doc["skills"], doc["bosses"])
            _loaded["algorithms"][kind] = cached
        return cached


def ehb_rates(kind: str = MAIN) -> Dict[str, float]:
    """``{boss: kills per efficient hour}`` for an algorithm type; bosses with
    no rate are absent. Types without a boss table (lvl3, f2p...) are empty."""
    return {meta["boss"]: meta["rate"] for meta in tables()["ehb"].get(kind) or []
            if meta and meta["rate"] > 0}


# --------------------------------------------------------------------------- #
# Which algorithm a player gets
# --------------------------------------------------------------------------- #

def combat_level(levels: Mapping[str, int]) -> int:
    values = [levels[s] for s in ("attack", "strength", "defence", "ranged", "magic",
                                  "hitpoints", "prayer")]
    if any(level == 0 for level in values):
        return 0
    attack, strength, defence, ranged, magic, hitpoints, prayer = values
    base = 0.25 * (defence + max(hitpoints, 10) + prayer // 2)
    melee = 0.325 * (attack + strength)
    ranging = 0.325 * ((3 * ranged) // 2)
    maging = 0.325 * ((3 * magic) // 2)
    return math.floor(base + max(melee, ranging, maging))


def player_build(stats: Mapping[str, float], killcounts: Mapping[str, float], *,
                 fake_f2p: bool = False) -> str:
    """WOM's ``getBuild``: main, f2p, f2p_lvl3, lvl3, hp10, def1 or zerker.

    Missing values count as unranked (-1), as they do in a WOM snapshot.
    """
    doc = tables()
    xp = lambda skill: stats.get(skill, -1) if stats.get(skill) is not None else -1  # noqa: E731
    levels = {s: _level(xp(s)) for s in ("attack", "strength", "defence", "ranged", "magic",
                                          "hitpoints", "prayer")}
    combat = combat_level(levels)
    is_lvl3 = combat <= 3
    if not fake_f2p:
        member_stats = any(xp(s) > 0 for s in doc["member_skills"])
        f2p_bosses = set(doc["f2p_bosses"])
        member_kc = any((killcounts.get(b) or 0) > 0 for b in doc["bosses"] if b not in f2p_bosses)
        if not member_stats and not member_kc:
            return F2P_LVL3 if is_lvl3 else F2P
    if is_lvl3:
        return LVL3
    if combat > 3 and levels["hitpoints"] == 10:
        return "hp10"
    if levels["defence"] == 1:
        return DEF1
    if 40 <= levels["defence"] <= 45:
        return "zerker"
    return MAIN


# Our account types (utils.account_types) -> WOM's player types.
_IRON_TYPES = {"ironman": "ironman", "hardcore_ironman": "hardcore", "ultimate_ironman": "ultimate"}


def algorithm_type(account_type: Optional[str] = None, build: str = MAIN) -> str:
    """WOM's ``getAlgorithmType``. Group ironmen are scored as regular
    accounts, as WOM scores them (it has no group ironman type)."""
    kind = _IRON_TYPES.get(account_type or "", "regular")
    iron = kind in ("ironman", "hardcore", "ultimate")
    if build == F2P and iron:
        return F2P_IRONMAN
    if build == F2P_LVL3 and iron:
        return F2P_LVL3_IRONMAN
    if kind == "ultimate":
        return ULTIMATE
    if iron:
        return IRONMAN
    return build if build in (F2P_LVL3, F2P, LVL3, DEF1) else MAIN


def from_our_skills(skills: Mapping[str, float]) -> Dict[str, float]:
    """Our skill keys (``runecraft``) to the algorithm's (``runecrafting``)."""
    return {_OUR_SKILL_NAMES.get(skill, skill): xp for skill, xp in skills.items()}


def player_efficiency(skills: Mapping[str, float], killcounts: Mapping[str, float], *,
                      account_type: Optional[str] = None, build: Optional[str] = None) -> dict:
    """``{"ehp", "ehb", "type", "build"}`` for one set of stats.

    ``skills`` uses WOM's keys (see `from_our_skills`); unknown skills and
    bosses count as zero, as WOM counts an unranked entry. ``build`` is worked
    out from the stats unless given.
    """
    build = build or player_build(skills, killcounts)
    kind = algorithm_type(account_type, build)
    algo = algorithm(kind)
    return {"ehp": algo.calculate_ehp(skills), "ehb": algo.calculate_ehb(killcounts),
            "type": kind, "build": build}
