"""Player erasure rules (ticket 494): what moves, what goes, what stays."""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

# tests/conftest.py stubs the whole ``db`` package, so load the module by path.
_PATH = pathlib.Path(__file__).resolve().parents[2] / "db" / "player_erasure.py"
_spec = importlib.util.spec_from_file_location("player_erasure_under_test", _PATH)
pe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pe  # dataclasses resolve their module here
_spec.loader.exec_module(pe)


class TestEventTables:
    def test_event_rows_are_reassigned_not_deleted(self):
        for t in ("web_event_completions", "web_event_effort", "web_event_player_points",
                  "web_event_team_members", "event_participants"):
            assert pe.is_event_table(t)

    def test_own_data_is_not_event_data(self):
        for t in ("drops", "player_points", "notified", "events_audit", "player_event_log"):
            assert not pe.is_event_table(t)


class TestPlaceholderName:
    def test_no_real_rsn_can_match(self):
        # RSNs are letters, digits, space, hyphen, underscore; '#' never appears,
        # so intake can never resolve a real submission onto a placeholder.
        name = f"{pe.PLACEHOLDER_PREFIX}9999"
        assert len(name) <= 20  # players.player_name is varchar(20)
        assert not re.fullmatch(r"[A-Za-z0-9 _-]{1,12}", name)


class TestRedisKeys:
    def test_player_and_user_patterns(self):
        keys = pe.redis_keys_for(0, 0)  # id 0 is a real id
        assert "player:0:*" in keys and "player:0" in keys
        assert "plugin:notify:0" in keys
        assert "web:profile:0" in keys

    def test_player_only(self):
        keys = pe.redis_keys_for(5, None)
        assert not any(k.startswith("web:") for k in keys)

    def test_patterns_are_scoped_to_the_id(self):
        # A pattern ending in the bare id must not be a prefix glob, or
        # player 5 would also take player 55's keys.
        for k in pe.redis_keys_for(5, 7):
            assert not re.search(r":(5|7)\*", k)


class TestRefusals:
    def test_apply_refuses_when_plan_has_refusals(self):
        plan = pe.Plan(player_id=1, user_id=None, refusals=["billing rows"])
        try:
            pe.apply_erasure(object(), plan)
        except RuntimeError as e:
            assert "billing rows" in str(e)
        else:
            raise AssertionError("expected refusal")
