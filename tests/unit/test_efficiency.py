"""EHP/EHB: our port of Wise Old Man's algorithm against WOM's own numbers,
and the reader for their rate files."""
import json
from pathlib import Path

import pytest

from utils import efficiency as eff
from utils import js_literal

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "efficiency_parity.json").read_text())
WOM_TYPE = {"regular": None, "ironman": "ironman", "hardcore": "hardcore_ironman",
            "ultimate": "ultimate_ironman", "unknown": None}


class TestTables:
    def test_every_variant_loads_and_scores(self):
        doc = eff.tables()
        assert set(doc["ehp"]) == {"main", "ironman", "ultimate", "lvl3", "f2p", "f2p_lvl3",
                                   "f2p_ironman", "f2p_lvl3_ironman", "def1"}
        assert set(doc["ehb"]) == {"main", "ironman", "ultimate", "def1"}
        for kind in doc["ehp"]:
            algo = eff.algorithm(kind)
            maxed = {skill: eff.MAX_SKILL_EXP for skill in algo.real_skills}
            top = algo.calculate_ehp(maxed)
            assert algo.calculate_ehp({}) == 0
            assert 5_000 < top < 200_000
            assert top == pytest.approx(algo.maximum_ehp[eff.OVERALL], rel=1e-9)

    def test_more_xp_is_never_fewer_hours(self):
        algo = eff.algorithm("main")
        low = {skill: 1_000_000 for skill in algo.real_skills}
        high = {**low, "slayer": 20_000_000}
        assert algo.calculate_ehp(high) > algo.calculate_ehp(low)

    def test_boss_rates(self):
        rates = eff.ehb_rates("main")
        assert rates["zulrah"] > 0 and all(rate > 0 for rate in rates.values())
        assert eff.ehb_rates("lvl3") == {}
        assert eff.algorithm("main").calculate_ehb({"zulrah": rates["zulrah"] * 2}) == pytest.approx(2)
        assert eff.algorithm("main").calculate_ehb({"zulrah": -1, "not_a_boss": 50}) == 0

    def test_the_source_and_licence_are_recorded(self):
        source = eff.tables()["source"]
        assert len(source["commit"]) == 40 and "MIT" in source["license"]


class TestParityWithWiseOldMan:
    """200 real players. Our figure must equal the one WOM computed."""

    @pytest.fixture(autouse=True)
    def _same_rates(self):
        if eff.tables()["source"]["commit"] != FIXTURE["rates_commit"]:
            pytest.skip("rates have been synced past the fixture; refresh it with "
                        "scripts/sync_wom_efficiency.py --parity <wom group id> --write-fixture")

    def test_ehp_and_ehb_match_for_every_player(self):
        wrong = []
        for i, player in enumerate(FIXTURE["players"]):
            got = eff.player_efficiency(player["skills"], player["kc"],
                                        account_type=WOM_TYPE[player["type"]], build=player["build"])
            if abs(got["ehp"] - player["ehp"]) > 1e-4 or abs(got["ehb"] - player["ehb"]) > 1e-4:
                wrong.append((i, player["type"], player["build"], got["ehp"], player["ehp"],
                              got["ehb"], player["ehb"]))
        assert wrong == []

    def test_we_work_out_the_same_build(self):
        wrong = [(i, p["build"], eff.player_build(p["skills"], p["kc"]))
                 for i, p in enumerate(FIXTURE["players"])
                 if eff.player_build(p["skills"], p["kc"]) != p["build"]]
        assert wrong == []

    def test_the_fixture_covers_more_than_mains(self):
        kinds = {eff.algorithm_type(WOM_TYPE[p["type"]], p["build"]) for p in FIXTURE["players"]}
        assert {"main", "ironman", "ultimate", "def1"} <= kinds and len(FIXTURE["players"]) == 200


class TestAlgorithmChoice:
    @pytest.mark.parametrize("account_type,build,expected", [
        (None, "main", "main"), ("normal", "main", "main"),
        ("ironman", "main", "ironman"), ("hardcore_ironman", "main", "ironman"),
        ("ultimate_ironman", "main", "ultimate"), ("ultimate_ironman", "f2p", "f2p_ironman"),
        ("ironman", "f2p_lvl3", "f2p_lvl3_ironman"), ("ironman", "def1", "ironman"),
        (None, "def1", "def1"), (None, "lvl3", "lvl3"), (None, "f2p", "f2p"),
        (None, "zerker", "main"), (None, "hp10", "main"),
        # WOM has no group ironman type: they are scored as regular accounts.
        ("group_ironman", "main", "main"), ("hardcore_group_ironman", "main", "main"),
    ])
    def test_matches_woms_rules(self, account_type, build, expected):
        assert eff.algorithm_type(account_type, build) == expected

    def test_builds(self):
        fresh = {"hitpoints": 1154}
        assert eff.player_build(fresh, {}) == "f2p_lvl3"
        assert eff.player_build({**fresh, "slayer": 500}, {}) == "lvl3"
        pure = {"attack": 1_000_000, "strength": 5_000_000, "hitpoints": 3_000_000, "defence": 0,
                "ranged": 1_000_000, "magic": 1_000_000, "prayer": 20_000, "agility": 5_000}
        assert eff.player_build(pure, {}) == "def1"
        assert eff.player_build({**pure, "defence": 60_000}, {}) == "zerker"
        assert eff.player_build({**pure, "defence": 5_000_000}, {}) == "main"
        f2p = {k: v for k, v in pure.items() if k != "agility"}
        assert eff.player_build(f2p, {"obor": 50}) == "f2p"
        assert eff.player_build(f2p, {"zulrah": 1}) == "def1"
        assert eff.player_build(f2p, {}, fake_f2p=True) == "def1"

    def test_our_skill_names_are_translated(self):
        assert eff.from_our_skills({"runecraft": 5, "attack": 1}) == {"runecrafting": 5, "attack": 1}

    def test_rounding_is_javascripts(self):
        assert eff.round_number(0.000005, 5) == 0.00001
        assert eff.round_number(1.234564, 5) == 1.23456


class TestJsLiteral:
    def test_reads_a_rate_file(self):
        source = """
        import { Skill } from '../types';
        // comment
        export default [
          { skill: Skill.ATTACK, methods: [{ startExp: 1_210_421, rate: 315_000,
            description: "Chinning in Kruk's Dungeon" }, { startExp: 0, rate: 3.5, realRate: 1e3 }],
            bonuses: [{ originSkill: Skill.DEFENCE, end: false, ratio: 1.25, }], },
          /* block */ { 'quoted': 'it\\'s: fine', flag: true, none: null }
        ];
        """
        value, _ = js_literal.find_literal(source, "export default")
        assert value[0]["skill"] == "Skill.ATTACK"
        assert value[0]["methods"][0] == {"startExp": 1210421, "rate": 315000,
                                          "description": "Chinning in Kruk's Dungeon"}
        assert value[0]["methods"][1]["realRate"] == 1000.0
        assert value[0]["bonuses"][0] == {"originSkill": "Skill.DEFENCE", "end": False, "ratio": 1.25}
        assert value[1] == {"quoted": "it's: fine", "flag": True, "none": None}

    def test_reads_an_enum_and_computed_keys(self):
        enum, _ = js_literal.find_literal("export const Boss = { A_B: 'a_b', C: 'c' } as const;",
                                          "export const Boss =")
        assert enum == {"A_B": "a_b", "C": "c"}
        props, _ = js_literal.find_literal("x = mapValues({ [Skill.A]: { name: 'A', isMembers: true } }, f)",
                                           "= mapValues(")
        assert props == {"Skill.A": {"name": "A", "isMembers": True}}

    @pytest.mark.parametrize("source", ["[run()]", "[...other]", "{ a: `tpl` }", "[1, 2", "{ a: 1 } trailing"])
    def test_refuses_anything_that_is_not_data(self, source):
        with pytest.raises(ValueError):
            js_literal.parse(source)


class TestSyncBuildsTheSameTables:
    def test_validation_refuses_a_truncated_table(self):
        import importlib

        sync = importlib.import_module("scripts.sync_wom_efficiency")
        doc = json.loads(json.dumps({k: v for k, v in eff.tables().items() if k != "source"}))
        sync.validate(doc)
        doc["ehb"]["main"] = doc["ehb"]["main"][:5]
        with pytest.raises(ValueError):
            sync.validate(doc)

    def test_change_summary_names_the_boss_rates_that_moved(self):
        import importlib

        sync = importlib.import_module("scripts.sync_wom_efficiency")
        old = {k: v for k, v in eff.tables().items() if k != "source"}
        new = json.loads(json.dumps(old))
        new["ehb"]["main"][0]["rate"] += 1
        boss = new["ehb"]["main"][0]["boss"]
        lines = sync.describe_change(old, new)
        assert len(lines) == 1 and boss in lines[0] and lines[0].startswith("ehb/main")
        assert sync.describe_change(old, old) == []
