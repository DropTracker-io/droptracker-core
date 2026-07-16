"""Unit tests for web_api/event_leadership.py (web48a) — the pure config
parser and the election tally."""

import importlib.util
import os
import sys

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web_api", "event_leadership.py",
)
_spec = importlib.util.spec_from_file_location("_event_leadership_under_test", _MODULE_PATH)
el = importlib.util.module_from_spec(_spec)
sys.modules["_event_leadership_under_test"] = el
_spec.loader.exec_module(el)


class TestEffectiveLeadership:
    def test_defaults(self):
        assert el.effective_leadership(None) == {
            "enabled": False, "co_leaders": False, "selection": "admin"}

    def test_overlay_from_json_string(self):
        config = el.effective_leadership('{"enabled": true, "selection": "election"}')
        assert config == {"enabled": True, "co_leaders": False, "selection": "election"}

    def test_corrupt_json_falls_back(self):
        assert el.effective_leadership("{nope")["enabled"] is False
        assert el.effective_leadership([1, 2])["enabled"] is False

    def test_unknown_selection_ignored(self):
        assert el.effective_leadership('{"selection": "coup"}')["selection"] == "admin"


class TestNormalizeInput:
    def test_partial_object(self):
        assert el.normalize_leadership_input({"enabled": True}) == {"enabled": True}

    def test_full_object(self):
        body = {"enabled": True, "co_leaders": True, "selection": "election"}
        assert el.normalize_leadership_input(body) == body

    def test_rejects_non_bool(self):
        assert el.normalize_leadership_input({"enabled": "yes"}) is None

    def test_rejects_bad_selection(self):
        assert el.normalize_leadership_input({"selection": "monarchy"}) is None

    def test_rejects_non_dict(self):
        assert el.normalize_leadership_input("enabled") is None


class TestTally:
    def test_no_votes_keeps_current(self):
        assert el.tally_election([], current_leader=7) == 7
        assert el.tally_election([], current_leader=None) is None

    def test_strict_plurality_wins(self):
        votes = [(1, 9), (2, 9), (3, 5)]
        assert el.tally_election(votes, current_leader=5) == 9

    def test_tie_keeps_current_leader(self):
        votes = [(1, 9), (2, 5)]
        assert el.tally_election(votes, current_leader=5) == 5
        assert el.tally_election(votes, current_leader=None) is None

    def test_single_vote_elects(self):
        assert el.tally_election([(1, 9)], current_leader=None) == 9
