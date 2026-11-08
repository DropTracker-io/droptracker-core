"""Changing a PvP counting rule re-judges the kills already on file.

Intake judges each kill once per group (``pvp_kill_groups.counted`` /
``reason``). Before this, turning "Count kills on clanmates" on, or raising the
daily same-victim cap, only affected later kills: on 2026-11-08 Restricted
(group 97) switched clanmate kills on and their test kill stayed off the board
until staff flipped the row by hand. What these pin:

* ``rejudge_kills`` applies the current rules (clanmate, daily cap replayed in
  kill order, global group exempt) and leaves other verdicts alone;
* ``apply_counting_rule_change`` moves the group's standings for exactly the
  kills that flipped, skips excluded kills, and never raises;
* both settings writers call it when, and only when, a rule key changed.
"""
from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from data.submissions import pvp

GROUP = 97
DAY = datetime(2026, 11, 8, 18, 0)


def _kill(kid, victim="Antelic", victim_player_id=None, when=DAY, excluded=None):
    return SimpleNamespace(id=kid, player_id=7592, victim_name=victim,
                           victim_player_id=victim_player_id, date_added=when,
                           excluded_reason=excluded)


def _link(counted=True, reason=None):
    return SimpleNamespace(counted=counted, reason=reason)


def _config(group=GROUP, members=None, limit=None, unlimited=None):
    cfg = {}
    if members is not None:
        cfg[(group, pvp.COUNT_MEMBER_KILLS_KEY)] = members
    if limit is not None:
        cfg[(group, pvp.SAME_VICTIM_LIMIT_KEY)] = limit
    if unlimited is not None:
        cfg[(group, pvp.SAME_VICTIM_UNLIMITED_KEY)] = unlimited
    return cfg


class TestRejudge:
    def test_turning_clanmate_kills_on_counts_a_stored_member_kill(self):
        link = _link(counted=False, reason="member_kill")
        kill = _kill(7879, victim_player_id=5759)
        flipped = pvp.rejudge_kills([(link, kill)], GROUP, _config(members="1"), {5759})
        assert flipped == [(kill, True)]
        assert (link.counted, link.reason) == (True, None)

    def test_turning_it_off_uncounts_kills_on_current_members(self):
        member = (_link(), _kill(1, victim_player_id=5759))
        outsider = (_link(), _kill(2, victim="Zezima", victim_player_id=1))
        flipped = pvp.rejudge_kills([member, outsider], GROUP, _config(members="0"), {5759})
        assert flipped == [(member[1], False)]
        assert member[0].reason == "member_kill"
        assert (outsider[0].counted, outsider[0].reason) == (True, None)

    def test_a_stored_member_kill_stays_one_after_the_victim_leaves(self):
        link = _link(counted=False, reason="member_kill")
        flipped = pvp.rejudge_kills([(link, _kill(1, victim_player_id=5759))], GROUP,
                                    _config(members="0"), member_ids=set())
        assert flipped == []
        assert link.reason == "member_kill"

    def test_the_daily_cap_is_replayed_in_kill_order(self):
        rows = [(_link(), _kill(i)) for i in range(1, 6)]
        flipped = pvp.rejudge_kills(rows, GROUP, _config(limit="3"), set())
        assert [k.id for k, counted in flipped] == [4, 5]
        assert [link.counted for link, _ in rows] == [True, True, True, False, False]
        assert rows[4][0].reason == "same_victim_limit"

    def test_raising_the_cap_counts_the_kills_it_held_back(self):
        rows = [(_link(), _kill(i)) for i in range(1, 4)]
        rows += [(_link(False, "same_victim_limit"), _kill(i)) for i in range(4, 6)]
        flipped = pvp.rejudge_kills(rows, GROUP, _config(limit="5"), set())
        assert [(k.id, c) for k, c in flipped] == [(4, True), (5, True)]

    def test_the_cap_is_per_victim_and_per_day(self):
        rows = [(_link(), _kill(1)), (_link(), _kill(2, victim="ANTELIC")),
                (_link(), _kill(3, victim="Zezima")),
                (_link(), _kill(4, when=datetime(2026, 11, 9, 1, 0)))]
        flipped = pvp.rejudge_kills(rows, GROUP, _config(limit="1"), set())
        assert [k.id for k, _ in flipped] == [2]  # same victim, any case, same day

    def test_no_limit_counts_everything(self):
        rows = [(_link(False, "same_victim_limit"), _kill(i)) for i in range(1, 9)]
        flipped = pvp.rejudge_kills(rows, GROUP, _config(unlimited="1"), set())
        assert len(flipped) == 8

    def test_the_global_group_ignores_the_clanmate_rule(self):
        link = _link()
        flipped = pvp.rejudge_kills([(link, _kill(1, victim_player_id=5759))],
                                    pvp.GLOBAL_GROUP_ID, _config(group=2, members="0"), {5759})
        assert flipped == [] and link.counted

    def test_other_verdicts_are_left_alone_but_still_fill_the_cap(self):
        other = _link(counted=True, reason="staff_override")
        later = _link()
        flipped = pvp.rejudge_kills([(other, _kill(1)), (later, _kill(2))], GROUP,
                                    _config(limit="1"), set())
        assert other.reason == "staff_override"
        assert [(k.id, c) for k, c in flipped] == [(2, False)]


class TestApplyCountingRuleChange:
    @pytest.fixture
    def wired(self, monkeypatch):
        calls = {"record": [], "remove": [], "dirty": [], "committed": False, "closed": False}

        class _S:
            def commit(self):
                calls["committed"] = True

            def close(self):
                calls["closed"] = True

        standings = SimpleNamespace(
            record_kill=lambda kill, gids, **kw: calls["record"].append((kill.id, gids, kw)),
            remove_kill=lambda kill, gids, **kw: calls["remove"].append((kill.id, gids)),
        )
        monkeypatch.setattr(sys.modules["services"], "pvp_standings", standings, raising=False)
        monkeypatch.setitem(sys.modules, "services.pvp_standings", standings)
        import db.models as models
        monkeypatch.setattr(models, "Session", _S, raising=False)
        monkeypatch.setattr(pvp, "_mark_boards_dirty", lambda gids: calls["dirty"].append(gids))
        return calls

    def test_moves_standings_for_flipped_kills_only(self, wired, monkeypatch):
        monkeypatch.setattr(pvp, "recount_group", lambda s, gid: [
            (_kill(1), True), (_kill(2), False), (_kill(3, excluded="pvp_minigame"), True)])
        assert pvp.apply_counting_rule_change(GROUP) == 3
        assert wired["committed"] and wired["closed"]
        assert wired["record"] == [(1, [GROUP], {"recent": False})]
        assert wired["remove"] == [(2, [GROUP])]
        assert wired["dirty"] == [[GROUP]]

    def test_nothing_flipped_leaves_the_board_alone(self, wired, monkeypatch):
        monkeypatch.setattr(pvp, "recount_group", lambda s, gid: [])
        assert pvp.apply_counting_rule_change(GROUP) == 0
        assert wired["dirty"] == []

    def test_a_failure_never_reaches_the_settings_save(self, wired, monkeypatch):
        def boom(s, gid):
            raise RuntimeError("db down")
        monkeypatch.setattr(pvp, "recount_group", boom)
        assert pvp.apply_counting_rule_change(GROUP) == 0
        assert wired["closed"]


# --------------------------------------------------------------------------- #
# Both settings writers
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, row):
        self.row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.row

    def delete(self, **k):
        return 0


class _S:
    def __init__(self, row):
        self.row, self.added = row, []

    def query(self, *a):
        return _Q(self.row)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass


class _CM:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


class _FakeConfigRow(SimpleNamespace):
    group_id = config_key = id = None


@pytest.fixture
def recounts(monkeypatch):
    seen = []
    monkeypatch.setattr(pvp, "apply_counting_rule_change", seen.append)
    return seen


class TestSitePatchHook:
    def _patch(self, monkeypatch, stored, body):
        import web_api.routes.config as cfg

        row = SimpleNamespace(config_key=next(iter(body)), config_value=stored, long_value=None)
        s = _S(row)
        monkeypatch.setattr(cfg, "db_session", lambda: _CM(s))
        monkeypatch.setattr(cfg, "load_user", lambda s, uid: SimpleNamespace(user_id=uid))
        monkeypatch.setattr(cfg, "manageable_guild_ids", lambda uid: set())
        monkeypatch.setattr(cfg, "assert_group_admin", lambda *a, **k: None)
        monkeypatch.setattr(cfg, "GroupConfiguration", _FakeConfigRow)
        monkeypatch.setattr(cfg, "AuditLog", lambda **kw: SimpleNamespace(**kw))
        monkeypatch.setattr(cfg.group_config, "invalidate", lambda gid, *a: None)
        cfg.apply_group_config_patch(7, GROUP, body)

    def test_switching_clanmate_kills_on_recounts(self, monkeypatch, recounts):
        self._patch(monkeypatch, "0", {"pvp_count_member_kills": True})
        assert recounts == [GROUP]

    def test_resaving_the_same_value_does_not(self, monkeypatch, recounts):
        self._patch(monkeypatch, "1", {"pvp_count_member_kills": True})
        assert recounts == []

    def test_an_unrelated_setting_does_not(self, monkeypatch, recounts):
        self._patch(monkeypatch, "100", {"pvp_minimum_value_to_notify": 5000})
        assert recounts == []


class TestBotWriterHook:
    def test_a_changed_rule_recounts_and_a_no_op_does_not(self, monkeypatch, recounts):
        import importlib.util
        import os

        path = os.path.join(os.path.dirname(__file__), "..", "..", "services",
                            "group_config_writer.py")
        spec = importlib.util.spec_from_file_location("gcw_under_test", path)
        gcw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gcw)

        existing = {"pvp_same_victim_daily_limit": SimpleNamespace(
            config_key="pvp_same_victim_daily_limit", config_value="3", long_value=None)}

        class _BQ:
            def filter(self, *a, **k):
                return self

            def all(self):
                return list(existing.values())

            def first(self):
                return None

        class _BS:
            def query(self, *a):
                return _BQ()

            def add(self, obj):
                pass

            def commit(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(gcw, "Session", _BS)
        monkeypatch.setattr(gcw.group_config, "invalidate", lambda gid: None)

        gcw.set_group_config(GROUP, {"pvp_same_victim_daily_limit": "3"})
        assert recounts == []
        gcw.set_group_config(GROUP, {"pvp_same_victim_daily_limit": "5"})
        assert recounts == [GROUP]
