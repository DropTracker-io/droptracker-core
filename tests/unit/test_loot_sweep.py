"""Unit tests for services/loot_sweep.py v2 — nested groups (sub-sets) with
per-group + whole-set bonuses, NPC scoping (matcher index), and batched decay
(``awards_per_tier``).

Loaded straight from the file (like test_event_engine_scoring) so the conftest
``services`` sys.modules stub never shadows the real module.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "loot_sweep.py",
)
_spec = importlib.util.spec_from_file_location("_loot_sweep_ut", _PATH)
ls = importlib.util.module_from_spec(_spec)
sys.modules["_loot_sweep_ut"] = ls
_spec.loader.exec_module(ls)


def _row(item, qty=1, source_type="drop"):
    return SimpleNamespace(matched_target=item, quantity=qty, source_type=source_type)


# --- decay ----------------------------------------------------------------- #
class TestReceiptFactor:
    def test_linear_default_matches_grid(self):
        got = [ls.receipt_factor(k, 20, 1, "linear") for k in range(1, 6)]
        assert got == pytest.approx([1.0, 0.8, 0.6, 0.4, 0.2])

    def test_batched_tiers(self):
        # awards_per_tier=3: receipts 1-3 full, 4-6 at 80%, 7-9 at 60%.
        got = [ls.receipt_factor(k, 20, 3, "linear") for k in (1, 2, 3, 4, 5, 6, 7)]
        assert got == pytest.approx([1.0, 1.0, 1.0, 0.8, 0.8, 0.8, 0.6])

    def test_geometric(self):
        got = [ls.receipt_factor(k, 20, 1, "geometric") for k in range(1, 4)]
        assert got == pytest.approx([1.0, 0.8, 0.64])


class TestItemPoints:
    def test_full_linear_sweep(self):
        assert ls.item_points(40, 5, 5, 20, 1, "linear") == 120  # 40+32+24+16+8

    def test_batched_full_then_step(self):
        # base 4, awards_per_tier 3: first 3 = 4 each (12); next 3 at 80% =
        # 3.2 each (decimal receipts). count 4 → 15.2; count 6 → 21.6.
        assert ls.item_points(4, 3, 15, 20, 3) == 12
        assert ls.item_points(4, 4, 15, 20, 3) == 15.2
        assert ls.item_points(4, 6, 15, 20, 3) == 21.6

    def test_decimal_receipts(self):
        # A 1-pointer at 20% linear decay pays 1, 0.8, 0.6, 0.4, 0.2 — stored
        # and awarded with decimals, not rounded to whole points.
        assert [ls.receipt_points(1, k, 20) for k in range(1, 6)] == [1, 0.8, 0.6, 0.4, 0.2]
        assert ls.item_points(1, 5, 5, 20) == 3.0
        assert ls.item_points(1, 2, 5, 20) == 1.8

    def test_default_max_awards(self):
        assert ls.default_max_awards(1) == 5
        assert ls.default_max_awards(3) == 15

    def test_cap(self):
        assert ls.item_points(40, 99, 5, 20, 1) == 120


# --- config parsing -------------------------------------------------------- #
class TestConfig:
    def test_groups_and_npcs(self):
        cfg = ls.LootSweepConfig({
            "decay_percent": 20, "set_bonus_points": 40,
            "groups": [
                {"label": "Ahrim", "npcs": ["Ahrim the Blighted"], "bonus_points": 4,
                 "items": [{"item_name": "Ahrim's hood", "points": 1}]},
            ],
        })
        assert len(cfg.groups) == 1
        g = cfg.groups[0]
        assert g.label == "Ahrim"
        assert g.npc_keys == frozenset({"ahrim the blighted"})
        assert g.bonus_points == 4
        assert cfg.set_bonus_points == 40
        assert g.by_key["ahrim's hood"].points == 1.0

    def test_v1_backcompat_wraps_flat_items(self):
        cfg = ls.LootSweepConfig({
            "set_bonus_points": 40,
            "items": [{"item_name": "Armadyl helmet", "points": 9}],
        })
        assert len(cfg.groups) == 1
        assert cfg.groups[0].bonus_points == 40   # moved onto the group
        assert cfg.set_bonus_points == 0

    def test_matcher_index(self):
        cfg = ls.LootSweepConfig({"groups": [
            {"npcs": ["Vet'ion", "Calvar'ion"],
             "items": [{"item_name": "Dragon 2h"},
                       {"item_name": "Vet'ion jr.", "source": "pet"}]},
        ]})
        idx = cfg.matcher_index()
        assert idx["dragon 2h"] == {"source": "drop",
                                    "npcs": frozenset({"vet'ion", "calvar'ion"})}
        # pet items aren't NPC-scoped
        assert idx["vet'ion jr."] == {"source": "pet", "npcs": frozenset()}

    def test_item_awards_per_tier_and_max(self):
        cfg = ls.LootSweepConfig({"groups": [{"items": [
            {"item_name": "Brimstone key", "points": 4, "awards_per_tier": 3},
        ]}]})
        it = cfg.groups[0].by_key["brimstone key"]
        assert it.awards_per_tier == 3
        assert it.max_awards == 15


# --- scoring: simple boss (one group) -------------------------------------- #
def _kreearra():
    return ls.LootSweepConfig({
        "decay_percent": 20, "set_bonus_points": 0,
        "groups": [{
            "label": "Kree'arra", "npcs": ["Kree'arra"], "bonus_points": 40,
            "items": [
                {"item_name": "Armadyl helmet", "points": 9},
                {"item_name": "Armadyl chestplate", "points": 9},
                {"item_name": "Armadyl chainskirt", "points": 9},
                {"item_name": "Armadyl hilt", "points": 13},
                {"item_name": "Pet kree'arra", "points": 60, "counts_for_group": False},
            ],
        }],
    })


class TestSimpleBoss:
    def test_one_of_each_completes_group(self):
        cfg = _kreearra()
        counts = {"armadyl helmet": 1, "armadyl chestplate": 1,
                  "armadyl chainskirt": 1, "armadyl hilt": 1}
        r = ls.score_counts(counts, cfg)
        assert r["item_total"] == 40
        assert r["group_bonus_total"] == 40  # group complete
        assert r["set_total"] == 0           # no whole-set bonus configured
        assert r["total"] == 80
        assert r["groups"][0]["completions"] == 1

    def test_pet_scores_but_does_not_gate(self):
        cfg = _kreearra()
        r = ls.score_counts({"pet kree'arra": 1, "armadyl helmet": 1}, cfg)
        assert r["item_total"] == 69
        assert r["groups"][0]["completions"] == 0
        assert r["total"] == 69

    def test_group_bonus_decays_on_repeat_completion(self):
        # Completing the set a second time scores the bonus AGAIN, decayed like
        # an item receipt: 40 then 40*0.8 = 32.
        cfg = _kreearra()
        counts = {k: 2 for k in ("armadyl helmet", "armadyl chestplate",
                                 "armadyl chainskirt", "armadyl hilt")}
        r = ls.score_counts(counts, cfg)
        assert r["groups"][0]["completions"] == 2
        assert r["groups"][0]["awarded"] == 2          # both completions scored
        assert r["group_bonus_total"] == 72            # 40 + 32

    def test_group_bonus_decays_to_zero_after_five(self):
        # Mirrors an item's 5 decay tiers: 40+32+24+16+8 = 120, capped there.
        cfg = _kreearra()
        counts = {k: 7 for k in ("armadyl helmet", "armadyl chestplate",
                                 "armadyl chainskirt", "armadyl hilt")}
        r = ls.score_counts(counts, cfg)
        assert r["groups"][0]["completions"] == 7
        assert r["groups"][0]["awarded"] == 5          # capped at bonus_max (tiers)
        assert r["group_bonus_total"] == 120           # 40+32+24+16+8


# --- scoring: meta-set (Barrows-style) ------------------------------------- #
def _mini_barrows():
    def bro(label, npc, prefix):
        return {"label": label, "npcs": [npc], "bonus_points": 4,
                "items": [{"item_name": f"{prefix} {p}", "points": 1}
                          for p in ("a", "b", "c", "d")]}
    return ls.LootSweepConfig({
        "decay_percent": 20, "set_bonus_points": 40,
        "groups": [bro("Ahrim", "Ahrim the Blighted", "ahrim"),
                   bro("Dharok", "Dharok the Wretched", "dharok")],
    })


class TestMetaSet:
    def test_one_group_done_no_set_bonus(self):
        cfg = _mini_barrows()
        counts = {f"ahrim {p}": 1 for p in "abcd"}
        r = ls.score_counts(counts, cfg)
        assert r["group_bonus_total"] == 4    # ahrim complete
        assert r["set_completions"] == 0      # dharok not done
        assert r["set_total"] == 0

    def test_all_groups_done_awards_set_bonus(self):
        cfg = _mini_barrows()
        counts = {f"{b} {p}": 1 for b in ("ahrim", "dharok") for p in "abcd"}
        r = ls.score_counts(counts, cfg)
        assert r["group_bonus_total"] == 8    # both brothers +4
        assert r["set_completions"] == 1
        assert r["set_total"] == 40
        assert r["total"] == 8 + 8 + 40       # items(8) + group bonuses(8) + set(40)

    def test_repeat_completion_decays_both_levels(self):
        # Two full Barrows completions: each brother sub-set pays 4 then 3.2
        # (7.2 each → 14.4 across two brothers); the whole-set bonus pays
        # 40 then 32 (72). Items score their own decaying points on top.
        cfg = _mini_barrows()
        counts = {f"{b} {p}": 2 for b in ("ahrim", "dharok") for p in "abcd"}
        r = ls.score_counts(counts, cfg)
        assert r["set_completions"] == 2
        assert r["group_bonus_total"] == 14.4   # (4+3.2) * 2 brothers
        assert r["set_total"] == 72             # 40 + 32


# --- scoring: batched item end to end -------------------------------------- #
class TestBatchedScoring:
    def test_brimstone_batches(self):
        cfg = ls.LootSweepConfig({"decay_percent": 20, "groups": [{
            "npcs": ["Alchemical Hydra"],
            "items": [{"item_name": "Brimstone key", "points": 4, "awards_per_tier": 3,
                       "counts_for_group": False}],
        }]})
        # 5 receipts: 4+4+4 (tier0, full) + 3.2+3.2 (tier1) = 18.4
        assert ls.score_counts({"brimstone key": 5}, cfg)["item_total"] == 18.4


class TestFromRows:
    def test_counts_and_total(self):
        cfg = _kreearra()
        rows = [_row("Armadyl helmet"), _row("Armadyl chestplate"),
                _row("Armadyl chainskirt"), _row("Armadyl hilt")]
        assert ls.counts_from_rows(rows) == {
            "armadyl helmet": 1, "armadyl chestplate": 1,
            "armadyl chainskirt": 1, "armadyl hilt": 1}
        assert ls.team_total(rows, cfg) == 80

    def test_bonus_rows_ignored(self):
        assert ls.counts_from_rows([_row("x", source_type="bonus")]) == {}


# --- receipt enrichment (the Discord message detail) ----------------------- #
class TestReceiptDetail:
    def test_first_receipt_full_points_and_decay_preview(self):
        cfg = _kreearra()
        prev = ls.score_counts({}, cfg)
        curr = ls.score_counts({"armadyl helmet": 1}, cfg)
        d = ls.receipt_detail(curr, prev, cfg, "Armadyl helmet")
        assert d["item_name"] == "Armadyl helmet"
        assert d["received_points"] == 9        # first receipt, full base
        assert d["item_count"] == 1
        assert d["item_max"] == 5               # default 5 decay tiers
        assert d["item_remaining"] == 4
        assert d["next_receipt_points"] == 7.2  # 9 * 0.8
        assert d["group_have"] == 1
        assert d["group_need"] == 4             # gating items (pet excluded)
        assert d["group_bonus_points"] == 40

    def test_decayed_receipt(self):
        cfg = _kreearra()
        prev = ls.score_counts({"armadyl hilt": 1}, cfg)
        curr = ls.score_counts({"armadyl hilt": 2}, cfg)
        d = ls.receipt_detail(curr, prev, cfg, "Armadyl hilt")
        assert d["received_points"] == 10.4     # 13 * 0.8
        assert d["item_count"] == 2
        assert d["next_receipt_points"] == 7.8  # 13 * 0.6

    def test_capped_item_scores_nothing(self):
        cfg = _kreearra()
        prev = ls.score_counts({"armadyl helmet": 5}, cfg)
        curr = ls.score_counts({"armadyl helmet": 6}, cfg)
        d = ls.receipt_detail(curr, prev, cfg, "Armadyl helmet")
        assert d["received_points"] == 0
        assert d["item_remaining"] == 0
        assert d["next_receipt_points"] == 0

    def test_group_completion_progress(self):
        cfg = _kreearra()
        base = {"armadyl helmet": 1, "armadyl chestplate": 1, "armadyl chainskirt": 1}
        prev = ls.score_counts(base, cfg)
        curr = ls.score_counts({**base, "armadyl hilt": 1}, cfg)
        d = ls.receipt_detail(curr, prev, cfg, "Armadyl hilt")
        assert (d["group_have"], d["group_need"]) == (4, 4)
        assert d["group_completions"] == 1
        assert d["group_awarded"] == 1

    def test_set_progress_across_groups(self):
        cfg = _mini_barrows()
        prevc = {f"ahrim {p}": 1 for p in "abcd"}
        prevc.update({f"dharok {p}": 1 for p in "abc"})
        currc = {f"ahrim {p}": 1 for p in "abcd"}
        currc.update({f"dharok {p}": 1 for p in "abcd"})
        d = ls.receipt_detail(ls.score_counts(currc, cfg),
                              ls.score_counts(prevc, cfg), cfg, "dharok d")
        assert (d["set_have"], d["set_need"]) == (2, 2)
        assert d["set_completions"] == 1
        assert d["set_bonus_points"] == 40
        assert d["group_label"] == "Dharok"

    def test_alias_credits_its_entry(self):
        cfg = ls.LootSweepConfig({"decay_percent": 20, "groups": [{
            "label": "Vestige", "npcs": ["Vardorvis"], "bonus_points": 0,
            "items": [{"item_name": "Ultor vestige", "points": 10,
                       "match_names": ["Gold ring"]}]}]})
        d = ls.receipt_detail(ls.score_counts({"gold ring": 1}, cfg),
                              ls.score_counts({}, cfg), cfg, "Gold ring")
        assert d is not None
        assert d["item_name"] == "Ultor vestige"
        assert d["received_points"] == 10

    def test_unknown_item_returns_none(self):
        cfg = _kreearra()
        curr = ls.score_counts({}, cfg)
        assert ls.receipt_detail(curr, curr, cfg, "Twisted bow") is None


class TestMatchNamesAndRequired:
    """"Also counts as" aliases + required-count gating (2026-07-19)."""

    def _cfg(self):
        return ls.LootSweepConfig({"decay_percent": 20, "groups": [{
            "npcs": ["Vardorvis"],
            "bonus_points": 10,
            "items": [
                {"item_name": "Ultor vestige", "points": 5,
                 "match_names": ["Gold ring"]},
                {"item_name": "Executioner's axe head", "points": 2},
            ],
        }]})

    def test_alias_keys_registered_in_matcher(self):
        idx = self._cfg().matcher_index()
        assert "ultor vestige" in idx and "gold ring" in idx
        assert idx["gold ring"]["npcs"] == idx["ultor vestige"]["npcs"]

    def test_alias_receipts_pool_into_one_entry(self):
        cfg = self._cfg()
        b = ls.score_counts({"gold ring": 1, "ultor vestige": 1,
                             "executioner's axe head": 1}, cfg)
        item = b["groups"][0]["items"][0]
        # 2 pooled receipts: 5 + 4 = 9, one decay step despite mixed names.
        assert item["count"] == 2 and item["points"] == 9
        assert b["groups"][0]["awarded"] == 1  # both entries collected → bonus

    def test_icon_ids_for_orders_primary_then_pieces(self):
        cfg = ls.LootSweepConfig({"groups": [{
            "npcs": ["Vardorvis"],
            "items": [
                {"item_name": "Ultor vestige", "item_id": 28281,
                 "match_names": ["Gold ring"]},
                {"item_name": "Any ancestral piece", "virtual": True,
                 "match_names": ["Ancestral hat", "Ancestral robe top"]},
            ],
        }]})
        alias_ids = {"gold ring": 1635, "ancestral hat": 21018,
                     "ancestral robe top": 21021}
        vestige, ancestral = cfg.groups[0].items
        # Real item: its own icon first, then the alias.
        assert ls.icon_ids_for(vestige, alias_ids) == [28281, 1635]
        # Virtual label: only the pieces (no own icon).
        assert ls.icon_ids_for(ancestral, alias_ids) == [21018, 21021]
        # Missing alias ids are skipped, not crashed on.
        assert ls.icon_ids_for(vestige, {}) == [28281]

    def test_virtual_label_pools_pieces_without_own_key(self):
        cfg = ls.LootSweepConfig({"decay_percent": 20, "groups": [{
            "npcs": ["Great Olm"],
            "bonus_points": 25,
            "items": [
                {"item_name": "Any ancestral piece", "virtual": True, "points": 3,
                 "required": 3,
                 "match_names": ["Ancestral hat", "Ancestral robe top",
                                 "Ancestral robe bottom"]},
            ],
        }]})
        item = cfg.groups[0].items[0]
        assert item.virtual is True
        # The label is NOT a credit key — only the three real pieces are.
        assert "any ancestral piece" not in item.match_keys
        assert set(item.match_keys) == {
            "ancestral hat", "ancestral robe top", "ancestral robe bottom"}
        idx = cfg.matcher_index()
        assert "any ancestral piece" not in idx and "ancestral hat" in idx
        # A drop of the label name itself credits nothing; real pieces pool.
        assert ls.score_counts({"any ancestral piece": 5}, cfg)["total"] == 0
        b = ls.score_counts({"ancestral hat": 2, "ancestral robe top": 1}, cfg)
        assert b["groups"][0]["items"][0]["count"] == 3
        assert b["groups"][0]["completions"] == 1  # required 3 met

    def test_same_name_in_two_entries_scores_both_additively(self):
        # An ancestral piece that's BOTH an individual entry and a member of an
        # "any 3" pool in the same group scores in each (the additive model).
        cfg = ls.LootSweepConfig({"decay_percent": 20, "groups": [{
            "npcs": ["Great Olm"],
            "bonus_points": 25,
            "items": [
                {"item_name": "Ancestral hat", "item_id": 21018, "points": 5},
                {"item_name": "Any ancestral piece", "virtual": True, "points": 2,
                 "required": 3,
                 "match_names": ["Ancestral hat", "Ancestral robe top",
                                 "Ancestral robe bottom"]},
            ],
        }]})
        b = ls.score_counts({"ancestral hat": 1}, cfg)
        indiv, pool = b["groups"][0]["items"]
        # The single drop credits the individual entry (5) AND the pool (2).
        assert indiv["count"] == 1 and indiv["points"] == 5
        assert pool["count"] == 1 and pool["points"] == 2
        assert b["total"] == 7  # additive, no group bonus yet (pool needs 3)
        # The matcher registers the piece once (merged), so intake records one row.
        idx = cfg.matcher_index()
        assert idx["ancestral hat"]["source"] == "drop"

    def test_pool_completes_on_any_mix_including_duplicates(self):
        # "Any 3" means 3 receipts total across the pool's names — same item ×3,
        # 2+1, or 1+1+1 all complete it once; 6 completes it twice.
        cfg = ls.LootSweepConfig({"decay_percent": 20, "groups": [{
            "npcs": ["Great Olm"], "bonus_points": 25, "bonus_max": 3,
            "items": [{"item_name": "Any ancestral piece", "virtual": True,
                       "points": 3, "required": 3,
                       "match_names": ["Ancestral hat", "Ancestral robe top",
                                       "Ancestral robe bottom"]}],
        }]})

        def completions(counts):
            return ls.score_counts(counts, cfg)["groups"][0]["completions"]

        assert completions({"ancestral hat": 3}) == 1               # 3 of the same
        assert completions({"ancestral hat": 2, "ancestral robe top": 1}) == 1  # 2+1
        assert completions({"ancestral hat": 1, "ancestral robe top": 1,
                            "ancestral robe bottom": 1}) == 1         # 1+1+1
        assert completions({"ancestral hat": 2}) == 0                # only 2
        assert completions({"ancestral hat": 6}) == 2                # two completions

    def test_required_gates_group_completion(self):
        cfg = ls.LootSweepConfig({"decay_percent": 20, "groups": [{
            "npcs": ["Great Olm"],
            "bonus_points": 25,
            "items": [
                {"item_name": "Ancestral hat", "points": 3, "required": 3,
                 "match_names": ["Ancestral robe top", "Ancestral robe bottom"]},
            ],
        }]})
        two = ls.score_counts({"ancestral hat": 1, "ancestral robe top": 1}, cfg)
        assert two["groups"][0]["completions"] == 0 and two["groups"][0]["awarded"] == 0
        three = ls.score_counts({"ancestral hat": 1, "ancestral robe top": 1,
                                 "ancestral robe bottom": 1}, cfg)
        assert three["groups"][0]["completions"] == 1 and three["groups"][0]["awarded"] == 1
        # Repeat completions keep stepping every `required` receipts.
        six = ls.score_counts({"ancestral robe top": 6}, cfg)
        assert six["groups"][0]["completions"] == 2
