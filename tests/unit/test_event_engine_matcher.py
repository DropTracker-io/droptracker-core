"""Unit tests for the pure matcher layer of services/event_engine.py (Task 17).

The engine module is loaded directly from its file path (its module-level
imports are stdlib + sqlalchemy.exc only) so the conftest sys.modules stubs
for db/redis/services never interfere.
"""

import importlib.util
import os
import sys

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_under_test", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_under_test"] = engine  # dataclass needs the registry
_spec.loader.exec_module(engine)


def _task(**kw):
    base = {
        "id": 1, "event_id": 10, "type": "item_collection", "label": "t",
        "target": None, "target_value": None, "points": 0,
        "requires_confirmation": False, "config": {},
    }
    base.update(kw)
    return base


def _env(kind, data, guid="g-1", player_id=5, ts=1751600000):
    return {"v": 1, "kind": kind, "guid": guid, "player_id": player_id,
            "ts": ts, "data": data}


# ── item_collection ───────────────────────────────────────────────────────────

class TestItemCollection:
    def test_exact_target_match_drop(self):
        t = _task(target="Abyssal whip", target_value=1)
        m = engine.match_task(t, _env("drop", {"item_name": "Abyssal whip", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Abyssal whip"}

    def test_case_and_whitespace_insensitive(self):
        t = _task(target="Abyssal Whip")
        m = engine.match_task(t, _env("drop", {"item_name": "  abyssal   WHIP ", "quantity": 2}))
        assert m == {"mode": "count", "quantity": 2, "matched_target": "abyssal   WHIP"}

    def test_clog_matches_with_quantity_one(self):
        t = _task(target="Dragon pickaxe")
        m = engine.match_task(t, _env("clog", {"item_name": "dragon pickaxe", "kc": 100}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "dragon pickaxe"}

    def test_non_matching_item(self):
        t = _task(target="Abyssal whip")
        assert engine.match_task(t, _env("drop", {"item_name": "Dragon whip"})) is None

    def test_wrong_kind_no_match(self):
        t = _task(target="Abyssal whip")
        assert engine.match_task(t, _env("pb", {"item_name": "Abyssal whip"})) is None

    def test_any_of_config_list_of_strings(self):
        t = _task(config={"kind": "any_of", "any_of": ["Bandos chestplate", "Bandos tassets"]})
        m = engine.match_task(t, _env("drop", {"item_name": "bandos tassets", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "bandos tassets"}

    def test_all_of_config_items_best_effort(self):
        t = _task(config={"kind": "all_of", "items": [
            {"item_name": "Ahrim's staff", "quantity": 1},
            {"item_name": "Ahrim's hood", "quantity": 1},
        ]}, target_value=2)
        m = engine.match_task(t, _env("drop", {"item_name": "AHRIM'S HOOD", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "AHRIM'S HOOD"}

    def test_point_collection_credits_item_points(self):
        t = _task(config={"kind": "point_collection", "items": [
            {"item_name": "Ahrim's staff", "points": 2.0},
            {"item_name": "Dharok's greataxe", "points": 4.5},
        ]}, target_value=10)
        m = engine.match_task(t, _env("drop", {"item_name": "dharok's greataxe", "quantity": 2}))
        assert m == {"mode": "count", "quantity": 9, "matched_target": "dharok's greataxe"}  # round(4.5 * 2)

    def test_assembly_config_matches_listed_items(self):
        t = _task(config={"kind": "assembly", "items": [{"item_name": "Godsword shard 1"}]})
        m = engine.match_task(t, _env("drop", {"item_name": "Godsword shard 1"}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Godsword shard 1"}

    def test_drop_quantity_folded(self):
        t = _task(target="Cannonball")
        m = engine.match_task(t, _env("drop", {"item_name": "Cannonball", "quantity": 250}))
        assert m == {"mode": "count", "quantity": 250, "matched_target": "Cannonball"}


class TestItemSourceRestriction:
    """Optional source-NPC restriction (config.source_npcs single / config.item_npcs
    per-item): a restricted item only credits from a DROP by a listed NPC; a clog
    (source string, not a "drop from this NPC") never satisfies a restricted item."""

    # ── single item: config.source_npcs ──
    def _single(self, **kw):
        return _task(target="Twisted bow", target_value=1,
                     config={"source_npcs": ["Chambers of Xeric"]}, **kw)

    def test_single_drop_from_allowed_npc_matches(self):
        m = engine.match_task(self._single(), _env("drop", {
            "item_name": "Twisted bow", "npc_name": "chambers of xeric", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Twisted bow"}

    def test_single_drop_from_other_npc_no_match(self):
        assert engine.match_task(self._single(), _env("drop", {
            "item_name": "Twisted bow", "npc_name": "Zulrah", "quantity": 1})) is None

    def test_single_clog_never_matches_when_restricted(self):
        assert engine.match_task(self._single(), _env("clog", {
            "item_name": "Twisted bow", "npc_name": "Chambers of Xeric"})) is None

    def test_unrestricted_single_clog_still_matches(self):
        t = _task(target="Twisted bow", target_value=1, config={})
        assert engine.match_task(t, _env("clog", {"item_name": "Twisted bow"})) == {
            "mode": "count", "quantity": 1, "matched_target": "Twisted bow"}

    # ── multi item: config.item_npcs (flat per-item map) ──
    def _list(self, **kw):
        return _task(config={
            "kind": "any_of",
            "items": [{"item_name": "Twisted bow"}, {"item_name": "Kodai insignia"}],
            "item_npcs": {"Twisted bow": ["Chambers of Xeric"]},
        }, target_value=1, **kw)

    def test_list_restricted_item_from_allowed_npc(self):
        m = engine.match_task(self._list(), _env("drop", {
            "item_name": "Twisted bow", "npc_name": "Chambers of Xeric", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Twisted bow"}

    def test_list_restricted_item_from_other_npc_no_match(self):
        assert engine.match_task(self._list(), _env("drop", {
            "item_name": "Twisted bow", "npc_name": "Vorkath", "quantity": 1})) is None

    def test_list_unrestricted_item_from_any_npc(self):
        # Kodai insignia carries no item_npcs entry -> any source counts.
        m = engine.match_task(self._list(), _env("drop", {
            "item_name": "Kodai insignia", "npc_name": "Vorkath", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Kodai insignia"}

    def test_list_restricted_item_clog_no_match(self):
        assert engine.match_task(self._list(), _env("clog", {
            "item_name": "Twisted bow", "npc_name": "Chambers of Xeric"})) is None

    def test_list_unrestricted_item_clog_still_matches(self):
        assert engine.match_task(self._list(), _env("clog", {
            "item_name": "Kodai insignia"})) == {
            "mode": "count", "quantity": 1, "matched_target": "Kodai insignia"}

    # ── precomputed (state-load) frozensets are honored as-is ──
    def test_precomputed_index_honored(self):
        t = _task(config={"kind": "any_of", "items": [{"item_name": "Twisted bow"}]},
                  item_source_index={"twisted bow": frozenset({"chambers of xeric"})},
                  task_source_npcs=frozenset())
        assert engine.match_task(t, _env("drop", {
            "item_name": "Twisted bow", "npc_name": "Chambers of Xeric"})) == {
            "mode": "count", "quantity": 1, "matched_target": "Twisted bow"}
        assert engine.match_task(t, _env("drop", {
            "item_name": "Twisted bow", "npc_name": "Zulrah"})) is None

    def test_precomputed_task_source_npcs_for_single(self):
        t = _task(target="Twisted bow", target_value=1, config={},
                  item_source_index={}, task_source_npcs=frozenset({"chambers of xeric"}))
        assert engine.match_task(t, _env("drop", {
            "item_name": "Twisted bow", "npc_name": "Chambers of Xeric"}))["quantity"] == 1
        assert engine.match_task(t, _env("drop", {
            "item_name": "Twisted bow", "npc_name": "Kraken"})) is None

    def test_malformed_item_npcs_value_does_not_crash(self):
        # A non-list npcs value (a bad seed/template/DB-edited config) must not
        # raise during matching and leaves the item UNRESTRICTED, rather than
        # wedging the consumer or silently never crediting.
        for bad in ("Chambers of Xeric", 5, {"x": 1}):
            t = _task(config={"kind": "any_of", "items": [{"item_name": "Twisted bow"}],
                              "item_npcs": {"Twisted bow": bad}})
            m = engine.match_task(t, _env("drop", {
                "item_name": "Twisted bow", "npc_name": "Vorkath", "quantity": 1}))
            assert m == {"mode": "count", "quantity": 1, "matched_target": "Twisted bow"}, bad


# ── kc_target ─────────────────────────────────────────────────────────────────

class TestKcTarget:
    def test_matching_npc_drop(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50)
        m = engine.match_task(t, _env("drop", {"item_name": "x", "npc_name": "zulrah", "kill_count": 12}))
        assert m == {"mode": "kc", "quantity": 1}

    def test_wrong_npc(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50)
        assert engine.match_task(t, _env("drop", {"npc_name": "Vorkath"})) is None

    def test_clog_does_not_count_kc(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50)
        assert engine.match_task(t, _env("clog", {"npc_name": "Zulrah"})) is None

    def test_missing_target_never_matches(self):
        t = _task(type="kc_target", target=None, target_value=50)
        assert engine.match_task(t, _env("drop", {"npc_name": ""})) is None


class TestKcTargetMultiNpc:
    """config.npcs extends the target — a kill of ANY listed NPC counts."""

    def _dks(self, **kw):
        return _task(
            type="kc_target", target="Dagannoth Rex", target_value=50,
            config={"npcs": ["Dagannoth Rex", "Dagannoth Prime", "Dagannoth Supreme"]},
            **kw)

    def test_any_listed_npc_matches(self):
        t = self._dks()
        for npc in ("Dagannoth Rex", "dagannoth PRIME", "Dagannoth Supreme"):
            m = engine.match_task(t, _env("drop", {"npc_name": npc, "kill_count": 3}))
            assert m == {"mode": "kc", "quantity": 1}, npc

    def test_unlisted_npc_no_match(self):
        assert engine.match_task(
            self._dks(), _env("drop", {"npc_name": "Zulrah"})) is None

    def test_precomputed_kc_npcs_wins_over_config(self):
        # State-load dicts carry kc_npcs; the matcher must honor them as-is.
        t = _task(type="kc_target", target="Dagannoth Rex", target_value=50,
                  kc_npcs=["dagannoth rex", "dagannoth prime"])
        assert engine.match_task(
            t, _env("drop", {"npc_name": "Dagannoth Prime"})) == {"mode": "kc", "quantity": 1}
        assert engine.match_task(
            t, _env("drop", {"npc_name": "Dagannoth Supreme"})) is None

    def test_state_scope_single_npc_is_bare_task_id(self):
        # Legacy key shape: deployed single-NPC watermarks must survive.
        t = _task(id=42, type="kc_target", target="Zulrah")
        assert engine._kc_state_scope(t, "zulrah") == 42

    def test_state_scope_multi_npc_is_per_npc(self):
        t = self._dks()
        t["id"] = 42
        assert engine._kc_state_scope(t, "dagannoth prime") == "42:dagannoth_prime"
        assert (engine._kc_state_scope(t, "dagannoth rex")
                != engine._kc_state_scope(t, "dagannoth prime"))

    def test_wom_kc_matches_any_precomputed_metric(self):
        t = self._dks(wom_metrics={"dagannoth_rex": "dagannoth rex",
                                   "dagannoth_prime": "dagannoth prime"})
        m = engine.match_task(t, _env("wom_kc", {"boss_metric": "dagannoth_prime", "kc": 120}))
        assert m == {"mode": "kc_abs", "quantity": 0}
        assert engine.match_task(
            t, _env("wom_kc", {"boss_metric": "dagannoth_supreme", "kc": 9})) is None


# ── pb_target ─────────────────────────────────────────────────────────────────

class TestPbTarget:
    def test_time_under_target_matches(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)  # 60s
        m = engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 59_400}))
        assert m == {"mode": "first", "quantity": 1}

    def test_time_equal_target_matches(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)
        m = engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 60_000}))
        assert m == {"mode": "first", "quantity": 1}

    def test_time_over_target_no_match(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)
        assert engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 60_001})) is None

    def test_zero_time_no_match(self):
        t = _task(type="pb_target", target="Zulrah", target_value=60)
        assert engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 0})) is None

    def test_no_target_value_no_match(self):
        t = _task(type="pb_target", target="Zulrah", target_value=None)
        assert engine.match_task(t, _env("pb", {"npc_name": "Zulrah", "time_ms": 1000})) is None


# ── xp_target / skill_target ──────────────────────────────────────────────────

class TestExperienceTargets:
    def test_xp_target_matches_skill(self):
        t = _task(type="xp_target", target="Slayer", target_value=1_000_000)
        m = engine.match_task(t, _env("experience", {"skill": "slayer", "xp": 5_000_000, "level": 90}))
        assert m == {"mode": "xp", "quantity": 0}

    def test_xp_target_wrong_skill(self):
        t = _task(type="xp_target", target="Slayer", target_value=1_000_000)
        assert engine.match_task(t, _env("experience", {"skill": "attack", "xp": 1})) is None

    def test_skill_target_level_reached(self):
        t = _task(type="skill_target", target="Agility", target_value=90)
        m = engine.match_task(t, _env("experience", {"skill": "Agility", "xp": 1, "level": 90}))
        assert m == {"mode": "first", "quantity": 1}

    def test_skill_target_level_below(self):
        t = _task(type="skill_target", target="Agility", target_value=90)
        assert engine.match_task(t, _env("experience", {"skill": "Agility", "level": 89})) is None

    def test_skill_target_ignores_drops(self):
        t = _task(type="skill_target", target="Agility", target_value=90)
        assert engine.match_task(t, _env("drop", {"skill": "Agility", "level": 99})) is None


# ── loot_value ────────────────────────────────────────────────────────────────

class TestLootValue:
    def test_any_source_folds_total_value(self):
        t = _task(type="loot_value", target_value=10_000_000)
        m = engine.match_task(t, _env("drop", {"item_name": "Coins", "npc_name": "Zulrah",
                                               "total_value": 250_000}))
        assert m == {"mode": "count", "quantity": 250_000}

    def test_target_npc_scopes_credit(self):
        t = _task(type="loot_value", target="Zulrah", target_value=10_000_000)
        env = {"item_name": "x", "npc_name": "Vorkath", "total_value": 100}
        assert engine.match_task(t, _env("drop", env)) is None
        env["npc_name"] = "zulrah"
        assert engine.match_task(t, _env("drop", env)) == {"mode": "count", "quantity": 100}

    def test_config_source_npcs_scope(self):
        t = _task(type="loot_value", target_value=1_000,
                  config={"source_npcs": ["Zulrah", "Vorkath"]})
        assert engine.match_task(t, _env("drop", {"npc_name": "Vorkath", "total_value": 5})) \
            == {"mode": "count", "quantity": 5}
        assert engine.match_task(t, _env("drop", {"npc_name": "Kraken", "total_value": 5})) is None

    def test_zero_value_and_wrong_kind_no_match(self):
        t = _task(type="loot_value", target_value=1_000)
        assert engine.match_task(t, _env("drop", {"npc_name": "Zulrah", "total_value": 0})) is None
        assert engine.match_task(t, _env("clog", {"npc_name": "Zulrah", "total_value": 50})) is None


# ── non-evaluated types ───────────────────────────────────────────────────────

class TestManualOnlyTypes:
    def test_ehp_ehb_custom_never_match(self):
        for task_type in ("ehp_target", "ehb_target", "custom"):
            t = _task(type=task_type, target="anything", target_value=1)
            for kind in ("drop", "pb", "clog", "ca", "experience"):
                assert engine.match_task(t, _env(kind, {"item_name": "anything",
                                                        "npc_name": "anything",
                                                        "skill": "anything",
                                                        "level": 99})) is None


# ── thresholds & config parsing ───────────────────────────────────────────────

class TestHelpers:
    def test_threshold_defaults_to_one(self):
        assert engine.completion_threshold(_task(target_value=None)) == 1
        assert engine.completion_threshold(_task(target_value=0)) == 1

    def test_threshold_uses_target_value(self):
        assert engine.completion_threshold(_task(type="kc_target", target_value=50)) == 50
        assert engine.completion_threshold(_task(type="xp_target", target_value=13_034_431)) == 13_034_431

    def test_first_match_types_threshold_one(self):
        assert engine.completion_threshold(_task(type="pb_target", target_value=60)) == 1
        assert engine.completion_threshold(_task(type="skill_target", target_value=99)) == 1

    def test_parse_task_config_variants(self):
        assert engine.parse_task_config(None) == {}
        assert engine.parse_task_config("") == {}
        assert engine.parse_task_config("not json") == {}
        assert engine.parse_task_config('{"kind": "any_of"}') == {"kind": "any_of"}
        assert engine.parse_task_config({"kind": "all_of"}) == {"kind": "all_of"}
        assert engine.parse_task_config("[1, 2]") == {}

    def test_item_match_quantity_none_for_missing_name(self):
        assert engine.item_match_quantity(_task(target="Abyssal whip"), None) is None
        assert engine.item_match_quantity(_task(target="Abyssal whip"), "") is None


# ── all_of / assembly distinct-item progress ─────────────────────────────────

class _Row:
    """Minimal EventCompletion stand-in for the pure rollup."""
    def __init__(self, matched_target=None, quantity=1, source_type="drop",
                 note=None, player_id=None):
        self.matched_target = matched_target
        self.quantity = quantity
        self.source_type = source_type
        self.note = note
        self.player_id = player_id


class TestDistinctItemProgress:
    def test_quantity_does_not_inflate_progress(self):
        # The original bug: one 1,338-coins drop completed a 3-item collect-all.
        rows = [_Row("Coins", quantity=1338)]
        assert engine._distinct_progress_from_rows(rows, threshold=3) == 1

    def test_distinct_items_counted_once_each(self):
        rows = [
            _Row("Bones", quantity=5),
            _Row("Coins", quantity=1338),
            _Row("bones", quantity=2),   # same item, different casing
        ]
        assert engine._distinct_progress_from_rows(rows, threshold=3) == 2

    def test_completes_with_all_items(self):
        rows = [_Row("Bones"), _Row("Coins"), _Row("Bronze axe")]
        assert engine._distinct_progress_from_rows(rows, threshold=3) == 3

    def test_manual_wildcard_rows_fill_by_quantity(self):
        # "Mark complete" awards have no matched item; their quantity fills.
        rows = [_Row("Bones"), _Row(None, quantity=2, source_type="manual")]
        assert engine._distinct_progress_from_rows(rows, threshold=3) == 3

    def test_wildcards_capped_at_threshold(self):
        rows = [_Row(None, quantity=50, source_type="manual"), _Row("Bones")]
        assert engine._distinct_progress_from_rows(rows, threshold=3) == 3

    def test_bonus_rows_ignored(self):
        rows = [_Row(None, quantity=10, source_type="bonus")]
        assert engine._distinct_progress_from_rows(rows, threshold=3) == 0

    def test_list_kind_helper(self):
        assert engine._list_kind(_task(config={"kind": "all_of"})) == "all_of"
        assert engine._list_kind(_task(config={})) is None
        assert engine._list_kind(_task(config=None)) is None


# ── grouped (all-of + any-of) progress ───────────────────────────────────────

GODSWORD = {
    "kind": "groups",
    "groups": [
        {"mode": "all_of",
         "items": ["Godsword shard 1", "Godsword shard 2", "Godsword shard 3"]},
        {"mode": "any_of", "need": 1,
         "items": ["Armadyl hilt", "Bandos hilt", "Saradomin hilt", "Zamorak hilt"]},
    ],
}
# Threshold = 3 (all shards) + 1 (any hilt).
GODSWORD_THRESHOLD = 4


class TestGroupedItemProgress:
    def test_groups_items_are_matchable(self):
        task = _task(config=GODSWORD)
        assert engine.item_match_quantity(task, "Godsword shard 2") == 1
        assert engine.item_match_quantity(task, "bandos hilt", 1) == 1
        assert engine.item_match_quantity(task, "Abyssal whip") is None

    def test_all_shards_without_hilt_is_incomplete(self):
        rows = [_Row("Godsword shard 1"), _Row("Godsword shard 2"), _Row("Godsword shard 3")]
        assert engine._grouped_progress_from_rows(rows, GODSWORD, GODSWORD_THRESHOLD) == 3

    def test_two_hilts_only_fill_the_hilt_group_once(self):
        rows = [_Row("Armadyl hilt"), _Row("Zamorak hilt")]
        assert engine._grouped_progress_from_rows(rows, GODSWORD, GODSWORD_THRESHOLD) == 1

    def test_duplicate_shards_do_not_inflate(self):
        rows = [_Row("Godsword shard 1", quantity=3), _Row("Godsword shard 1")]
        assert engine._grouped_progress_from_rows(rows, GODSWORD, GODSWORD_THRESHOLD) == 1

    def test_complete_godsword(self):
        rows = [
            _Row("Godsword shard 1"), _Row("Godsword shard 2"),
            _Row("Godsword shard 3"), _Row("Saradomin hilt"),
        ]
        assert engine._grouped_progress_from_rows(rows, GODSWORD, GODSWORD_THRESHOLD) == 4

    def test_any_of_group_folds_quantities(self):
        config = {
            "kind": "groups",
            "groups": [{"mode": "any_of", "need": 2,
                        "items": ["Boater", "Red boater", "Orange boater"]}],
        }
        assert engine._grouped_progress_from_rows([_Row("Boater")], config, 2) == 1
        assert engine._grouped_progress_from_rows(
            [_Row("Boater"), _Row("Boater")], config, 2) == 2
        assert engine._grouped_progress_from_rows(
            [_Row("Red boater", quantity=2)], config, 2) == 2

    def test_wildcard_manual_awards_fill_any_group(self):
        rows = [_Row(None, quantity=4, source_type="manual")]
        assert engine._grouped_progress_from_rows(rows, GODSWORD, GODSWORD_THRESHOLD) == 4

    def test_bonus_rows_ignored(self):
        rows = [_Row(None, quantity=10, source_type="bonus")]
        assert engine._grouped_progress_from_rows(rows, GODSWORD, GODSWORD_THRESHOLD) == 0


# ── any_path (either-or "dryness protection") progress ───────────────────────

# Suggestion #52's motivating case: the full Justiciar set OR any 5
# Justiciar items (duplicates folding). Threshold is a percentage.
JUSTICIAR = {
    "kind": "any_path",
    "paths": [
        {"label": "Full set",
         "groups": [{"mode": "all_of",
                     "items": ["Justiciar faceguard", "Justiciar chestguard",
                               "Justiciar legguards"]}]},
        {"label": "Any 5 pieces",
         "groups": [{"mode": "any_of", "need": 5,
                     "items": ["Justiciar faceguard", "Justiciar chestguard",
                               "Justiciar legguards"]}]},
    ],
}
ANY_PATH_THRESHOLD = 100


class TestAnyPathProgress:
    def test_path_items_are_matchable(self):
        task = _task(config=JUSTICIAR)
        assert engine.item_match_quantity(task, "Justiciar faceguard") == 1
        assert engine.item_match_quantity(task, "justiciar  LEGGUARDS", 2) == 2
        assert engine.item_match_quantity(task, "Abyssal whip") is None

    def test_progress_is_the_closest_path_percentage(self):
        # Two distinct pieces: full-set path 2/3 (66%) beats any-5 at 2/5 (40%).
        rows = [_Row("Justiciar faceguard"), _Row("Justiciar chestguard")]
        assert engine._anypath_progress_from_rows(
            rows, JUSTICIAR, ANY_PATH_THRESHOLD) == 66

    def test_full_set_completes(self):
        rows = [_Row("Justiciar faceguard"), _Row("Justiciar chestguard"),
                _Row("Justiciar legguards")]
        assert engine._anypath_progress_from_rows(
            rows, JUSTICIAR, ANY_PATH_THRESHOLD) == 100

    def test_duplicates_complete_via_the_any_of_path(self):
        # 5× the same piece never finishes the set, but the any-5 path folds
        # quantities — this is exactly the dryness-protection semantics.
        rows = [_Row("Justiciar faceguard", quantity=5)]
        assert engine._anypath_progress_from_rows(
            rows, JUSTICIAR, ANY_PATH_THRESHOLD) == 100

    def test_one_drop_advances_every_path_that_lists_it(self):
        rows = [_Row("Justiciar faceguard", quantity=4)]
        # Full set: 1/3 → 33%; any-5: 4/5 → 80%.
        assert engine._anypath_progress_from_rows(
            rows, JUSTICIAR, ANY_PATH_THRESHOLD) == 80

    def test_never_completes_one_drop_early(self):
        rows = [_Row("Justiciar faceguard", quantity=4)]
        assert engine._anypath_progress_from_rows(
            rows, JUSTICIAR, ANY_PATH_THRESHOLD) < 100

    def test_wildcard_manual_awards_advance_paths(self):
        rows = [_Row(None, quantity=3, source_type="manual")]
        assert engine._anypath_progress_from_rows(
            rows, JUSTICIAR, ANY_PATH_THRESHOLD) == 100

    def test_bonus_rows_ignored(self):
        rows = [_Row(None, quantity=10, source_type="bonus")]
        assert engine._anypath_progress_from_rows(
            rows, JUSTICIAR, ANY_PATH_THRESHOLD) == 0

    def test_empty_or_garbage_paths_yield_zero(self):
        assert engine._anypath_progress_from_rows(
            [_Row("Justiciar faceguard")], {"kind": "any_path", "paths": []},
            ANY_PATH_THRESHOLD) == 0
        assert engine._anypath_progress_from_rows(
            [_Row("Justiciar faceguard")],
            {"kind": "any_path", "paths": ["nonsense", {"groups": []}]},
            ANY_PATH_THRESHOLD) == 0


# ── any_path metric paths (v2: "boss pet OR 5,000 KC / 10M GP") ──────────────

GWD_OR = {
    "kind": "any_path",
    "paths": [
        {"label": "Any GWD hilt",
         "groups": [{"mode": "any_of", "need": 1,
                     "items": ["Armadyl hilt", "Bandos hilt"]}]},
        {"label": "500 GWD kills", "metric": "kc",
         "npcs": ["Kree'arra", "General Graardor"], "need": 500},
        {"label": "10M from GWD", "metric": "loot_value",
         "npcs": ["Kree'arra", "General Graardor"], "need": 10_000_000},
    ],
}


class TestMetricPathMatching:
    def test_drop_from_listed_npc_matches_kc_and_gp_paths(self):
        t = _task(config=GWD_OR)
        matches = engine.match_task_all(t, _env("drop", {
            "item_name": "Rune platebody", "quantity": 1,
            "npc_name": "Kree'arra", "total_value": 39_000}))
        # Not a listed item → no item match; both metric paths credit.
        assert matches == [
            {"mode": "kc", "quantity": 1, "path": 1},
            {"mode": "count", "quantity": 39_000, "path": 2},
        ]

    def test_listed_item_drop_matches_all_three_paths(self):
        t = _task(config=GWD_OR)
        matches = engine.match_task_all(t, _env("drop", {
            "item_name": "Armadyl hilt", "quantity": 1,
            "npc_name": "Kree'arra", "total_value": 25_000_000}))
        assert [(m["mode"], m.get("path")) for m in matches] == [
            ("count", None), ("kc", 1), ("count", 2)]
        assert matches[0]["matched_target"] == "Armadyl hilt"

    def test_drop_from_unlisted_npc_ignores_metric_paths(self):
        t = _task(config=GWD_OR)
        assert engine.match_task_all(t, _env("drop", {
            "item_name": "Rune platebody", "quantity": 1,
            "npc_name": "Zulrah", "total_value": 39_000})) == []

    def test_zero_value_drop_still_counts_the_kill(self):
        t = _task(config=GWD_OR)
        assert engine.match_task_all(t, _env("drop", {
            "item_name": "Ashes", "quantity": 1,
            "npc_name": "General Graardor", "total_value": 0})) == [
            {"mode": "kc", "quantity": 1, "path": 1}]

    def test_unscoped_gp_path_takes_any_drop(self):
        cfg = {"kind": "any_path", "paths": [
            {"groups": [{"mode": "any_of", "need": 1, "items": ["Armadyl hilt"]}]},
            {"metric": "loot_value", "need": 1_000_000},
        ]}
        assert engine.match_task_all(_task(config=cfg), _env("drop", {
            "item_name": "Rune platebody", "npc_name": "Zulrah",
            "total_value": 5_000})) == [
            {"mode": "count", "quantity": 5_000, "path": 1}]

    def test_wom_kc_matches_kc_path_via_precomputed_metrics(self):
        # State-load dicts carry resolved wom metrics per kc path; the
        # reconciler's absolute-KC envelope folds through the path watermark.
        t = _task(config=GWD_OR, metric_paths=[
            {"idx": 1, "metric": "kc", "need": 500,
             "npcs": frozenset({"kree'arra", "general graardor"}),
             "wom_metrics": {"kreearra": "kree'arra"}},
            {"idx": 2, "metric": "loot_value", "need": 10_000_000,
             "npcs": frozenset({"kree'arra", "general graardor"})},
        ])
        assert engine.match_task_all(t, _env("wom_kc", {
            "boss_metric": "kreearra", "kc": 210})) == [
            {"mode": "kc_abs", "quantity": 0, "path": 1}]
        # A boss outside the path's metric map stays unmatched.
        assert engine.match_task_all(t, _env("wom_kc", {
            "boss_metric": "zulrah", "kc": 99})) == []

    def test_non_any_path_task_defers_to_single_match(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50)
        env = _env("drop", {"item_name": "x", "npc_name": "Zulrah"})
        assert engine.match_task_all(t, env) == [engine.match_task(t, env)]

    def test_pet_submission_credits_pet_flagged_item_path_only(self):
        cfg = {"kind": "any_path", "pet_items": ["Pet kree'arra"], "paths": [
            {"groups": [{"mode": "any_of", "need": 1, "items": ["Pet kree'arra"]}]},
            {"metric": "kc", "npcs": ["Kree'arra"], "need": 500},
        ]}
        matches = engine.match_task_all(
            _task(config=cfg), _env("pet", {"pet_name": "Pet kree'arra"}))
        assert len(matches) == 1
        assert matches[0]["mode"] == "count" and "path" not in matches[0]

    def test_match_kc_scope_is_per_path_and_npc(self):
        t = _task(config=GWD_OR)
        assert engine._match_kc_scope(
            t, {"mode": "kc", "path": 1}, "kree'arra") == "1:p1:kree'arra"
        # kc_target matches (no path) keep the legacy shapes.
        kc = _task(type="kc_target", target="Zulrah", config={})
        assert engine._match_kc_scope(kc, {"mode": "kc"}, "zulrah") == 1


class TestMetricPathProgress:
    def test_kc_rows_fold_into_their_own_path(self):
        rows = [_Row(None, quantity=250, note="path:1")]
        assert engine._anypath_progress_from_rows(rows, GWD_OR, ANY_PATH_THRESHOLD) == 50

    def test_metric_rows_never_leak_into_item_paths(self):
        # 3 kills = 0% (floored); a wildcard leak would score the 1-hilt path 100.
        rows = [_Row(None, quantity=3, note="path:1")]
        assert engine._anypath_progress_from_rows(rows, GWD_OR, ANY_PATH_THRESHOLD) == 0

    def test_item_rows_do_not_feed_metric_paths(self):
        cfg = {"kind": "any_path", "paths": [
            {"metric": "kc", "npcs": ["Kree'arra"], "need": 2},
            {"metric": "loot_value", "need": 1_000_000},
        ]}
        rows = [_Row("Armadyl hilt", quantity=5)]
        assert engine._anypath_progress_from_rows(rows, cfg, ANY_PATH_THRESHOLD) == 0

    def test_wildcard_awards_are_percent_points_on_metric_paths(self):
        # Manual awards carry no path tag; on the percent-scaled task they
        # advance metric paths as percent points — the admin "mark complete"
        # award (threshold − done) must finish a metric-only task.
        cfg = {"kind": "any_path", "paths": [
            {"metric": "kc", "npcs": ["Kree'arra"], "need": 5000},
            {"metric": "loot_value", "need": 1_000_000},
        ]}
        partial = [_Row(None, quantity=5, source_type="manual")]
        assert engine._anypath_progress_from_rows(partial, cfg, ANY_PATH_THRESHOLD) == 5
        complete = [_Row(None, quantity=2500, note="path:0"),   # 50% of the KC path
                    _Row(None, quantity=50, source_type="manual")]
        assert engine._anypath_progress_from_rows(complete, cfg, ANY_PATH_THRESHOLD) == 100

    def test_gp_path_completes_at_need(self):
        rows = [_Row(None, quantity=6_000_000, note="path:2"),
                _Row(None, quantity=4_000_000, note="path:2")]
        assert engine._anypath_progress_from_rows(rows, GWD_OR, ANY_PATH_THRESHOLD) == 100

    def test_mixed_progress_takes_the_closest_path(self):
        rows = [_Row("Bandos hilt"),                      # item path done (100)
                _Row(None, quantity=100, note="path:1")]  # kc 20%
        assert engine._anypath_progress_from_rows(rows, GWD_OR, ANY_PATH_THRESHOLD) == 100

    def test_untagged_wildcards_still_credit_item_paths(self):
        rows = [_Row(None, quantity=1, source_type="manual")]
        assert engine._anypath_progress_from_rows(rows, GWD_OR, ANY_PATH_THRESHOLD) == 100

    def test_bonus_rows_ignored_even_when_tagged(self):
        rows = [_Row(None, quantity=500, source_type="bonus", note="path:1")]
        assert engine._anypath_progress_from_rows(rows, GWD_OR, ANY_PATH_THRESHOLD) == 0

    def test_row_path_idx_parsing(self):
        assert engine._row_path_idx(_Row(note="path:2")) == 2
        assert engine._row_path_idx(_Row(note="path:x")) is None
        assert engine._row_path_idx(_Row(note="an admin note")) is None
        assert engine._row_path_idx(_Row()) is None


# ── DT2 vestige pity rolls (Gold ring counts as the vestige) ──────────────────

class TestVestigeRings:
    def test_ring_credits_target_vestige_one_unit_per_drop(self):
        # The second successful roll drops a 2-ring STACK — still one vestige.
        t = _task(target="Ultor vestige", target_value=1)
        m = engine.match_task(t, _env("drop", {
            "item_name": "Gold ring", "quantity": 2, "npc_name": "Vardorvis"}))
        assert m == {"mode": "count", "quantity": 1,
                     "matched_target": "Ultor vestige"}

    def test_ring_from_awakened_variant_counts(self):
        t = _task(target="Venator vestige")
        m = engine.match_task(t, _env("drop", {
            "item_name": "Gold ring", "quantity": 1,
            "npc_name": "Leviathan (Awakened)"}))
        assert m is not None and m["matched_target"] == "Venator vestige"

    def test_ring_from_the_wrong_boss_does_not_credit(self):
        t = _task(target="Ultor vestige")
        assert engine.match_task(t, _env("drop", {
            "item_name": "Gold ring", "quantity": 1,
            "npc_name": "The Leviathan"})) is None

    def test_ring_ignored_when_no_vestige_listed(self):
        t = _task(target="Abyssal whip")
        assert engine.match_task(t, _env("drop", {
            "item_name": "Gold ring", "quantity": 2, "npc_name": "Vardorvis"})) is None

    def test_listed_gold_ring_keeps_literal_stack_semantics(self):
        # A task genuinely about gold rings is untouched by the alias.
        t = _task(config={"kind": "any_of", "items": ["Gold ring"]}, target_value=5)
        m = engine.match_task(t, _env("drop", {
            "item_name": "Gold ring", "quantity": 2, "npc_name": "Vardorvis"}))
        assert m == {"mode": "count", "quantity": 2, "matched_target": "Gold ring"}

    def test_ring_credits_vestige_in_any_of_list_case_insensitive(self):
        t = _task(config={"kind": "any_of",
                          "items": ["Ultor vestige", "Magus vestige"]},
                  target_value=2)
        m = engine.match_task(t, _env("drop", {
            "item_name": "gold RING", "quantity": 2, "npc_name": "duke sucellus"}))
        assert m == {"mode": "count", "quantity": 1,
                     "matched_target": "Magus vestige"}

    def test_ring_credits_point_collection_at_the_vestige_weight(self):
        t = _task(config={"kind": "point_collection",
                          "items": [{"item_name": "Bellator vestige", "points": 40}]},
                  target_value=100)
        m = engine.match_task(t, _env("drop", {
            "item_name": "Gold ring", "quantity": 2, "npc_name": "The Whisperer"}))
        assert m == {"mode": "count", "quantity": 40,
                     "matched_target": "Bellator vestige"}

    def test_clog_gold_ring_never_aliases(self):
        # Rings have no vestige clog: only DROPS carry the pity signal.
        t = _task(target="Ultor vestige")
        assert engine.match_task(t, _env("clog", {
            "item_name": "Gold ring", "npc_name": "Vardorvis"})) is None

    def test_ring_credits_vestige_inside_any_path_item_path(self):
        cfg = {"kind": "any_path", "paths": [
            {"groups": [{"mode": "any_of", "need": 1, "items": ["Ultor vestige"]}]},
            {"metric": "kc", "npcs": ["Vardorvis"], "need": 500},
        ]}
        matches = engine.match_task_all(_task(config=cfg), _env("drop", {
            "item_name": "Gold ring", "quantity": 2, "npc_name": "Vardorvis",
            "total_value": 0}))
        assert [(m["mode"], m.get("path"), m.get("matched_target"))
                for m in matches] == [("count", None, "Ultor vestige"),
                                      ("kc", 1, None)]


# ── pb completion requirements (times / unique_players / whole_team) ─────────

class TestPbCompletionModes:
    def _pb_env(self, ms=65_000):
        return _env("pb", {"npc_name": "Zulrah", "time_ms": ms})

    def test_legacy_stays_first_mode(self):
        t = _task(type="pb_target", target="Zulrah", target_value=70, config={})
        assert engine.match_task(t, self._pb_env()) == {"mode": "first", "quantity": 1}

    def test_times_one_stays_first_mode(self):
        t = _task(type="pb_target", target="Zulrah", target_value=70,
                  config={"mode": "times", "need": 1})
        assert engine.match_task(t, self._pb_env()) == {"mode": "first", "quantity": 1}

    def test_counted_modes_are_count_matches(self):
        for cfg in ({"mode": "times", "need": 5},
                    {"mode": "unique_players", "need": 3},
                    {"mode": "whole_team"}):
            t = _task(type="pb_target", target="Zulrah", target_value=70, config=cfg)
            assert engine.match_task(t, self._pb_env()) == {"mode": "count", "quantity": 1}

    def test_slow_kill_still_no_match(self):
        t = _task(type="pb_target", target="Zulrah", target_value=70,
                  config={"mode": "times", "need": 5})
        assert engine.match_task(t, self._pb_env(ms=71_000)) is None

    def test_threshold_per_mode(self):
        assert engine.completion_threshold(
            {"type": "pb_target", "target_value": 70}) == 1
        assert engine.completion_threshold(
            {"type": "pb_target", "target_value": 70,
             "config": {"mode": "times", "need": 5}}) == 5
        assert engine.completion_threshold(
            {"type": "pb_target", "target_value": 70,
             "config": {"mode": "unique_players", "need": 4}}) == 4
        # whole_team resolves per team at apply time; the pure fallback is 1.
        assert engine.completion_threshold(
            {"type": "pb_target", "target_value": 70,
             "config": {"mode": "whole_team"}}) == 1

    def test_pb_mode_parses_string_configs_and_garbage(self):
        assert engine._pb_mode(
            {"config": '{"mode": "unique_players", "need": 3}'}) == ("unique_players", 3)
        assert engine._pb_mode({"config": None}) == ("times", 1)
        assert engine._pb_mode({"config": {"mode": "nonsense", "need": "x"}}) == ("times", 1)

    def test_distinct_players_fold(self):
        rows = [_Row(player_id=1), _Row(player_id=1), _Row(player_id=2),
                _Row(None, quantity=2, source_type="manual"),  # admin wildcard
                _Row(player_id=9, source_type="bonus")]
        assert engine._distinct_players_from_rows(rows, 10) == 4
        assert engine._distinct_players_from_rows(rows, 3) == 3


# ── kc dedupe: kill_count keying + cooldown fallback ─────────────────────────

class _FakeRedis:
    def __init__(self):
        self.sets = {}
        self.kv = {}

    def sadd(self, key, member):
        s = self.sets.setdefault(key, set())
        if member in s:
            return 0
        s.add(member)
        return 1

    def expire(self, key, ttl):
        return True

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)


class TestKcDedupe:
    def test_same_kill_count_counts_once(self):
        r = _FakeRedis()
        env = _env("drop", {"npc_name": "Zulrah", "kill_count": 12})
        assert engine._kc_dedupe(r, 2, 9, 5, env) is True
        assert engine._kc_dedupe(r, 2, 9, 5, env) is False  # second stack, same kill

    def test_new_kill_count_counts_again(self):
        r = _FakeRedis()
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah", "kill_count": 12})) is True
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah", "kill_count": 13})) is True

    def test_zero_kill_count_uses_cooldown_not_collapse(self):
        # The plugin sends 0 when KC is unavailable — two kills far apart must
        # BOTH count (0 must not dedupe them into one).
        r = _FakeRedis()
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah", "kill_count": 0}, ts=1000)) is True
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah", "kill_count": 0}, ts=1100)) is True

    def test_missing_kill_count_stacks_within_cooldown_count_once(self):
        # The original bug: one kill dropping 3 stacks (each its own guid,
        # 1-2s apart) counted as 3 kills.
        r = _FakeRedis()
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah"}, guid="a", ts=1000)) is True
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah"}, guid="b", ts=1001)) is False
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah"}, guid="c", ts=1002)) is False
        # ...and the next real kill (past the cooldown) counts.
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah"}, guid="d", ts=1065)) is True

    def test_string_kill_count_coerced(self):
        # Embed field values arrive as strings.
        r = _FakeRedis()
        env = _env("drop", {"npc_name": "Zulrah", "kill_count": "12"})
        assert engine._kc_dedupe(r, 2, 9, 5, env) is True
        assert engine._kc_dedupe(r, 2, 9, 5, env) is False

    def test_players_deduped_independently(self):
        r = _FakeRedis()
        assert engine._kc_dedupe(r, 2, 9, 5, _env("drop", {"npc_name": "Zulrah"}, ts=1000, player_id=5)) is True
        assert engine._kc_dedupe(r, 2, 9, 6, _env("drop", {"npc_name": "Zulrah"}, ts=1001, player_id=6)) is True


# ── submission_policy (api_only / confirm_non_api / all) ─────────────────────

def _event(**kw):
    base = {
        "id": 10, "name": "ev", "group_id": 1,
        "requires_confirmation": False, "submission_policy": "all",
        "has_bingo": False, "board_size": 5,
        "bonus_line_points": 0, "bonus_blackout_points": 0,
        "window_start": None, "window_end": None,
    }
    base.update(kw)
    return base


class TestSubmissionPolicy:
    def test_all_accepts_both_sources(self):
        ev = _event(submission_policy="all")
        assert engine.accepts_submission_source(ev, {"used_api": True}) is True
        assert engine.accepts_submission_source(ev, {"used_api": False}) is True
        assert engine.accepts_submission_source(ev, {}) is True  # legacy envelope

    def test_api_only_rejects_non_api(self):
        ev = _event(submission_policy="api_only")
        assert engine.accepts_submission_source(ev, {"used_api": True}) is True
        assert engine.accepts_submission_source(ev, {"used_api": False}) is False
        assert engine.accepts_submission_source(ev, {}) is False  # legacy envelope

    def test_confirm_non_api_accepts_but_holds(self):
        ev = _event(submission_policy="confirm_non_api")
        task = _task()
        assert engine.accepts_submission_source(ev, {"used_api": False}) is True
        assert engine.completion_status(ev, task, {"used_api": True}) == "auto"
        assert engine.completion_status(ev, task, {"used_api": False}) == "pending"
        assert engine.completion_status(ev, task, {}) == "pending"  # legacy envelope

    def test_requires_confirmation_still_forces_pending(self):
        env = {"used_api": True}
        assert engine.completion_status(
            _event(requires_confirmation=True), _task(), env) == "pending"
        assert engine.completion_status(
            _event(), _task(requires_confirmation=True), env) == "pending"

    def test_all_policy_auto_for_non_api(self):
        assert engine.completion_status(_event(), _task(), {"used_api": False}) == "auto"

    def test_handle_envelope_skips_api_only_event_for_non_api(self):
        # End-to-end through handle_envelope: the api_only event is skipped
        # before any task matching / DB work (session=None would blow up
        # if a match were recorded).
        state = engine.MatcherState(
            events={10: _event(submission_policy="api_only")},
            tasks_by_event={10: [_task(target="Twisted bow")]},
            participants={5: [(10, 77, None)]},
        )
        env = _env("drop", {"item_name": "Twisted bow", "quantity": 1})
        env["used_api"] = False
        assert engine.handle_envelope(None, _FakeRedis(), state, env) == []


# ── bingo events only track tasks bound to a board tile ──────────────────────

class TestBingoBoardScoping:
    """A bingo event's task list may hold more tasks than the board has tiles
    (e.g. leftovers picked at creation but never placed). Those unbound tasks
    must not track completion — otherwise they fire completion messages while
    no tile is marked. Matching is restricted to cell-bound tasks; session=None
    proves the unbound task is skipped before any DB work."""

    def test_unbound_task_in_bingo_event_is_skipped(self):
        state = engine.MatcherState(
            events={10: _event(has_bingo=True)},
            tasks_by_event={10: [_task(id=1, target="Twisted bow")]},
            cells_by_task={},  # no cell bound to task 1 → off the board
            participants={5: [(10, 77, None)]},
        )
        env = _env("drop", {"item_name": "Twisted bow", "quantity": 1})
        assert engine.handle_envelope(None, _FakeRedis(), state, env) == []

    def test_standard_event_still_tracks_unbound_tasks(self):
        # has_bingo False → the bingo restriction never applies, so a task with
        # no cell binding still matches (reaching record_match, which needs a
        # real session — hence the AttributeError, proving the match was NOT
        # skipped by the bingo gate).
        state = engine.MatcherState(
            events={10: _event(has_bingo=False)},
            tasks_by_event={10: [_task(id=1, target="Twisted bow")]},
            cells_by_task={},
            participants={5: [(10, 77, None)]},
        )
        env = _env("drop", {"item_name": "Twisted bow", "quantity": 1})
        import pytest
        with pytest.raises(AttributeError):
            engine.handle_envelope(None, _FakeRedis(), state, env)


# ── WOM reconciler envelopes (kind=wom_kc, source=wom) ───────────────────────

class TestWomKcMatch:
    def test_wom_kc_matches_precomputed_metric(self):
        t = _task(type="kc_target", target="Zulrah", target_value=50,
                  wom_metric="zulrah")
        m = engine.match_task(t, _env("wom_kc", {"boss_metric": "zulrah", "kc": 250}))
        assert m == {"mode": "kc_abs", "quantity": 0}

    def test_wom_kc_metric_mismatch(self):
        t = _task(type="kc_target", target="Zulrah", wom_metric="zulrah")
        assert engine.match_task(
            t, _env("wom_kc", {"boss_metric": "vorkath", "kc": 10})) is None

    def test_wom_kc_task_without_metric_stays_plugin_only(self):
        t = _task(type="kc_target", target="Some Custom Boss")  # no wom_metric
        assert engine.match_task(
            t, _env("wom_kc", {"boss_metric": "some_custom_boss"})) is None

    def test_drop_matching_unchanged_for_kc_target(self):
        t = _task(type="kc_target", target="Zulrah", wom_metric="zulrah")
        m = engine.match_task(t, _env("drop", {"npc_name": "Zulrah", "kill_count": 12}))
        assert m == {"mode": "kc", "quantity": 1}

    def test_wom_kc_never_matches_other_task_types(self):
        assert engine.match_task(
            _task(type="loot_value"), _env("wom_kc", {"boss_metric": "zulrah"})) is None
        assert engine.match_task(
            _task(target="Zulrah"), _env("wom_kc", {"boss_metric": "zulrah"})) is None


class TestWomSourcePolicy:
    def test_api_only_accepts_wom_source(self):
        env = {"used_api": False, "source": "wom"}
        assert engine.accepts_submission_source(
            _event(submission_policy="api_only"), env) is True

    def test_api_only_still_rejects_non_api(self):
        assert engine.accepts_submission_source(
            _event(submission_policy="api_only"), {"used_api": False}) is False

    def test_confirm_non_api_auto_for_wom(self):
        env = {"used_api": False, "source": "wom"}
        assert engine.completion_status(
            _event(submission_policy="confirm_non_api"), _task(), env) == "auto"

    def test_confirm_non_api_still_pends_non_api(self):
        assert engine.completion_status(
            _event(submission_policy="confirm_non_api"), _task(),
            {"used_api": False}) == "pending"

    def test_requires_confirmation_still_pends_wom(self):
        env = {"used_api": True, "source": "wom"}
        assert engine.completion_status(
            _event(requires_confirmation=True), _task(), env) == "pending"


# ── pet_collection ────────────────────────────────────────────────────────────

class TestPetCollection:
    def test_specific_pet_match(self):
        t = _task(type="pet_collection", target="Baby mole", target_value=1)
        m = engine.match_task(t, _env("pet", {"pet_name": "Baby mole"}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Baby mole"}

    def test_specific_pet_case_insensitive(self):
        t = _task(type="pet_collection", target="baby MOLE")
        m = engine.match_task(t, _env("pet", {"pet_name": "Baby mole"}))
        assert m["mode"] == "count" and m["quantity"] == 1

    def test_specific_pet_mismatch(self):
        t = _task(type="pet_collection", target="Baby mole")
        assert engine.match_task(t, _env("pet", {"pet_name": "Beaver"})) is None

    def test_category_boss(self):
        t = _task(type="pet_collection", target=None, config={"categories": ["boss"]})
        assert engine.match_task(t, _env("pet", {"pet_name": "Baby mole"})) is not None
        assert engine.match_task(t, _env("pet", {"pet_name": "Beaver"})) is None

    def test_category_skilling(self):
        t = _task(type="pet_collection", target=None, config={"categories": ["skilling"]})
        assert engine.match_task(t, _env("pet", {"pet_name": "Beaver"})) is not None

    def test_any_pet_matches_and_excludes_misc(self):
        t = _task(type="pet_collection", target=None, config={})
        assert engine.match_task(t, _env("pet", {"pet_name": "Vorki"})) is not None
        # misc pets are opt-in — never counted by a bare "any pet" task.
        assert engine.match_task(t, _env("pet", {"pet_name": "Chompy chick"})) is None

    def test_misc_pet_counts_when_categorized(self):
        t = _task(type="pet_collection", target=None, config={"categories": ["misc"]})
        assert engine.match_task(t, _env("pet", {"pet_name": "Chompy chick"})) is not None

    def test_pet_list_membership(self):
        # Explicit allow list (customized category preset): only listed names.
        t = _task(type="pet_collection", target=None,
                  config={"pets": ["Baby mole", "Beaver"]})
        assert engine.match_task(t, _env("pet", {"pet_name": "Baby mole"})) is not None
        assert engine.match_task(t, _env("pet", {"pet_name": "Vorki"})) is None

    def test_pet_list_case_insensitive(self):
        t = _task(type="pet_collection", target=None, config={"pets": ["baby MOLE"]})
        m = engine.match_task(t, _env("pet", {"pet_name": "Baby mole"}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Baby mole"}

    def test_pet_list_includes_misc_when_listed(self):
        # Listing a misc pet is deliberate — it counts (unlike bare any-pet).
        t = _task(type="pet_collection", target=None, config={"pets": ["Chompy chick"]})
        assert engine.match_task(t, _env("pet", {"pet_name": "Chompy chick"})) is not None

    def test_wrong_kind_no_match(self):
        t = _task(type="pet_collection", target="Baby mole")
        assert engine.match_task(t, _env("drop", {"item_name": "Baby mole"})) is None

    def test_missing_pet_name(self):
        t = _task(type="pet_collection", target=None, config={})
        assert engine.match_task(t, _env("pet", {})) is None


# ── pets mixed into item_collection lists (config.pet_items) ──────────────────

class TestItemListPets:
    """Names flagged in ``config.pet_items`` credit from a `pet` submission by
    name and NEVER from a same-named drop/clog (which would double-credit
    alongside the pet echo). Unflagged lists ignore pet submissions entirely."""

    PET_LIST = {"kind": "all_of",
                "items": ["Dragon axe", "Phoenix"],
                "pet_items": ["Phoenix"]}

    def test_pet_submission_credits_flagged_name(self):
        t = _task(config=dict(self.PET_LIST), target_value=2)
        m = engine.match_task(t, _env("pet", {"pet_name": "phoenix"}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "phoenix"}

    def test_pet_submission_ignores_unflagged_name(self):
        t = _task(config=dict(self.PET_LIST), target_value=2)
        assert engine.match_task(t, _env("pet", {"pet_name": "Dragon axe"})) is None

    def test_drop_and_clog_skip_pet_flagged_name(self):
        t = _task(config=dict(self.PET_LIST), target_value=2)
        assert engine.match_task(t, _env("drop", {"item_name": "Phoenix", "quantity": 1})) is None
        assert engine.match_task(t, _env("clog", {"item_name": "Phoenix"})) is None

    def test_plain_item_still_credits_from_drop(self):
        t = _task(config=dict(self.PET_LIST), target_value=2)
        m = engine.match_task(t, _env("drop", {"item_name": "Dragon axe", "quantity": 1}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Dragon axe"}

    def test_pet_submission_without_flags_no_match(self):
        t = _task(config={"kind": "all_of", "items": ["Dragon axe"]}, target_value=1)
        assert engine.match_task(t, _env("pet", {"pet_name": "Dragon axe"})) is None

    def test_pet_weight_in_point_collection(self):
        t = _task(config={"kind": "point_collection",
                          "items": [{"item_name": "Zulrah's scales", "points": 1},
                                    {"item_name": "Pet snakeling", "points": 50}],
                          "pet_items": ["Pet snakeling"]},
                  target_value=100)
        m = engine.match_task(t, _env("pet", {"pet_name": "Pet snakeling"}))
        assert m == {"mode": "count", "quantity": 50, "matched_target": "Pet snakeling"}

    def test_pet_in_groups_config(self):
        t = _task(config={"kind": "groups",
                          "groups": [{"mode": "all_of", "items": ["Kq head"]},
                                     {"mode": "any_of", "need": 1, "items": ["Kalphite princess"]}],
                          "pet_items": ["Kalphite princess"]},
                  target_value=2)
        m = engine.match_task(t, _env("pet", {"pet_name": "Kalphite princess"}))
        assert m == {"mode": "count", "quantity": 1, "matched_target": "Kalphite princess"}
