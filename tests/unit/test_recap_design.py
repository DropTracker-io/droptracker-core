"""Recap designs (services/recap_design.py, suggestion #224).

``normalize_design`` is the only validator a stored design ever goes through,
and its defaults ARE the card every clan sees until it customises one, so both
are pinned here. The TypeScript mirror (packages/api-types recap-design.ts)
must agree with ``default_design``.
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_recap_design_under_test", os.path.join(_ROOT, "services", "recap_design.py"))
rd = importlib.util.module_from_spec(_spec)
sys.modules["_recap_design_under_test"] = rd
_spec.loader.exec_module(rd)


class TestDefaults:
    def test_the_default_is_the_card_before_designs(self):
        d = rd.default_design()
        on = [s["key"] for s in d["sections"] if s["enabled"]]
        assert on == ["gallery", "sources", "members", "standouts", "milestones", "drop"]
        assert d["standouts"] == ["pbs", "clog_slots", "cas", "pets", "deaths"]
        assert d["standouts_depth"] == 1
        assert d["counts"]["members"] == 5 and d["counts"]["gallery"] == 10
        assert d["layout"] == "auto" and d["accent"] == "gold"

    def test_garbage_normalises_to_the_default(self):
        assert rd.normalize_design(None) == rd.default_design()
        assert rd.normalize_design("nope") == rd.default_design()

    def test_normalising_is_idempotent(self):
        d = rd.normalize_design({"accent": "frost", "standouts": ["xp"], "counts": {"members": 8}})
        assert rd.normalize_design(d) == d


class TestNormalize:
    def test_sections_keep_order_and_column_and_gain_missing_ones(self):
        d = rd.normalize_design({"sections": [
            {"key": "members", "enabled": True, "column": "main"},
            {"key": "gallery", "enabled": False},
            {"key": "bogus", "enabled": True},
            {"key": "members", "enabled": False},
        ]})
        keys = [s["key"] for s in d["sections"]]
        assert keys[:2] == ["members", "gallery"]
        assert set(keys) == set(rd.SECTION_KEYS) and len(keys) == len(rd.SECTION_KEYS)
        assert d["sections"][0] == {"key": "members", "enabled": True, "column": "main"}
        assert d["sections"][1]["enabled"] is False
        drop = next(s for s in d["sections"] if s["key"] == "drop")
        assert "column" not in drop

    def test_a_bad_column_falls_back_to_the_default_one(self):
        d = rd.normalize_design({"sections": [{"key": "pets", "enabled": True, "column": "left"}]})
        assert d["sections"][0]["column"] == "side"

    def test_standouts_dedupe_filter_and_cap(self):
        d = rd.normalize_design({"standouts": ["xp", "xp", "nope", *rd.STANDOUT_KEYS]})
        assert d["standouts"][0] == "xp"
        assert len(d["standouts"]) == rd.MAX_STANDOUTS
        assert len(set(d["standouts"])) == len(d["standouts"])

    def test_no_standouts_is_a_choice_not_a_reset(self):
        assert rd.normalize_design({"standouts": []})["standouts"] == []

    def test_numbers_are_clamped(self):
        d = rd.normalize_design({"standouts_depth": 9, "counts": {"members": 1, "gallery": "x"}})
        assert d["standouts_depth"] == 3
        assert d["counts"]["members"] == 3
        assert d["counts"]["gallery"] == 10

    def test_text_is_trimmed_and_single_line_where_it_should_be(self):
        d = rd.normalize_design({
            "title": "A" * 200, "headline": "one\ntwo",
            "note": {"title": "Hi\x00", "body": "line 1\nline 2" + "x" * 1000},
        })
        assert len(d["title"]) == rd.TITLE_MAX
        assert d["headline"] == "one two"
        assert d["note"]["title"] == "Hi"
        assert d["note"]["body"].startswith("line 1\nline 2")
        assert len(d["note"]["body"]) == rd.NOTE_BODY_MAX

    def test_hidden_players_are_ints_deduped_and_capped(self):
        d = rd.normalize_design({"hidden_players": [1, "2", 2, "x", *range(100)]})
        assert d["hidden_players"][:2] == [1, 2]
        assert len(d["hidden_players"]) == rd.MAX_HIDDEN_PLAYERS

    def test_hero_stats(self):
        d = rd.normalize_design({"hero_stats": ["xp", "bogus", "ehb", "kills", "pets", "pbs"]})
        assert d["hero_stats"] == ["xp", "ehb", "kills", "pets"]


class TestPeriods:
    def test_valid(self):
        assert rd.is_valid_period("")
        assert rd.is_valid_period("2026-10")

    def test_invalid(self):
        for bad in ("2026", "2026-13", "2026-1", "all", None, "abcd-ef"):
            assert not rd.is_valid_period(bad)


class _Result:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _Session:
    def __init__(self, rows):
        self.rows = rows  # {period: (design_json, updated_at, by, approved_at, approved_by)}

    def execute(self, statement, params=None):
        return _Result(self.rows.get(params["period"]))

    def rollback(self):
        pass


class TestResolve:
    def test_builtin_when_nothing_is_stored(self):
        out = rd.resolve(_Session({}), 1, "2026-10")
        assert out["source"] == "builtin" and out["design"] == rd.default_design()

    def test_the_month_beats_the_clan_default(self):
        import json
        from datetime import datetime

        out = rd.resolve(_Session({
            "": (json.dumps({"accent": "frost"}), datetime(2026, 10, 1), 1, None, None),
            "2026-10": (json.dumps({"accent": "ember"}), datetime(2026, 11, 1), 1, None, None),
        }), 1, "2026-10")
        assert out["source"] == "month" and out["design"]["accent"] == "ember"

    def test_an_approval_only_row_follows_the_default(self):
        import json
        from datetime import datetime

        out = rd.resolve(_Session({
            "": (json.dumps({"accent": "frost"}), datetime(2026, 10, 1), 1, None, None),
            "2026-10": ("", datetime(2026, 11, 1), None, datetime(2026, 11, 1, 9), 5),
        }), 1, "2026-10")
        assert out["source"] == "default" and out["design"]["accent"] == "frost"
        assert out["approved_at"] == "2026-11-01T09:00:00" and out["approved_by"] == 5

    def test_a_missing_table_is_the_builtin_card(self):
        class Broken(_Session):
            def execute(self, statement, params=None):
                raise RuntimeError("Table 'recap_designs' doesn't exist")

        assert rd.resolve(Broken({}), 1, "2026-10")["source"] == "builtin"
