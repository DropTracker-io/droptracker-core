"""Unit tests for the event task generator core (services/task_generator.py).

The generator decides three things an admin used to do by hand: how big a
target is (sizing), how hard it is for THIS event (tiering against the team's
capacity), and which tasks make a balanced board (selection). Each is pinned
here against a small synthetic catalog so the tests never depend on live
drop tables.
"""
import random

import pytest

import services.task_generator as tg


def _enc(key, *, category="bosses", kph=40.0, uniques=None, unique_rate=None,
         pet=None, pet_rate=None, ca_tiers=(), conditional=False, label=None):
    return {
        "key": key,
        "label": label or key.replace("_", " ").title(),
        "category": category,
        "unit": "kills",
        "kc_npcs": [label or key.replace("_", " ").title()],
        "npc_ids": [1],
        "kph": kph,
        "kph_estimated": False,
        "uniques": [{"name": n, "rate": r} for n, r in (uniques or [])],
        "unique_rate": unique_rate,
        "unique_rate_estimated": False,
        "conditional": conditional,
        "pet": pet,
        "pet_rate": pet_rate,
        "ca_monsters_resolved": [label or key.title()] if ca_tiers else [],
        "ca_tiers": list(ca_tiers),
    }


CATALOG = [
    _enc("zulrah", kph=46, uniques=[("Tanzanite fang", 1 / 512), ("Magic fang", 1 / 512),
                                    ("Serpentine visage", 1 / 512)],
         unique_rate=3 / 512, pet="Pet snakeling", pet_rate=1 / 4000,
         ca_tiers=["Hard", "Elite"]),
    _enc("vorkath", kph=34, uniques=[("Vorkath's head", 1 / 50), ("Draconic visage", 1 / 5000)],
         unique_rate=1 / 49),
    _enc("chambers_of_xeric", category="raids", kph=3.5,
         uniques=[("Twisted bow", None), ("Dexterous prayer scroll", None)],
         unique_rate=1 / 29, conditional=True),
    _enc("callisto", category="wilderness", kph=140,
         uniques=[("Dragon pickaxe", 1 / 256), ("Tyrannical ring", 1 / 512)],
         unique_rate=3 / 512),
]


# --------------------------------------------------------------------------- #
# Sizing helpers
# --------------------------------------------------------------------------- #

class TestSizing:
    def test_capacity_scales_with_team_days_and_activity(self):
        assert tg.capacity_hours(5, 7) == pytest.approx(52.5)
        assert tg.capacity_hours(5, 7, "hardcore") == pytest.approx(105)
        # Nonsense inputs clamp rather than produce a zero-capacity event.
        assert tg.capacity_hours(0, 0) > 0

    def test_tier_bounds_are_contiguous_and_increasing(self):
        for cap in (5, 52.5, 420, 5000):
            b = tg.tier_bounds(cap)
            prev_hi = 0.0
            for t in tg.DIFFICULTIES:
                lo, hi = b[t]
                assert lo == pytest.approx(prev_hi)
                assert hi > lo
                prev_hi = hi

    def test_small_events_use_the_hour_floors(self):
        b = tg.tier_bounds(9)  # 3 players, 2 days
        assert b["air"][1] == tg.TIER_FLOOR_HOURS["air"]
        assert b["fire"][1] == tg.TIER_FLOOR_HOURS["fire"]

    def test_big_events_scale_past_the_floors(self):
        b = tg.tier_bounds(420)
        assert b["fire"][1] == pytest.approx(420 * tg.TIER_SHARE["fire"])

    def test_tier_for_hours(self):
        b = tg.tier_bounds(52.5)
        assert tg.tier_for_hours(0.1, b) == "air"
        assert tg.tier_for_hours(b["water"][1], b) == "water"
        assert tg.tier_for_hours(b["fire"][1] + 0.01, b) is None
        assert tg.tier_for_hours(0, b) is None

    @pytest.mark.parametrize("raw,expected", [
        (0.3, 1), (4.4, 4), (9.6, 10), (13, 15), (38, 40), (160, 150),
        (230, 250), (820, 750), (1200, 1000), (2_600_000, 2_500_000),
    ])
    def test_nice_round(self, raw, expected):
        assert tg.nice_round(raw) == expected

    def test_apportion_sums_and_follows_weights(self):
        out = tg.apportion(25, {"air": 3, "water": 3, "earth": 2, "fire": 1})
        assert sum(out.values()) == 25
        assert out["air"] >= out["earth"] >= out["fire"]
        assert tg.apportion(4, {"fire": 1}) == {"air": 0, "water": 0, "earth": 0, "fire": 4}
        # All-zero weights fall back to an even split instead of nothing.
        assert sum(tg.apportion(8, {}).values()) == 8

    def test_format_count(self):
        assert tg.format_count(2_000_000) == "2m"
        assert tg.format_count(1_500_000) == "1.5m"
        assert tg.format_count(75_000) == "75k"
        assert tg.format_count(5_000) == "5,000"


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #

class TestCandidates:
    bounds = tg.tier_bounds(52.5)

    def _by_kind(self, enc, kind):
        return [c for c in tg.encounter_candidates(enc, self.bounds) if c["kind"] == kind]

    def test_kc_targets_land_in_the_tier_they_were_sized_for(self):
        for c in self._by_kind(CATALOG[0], "kc"):
            assert tg.tier_for_hours(c["hours"], self.bounds) is not None
            assert c["task"]["type"] == "kc_target"
            assert c["task"]["target"] == "Zulrah"
            assert c["task"]["label"].endswith("Zulrah kills")

    def test_any_unique_locks_items_to_the_boss(self):
        uniq = [c for c in self._by_kind(CATALOG[0], "uniques")
                if c["key"].startswith("unique_any:")]
        assert uniq
        cfg = uniq[0]["task"]["config"]
        assert cfg["kind"] == "any_of"
        assert {i["item_name"] for i in cfg["items"]} == {
            "Tanzanite fang", "Magic fang", "Serpentine visage"}
        assert all(v == ["Zulrah"] for v in cfg["item_npcs"].values())

    def test_conditional_tables_get_no_specific_item_tasks(self):
        # A raid unique is ~8h: use a big enough event for it to fit at all.
        raid = [c for c in tg.encounter_candidates(CATALOG[2], tg.tier_bounds(420))
                if c["kind"] == "uniques"]
        assert raid and all(c["key"].startswith("unique_any:") for c in raid)

    def test_specific_unique_names_its_boss_only_when_needed(self):
        labels = {c["task"]["label"] for c in tg.encounter_candidates(
            CATALOG[3], tg.tier_bounds(420)) if c["key"].startswith("unique:")}
        assert "Dragon pickaxe from Callisto" in labels
        vork = {c["task"]["label"] for c in tg.encounter_candidates(CATALOG[1], self.bounds)
                if c["key"].startswith("unique:")}
        assert "Vorkath's head" in vork

    def test_candidates_above_elite_are_dropped(self):
        # Draconic visage at 1/5000 × 34/hr ≈ 147h: far past a 52h event.
        keys = {c["key"] for c in tg.encounter_candidates(CATALOG[1], self.bounds)}
        assert "unique:vorkath:draconic visage" not in keys

    def test_no_kill_rate_means_no_candidates(self):
        assert tg.encounter_candidates(_enc("mystery", kph=0), self.bounds) == []

    def test_ca_candidates_use_the_registry_tiers(self):
        cas = self._by_kind(CATALOG[0], "ca")
        assert {c["task"]["config"]["tiers"][0] for c in cas} <= {"Hard", "Elite"}
        assert all(c["task"]["type"] == "ca_target" for c in cas)

    def test_general_candidates_cover_xp_slayer_and_loot(self):
        kinds = {c["kind"] for c in tg.general_candidates(self.bounds)}
        assert kinds == {"xp", "slayer", "loot"}


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

def _gen(**kw):
    args = dict(count=12, capacity=52.5, mix={"air": 3, "water": 3, "earth": 2, "fire": 1},
                seed=1)
    args.update(kw)
    return tg.generate(CATALOG, **args)


class TestGenerate:
    def test_returns_count_tasks_with_points_and_difficulty(self):
        res = _gen()
        assert len(res["tasks"]) == 12
        for row in res["tasks"]:
            assert row["task"]["difficulty"] == row["difficulty"]
            assert row["task"]["points"] == tg.DEFAULT_POINTS[row["difficulty"]]
            cfg = row["task"].get("config")
            assert cfg is None or isinstance(cfg, str)  # web EventTaskInput shape

    def test_same_seed_same_board(self):
        a = [r["key"] for r in _gen(seed=42)["tasks"]]
        b = [r["key"] for r in _gen(seed=42)["tasks"]]
        c = [r["key"] for r in _gen(seed=43)["tasks"]]
        assert a == b
        assert a != c

    def test_no_boss_appears_more_than_its_cap(self):
        for seed in range(20):
            res = _gen(count=20, seed=seed)
            per_enc = {}
            for r in res["tasks"]:
                if r["encounter"]:
                    per_enc[r["encounter"]] = per_enc.get(r["encounter"], 0) + 1
            assert max(per_enc.values(), default=0) <= tg.MAX_PER_FAMILY["encounter"]

    def test_labels_are_unique(self):
        for seed in range(10):
            labels = [r["task"]["label"] for r in _gen(count=20, seed=seed)["tasks"]]
            assert len(labels) == len(set(labels))

    def test_category_and_kind_filters(self):
        res = _gen(categories={"raids", "wilderness"}, kinds={"kc", "uniques"}, count=6)
        assert res["tasks"]
        assert {r["category"] for r in res["tasks"]} <= {"raids", "wilderness"}
        assert {r["kind"] for r in res["tasks"]} <= {"kc", "uniques"}

    def test_must_include_survives_the_category_filter(self):
        res = _gen(categories={"raids"}, must_include=["zulrah"], count=4)
        assert "zulrah" in {r["encounter"] for r in res["tasks"]}

    def test_exclusions(self):
        first = _gen(seed=5)
        keys = [r["key"] for r in first["tasks"]]
        again = _gen(seed=5, exclude_keys=keys)
        assert not set(keys) & {r["key"] for r in again["tasks"]}
        no_zul = _gen(exclude_encounters=["zulrah"], count=15)
        assert "zulrah" not in {r["encounter"] for r in no_zul["tasks"]}
        labels = [r["task"]["label"] for r in first["tasks"]]
        assert not set(labels) & {r["task"]["label"] for r in _gen(
            seed=5, exclude_labels=labels)["tasks"]}

    def test_reroll_keeps_tier_and_respects_taken(self):
        board = _gen(seed=9)
        keys = [r["key"] for r in board["tasks"]]
        re = _gen(count=1, only_tier="earth", exclude_keys=keys, taken_keys=keys, seed=10)
        assert len(re["tasks"]) == 1
        assert re["tasks"][0]["difficulty"] == "earth"
        assert re["tasks"][0]["key"] not in keys

    def test_shortfall_borrows_then_reports_unfilled(self):
        # Only raids + kc: a thin pool, so asking for many elites must borrow.
        res = _gen(categories={"raids"}, kinds={"kc"}, count=10,
                   mix={"fire": 1})
        assert res["shortfall"]
        tiny = tg.generate([], count=5, capacity=52.5, mix={"air": 1}, seed=1,
                           categories={"raids"})
        assert tiny["tasks"] == []
        assert tiny["shortfall"].get("unfilled") == 5

    def test_count_is_capped(self):
        res = _gen(count=10_000)
        assert len(res["tasks"]) <= tg.MAX_GENERATE


class TestClanFocus:
    def _share(self, focus):
        hits = total = 0
        for seed in range(60):
            res = _gen(count=6, seed=seed, clan_focus=focus,
                       activity={"zulrah": 40}, roster=50,
                       categories={"bosses", "wilderness", "raids"},
                       kinds={"kc", "uniques"})
            for r in res["tasks"]:
                total += 1
                hits += r["encounter"] == "zulrah"
        return hits / total

    def test_familiar_favours_active_bosses_and_fresh_avoids_them(self):
        familiar, off, fresh = self._share("familiar"), self._share("off"), self._share("fresh")
        assert familiar > off > fresh

    def test_clan_factor_is_neutral_without_an_encounter(self):
        c = {"encounter": None}
        assert tg._clan_factor(c, "familiar", {"zulrah": 10}, 50) == 1.0


class TestSelectDirect:
    def test_taken_counts_toward_diversity(self):
        bounds = tg.tier_bounds(52.5)
        pool = tg.build_candidates([CATALOG[0]], bounds)
        zul = [c for c in pool if c["encounter"] == "zulrah"]
        picks, _ = tg.select(pool, count=5, mix={"air": 1, "water": 1, "earth": 1, "fire": 1},
                             rng=random.Random(1), taken=zul[:2])
        assert not [p for p in picks if p["encounter"] == "zulrah"]
