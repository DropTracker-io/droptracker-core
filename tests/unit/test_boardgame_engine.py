"""services/boardgame_engine.py — pure parts (settings, dice, coins, mercy)
plus the tile-effect framework (services/boardgame_effects.py): roadblock
pass/land/break semantics, the blocked-turn stall, and the movement core.

The conftest stubs the ``services`` package, so the real modules load by
file path (the test_event_types.py pattern). boardgame_effects registers
under its REAL dotted name so the engine's lazy ``from services.
boardgame_effects import ...`` resolves to the real thing. Movement tests
run against a tiny in-memory session fake with real filter evaluation."""
from __future__ import annotations

import importlib.util
import json
import random
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

_BASE = Path(__file__).resolve().parent.parent.parent / "services"

# The effects module FIRST (under its dotted name — the engine lazy-imports
# it at call time and must get the real module, not the conftest stub).
_fx_spec = importlib.util.spec_from_file_location(
    "services.boardgame_effects", _BASE / "boardgame_effects.py")
fx = importlib.util.module_from_spec(_fx_spec)
sys.modules["services.boardgame_effects"] = fx
_fx_spec.loader.exec_module(fx)

_spec = importlib.util.spec_from_file_location(
    "_real_boardgame_engine", _BASE / "boardgame_engine.py")
bg = importlib.util.module_from_spec(_spec)
sys.modules["_real_boardgame_engine"] = bg
_spec.loader.exec_module(bg)


class TestBoardSettings:
    def test_none_returns_defaults(self):
        s = bg.board_settings(None)
        assert s == bg.DEFAULT_BOARD_SETTINGS
        assert s is not bg.DEFAULT_BOARD_SETTINGS  # deep copy, not the constant

    def test_corrupt_json_returns_defaults(self):
        assert bg.board_settings("{nope") == bg.DEFAULT_BOARD_SETTINGS
        assert bg.board_settings("[1,2]") == bg.DEFAULT_BOARD_SETTINGS

    def test_partial_document_merges_key_by_key(self):
        s = bg.board_settings('{"movement": {"dice_count": 2}, "coins": {"enabled": false}}')
        assert s["movement"]["dice_count"] == 2
        assert s["movement"]["dice_sides"] == 6          # untouched sibling
        assert s["coins"]["enabled"] is False
        assert s["coins"]["per_difficulty"]["fire"] == 50  # untouched nested
        assert s["mercy"]["enabled"] is True               # untouched section

    def test_dict_input_accepted(self):
        s = bg.board_settings({"tile_render": {"mode": "outline", "outline_width": 4}})
        assert s["tile_render"]["mode"] == "outline"
        assert s["tile_render"]["outline_width"] == 4
        assert s["tile_render"]["outline_color"] == "#ffcc33"


class TestRollDice:
    def test_seeded_dice_are_deterministic(self):
        s = bg.board_settings({"movement": {"dice_count": 2, "dice_sides": 6}})
        a = bg.roll_dice(s, random.Random(42))
        b = bg.roll_dice(s, random.Random(42))
        assert a == b and len(a) == 2
        assert all(1 <= f <= 6 for f in a)

    def test_fixed_step_returns_single_pseudo_face(self):
        s = bg.board_settings({"movement": {"mode": "fixed_step", "fixed_step": 3}})
        assert bg.roll_dice(s) == [3]

    def test_fixed_step_floor_is_one(self):
        s = bg.board_settings({"movement": {"mode": "fixed_step", "fixed_step": 0}})
        assert bg.roll_dice(s) == [1]

    def test_dice_bounds_clamped(self):
        s = bg.board_settings({"movement": {"dice_count": 99, "dice_sides": 200}})
        faces = bg.roll_dice(s, random.Random(1))
        assert len(faces) == 8            # count capped
        assert all(1 <= f <= 100 for f in faces)  # sides capped

    def test_raw_one_sided_die_rolls_one(self):
        # A hand-built dict that never went through board_settings: the clamp
        # floor is 1, so a d1 rolls 1 rather than silently becoming a d2.
        assert bg.roll_dice({"movement": {"dice_count": 2, "dice_sides": 1}}) == [1, 1]


class TestSingleSidedDiceNormalization:
    """Nd1 is deterministic, so it stores and reads as fixed_step: N."""

    def test_1d1_becomes_a_fixed_step_of_one(self):
        s = bg.board_settings({"movement": {"dice_count": 1, "dice_sides": 1}})
        assert s["movement"]["mode"] == "fixed_step"
        assert s["movement"]["fixed_step"] == 1
        assert bg.roll_dice(s) == [1]

    def test_nd1_becomes_a_fixed_step_of_n(self):
        s = bg.board_settings({"movement": {"dice_count": 3, "dice_sides": 1}})
        assert s["movement"]["mode"] == "fixed_step"
        assert s["movement"]["fixed_step"] == 3
        assert bg.roll_dice(s) == [3]

    def test_count_is_capped_before_it_becomes_the_step(self):
        s = bg.board_settings({"movement": {"dice_count": 99, "dice_sides": 1}})
        assert s["movement"]["fixed_step"] == 8
        assert bg.roll_dice(s) == [8]

    def test_sides_absent_or_multi_sided_is_left_alone(self):
        s = bg.board_settings({"movement": {"dice_count": 2, "dice_sides": 2}})
        assert s["movement"]["mode"] == "dice"
        assert bg.board_settings(None)["movement"]["mode"] == "dice"

    def test_collapsing_resets_the_die_so_dice_mode_stays_reachable(self):
        # The leftover d1 must not survive the collapse: it would re-normalize
        # the document the moment the leader picked "Dice roll" again.
        s = bg.board_settings({"movement": {"dice_count": 3, "dice_sides": 1}})
        assert s["movement"]["dice_sides"] == 6
        back = bg.board_settings({"movement": {**s["movement"], "mode": "dice"}})
        assert back["movement"]["mode"] == "dice"
        assert len(bg.roll_dice(back, random.Random(1))) == 3

    def test_explicit_fixed_step_keeps_its_own_step(self):
        # mode already fixed_step: the leftover dice_sides must not rewrite it.
        s = bg.board_settings({"movement": {"mode": "fixed_step", "fixed_step": 5,
                                            "dice_count": 2, "dice_sides": 1}})
        assert s["movement"]["fixed_step"] == 5
        assert bg.roll_dice(s) == [5]

    def test_garbage_settings_fall_back(self):
        s = bg.board_settings({"movement": {"dice_count": "x", "dice_sides": None}})
        faces = bg.roll_dice(s, random.Random(1))
        assert len(faces) == 1 and 1 <= faces[0] <= 6


class TestCoinReward:
    def test_ladder(self):
        s = bg.board_settings(None)
        assert bg.coin_reward(s, "air") == 10
        assert bg.coin_reward(s, "water") == 20
        assert bg.coin_reward(s, "earth") == 30
        assert bg.coin_reward(s, "fire") == 50

    def test_unknown_or_missing_difficulty_uses_default(self):
        s = bg.board_settings(None)
        assert bg.coin_reward(s, None) == 10
        assert bg.coin_reward(s, "banana") == 10

    def test_disabled_coins_award_zero(self):
        s = bg.board_settings({"coins": {"enabled": False}})
        assert bg.coin_reward(s, "fire") == 0

    def test_override_ladder(self):
        s = bg.board_settings({"coins": {"per_difficulty": {"air": 1}}})
        assert bg.coin_reward(s, "air") == 1

    def test_negative_values_clamped(self):
        s = bg.board_settings({"coins": {"per_difficulty": {"air": -5}, "default": -3}})
        assert bg.coin_reward(s, "air") == 0
        assert bg.coin_reward(s, None) == 0


class TestMercyDeadline:
    def test_grows_with_mercy_count(self):
        s = bg.board_settings(None)
        d0 = bg._mercy_deadline(s, 0)
        d2 = bg._mercy_deadline(s, 2)
        assert d0 and d2
        # base 24h; +12h per prior mercy → exactly 24h apart.
        assert abs((d2 - d0).total_seconds() - 24 * 3600) < 5

    def test_disabled_returns_none(self):
        s = bg.board_settings({"mercy": {"enabled": False}})
        assert bg._mercy_deadline(s, 0) is None


class TestFinishIdx:
    def _tile(self, idx, kind="normal"):
        return SimpleNamespace(idx=idx, tile_kind=kind)

    def test_last_tile_by_default(self):
        tiles = [self._tile(i) for i in range(5)]
        assert bg.finish_idx(tiles) == 4

    def test_explicit_finish_wins(self):
        tiles = [self._tile(0), self._tile(1, "finish"), self._tile(2)]
        assert bg.finish_idx(tiles) == 1

    def test_empty_board(self):
        assert bg.finish_idx([]) is None


class TestCanTriggerRoll:
    def test_team_mode(self):
        s = bg.board_settings(None)  # manual_roller: team
        assert bg.can_trigger_roll(s, is_team_member=True, is_admin=False) is True
        assert bg.can_trigger_roll(s, is_team_member=False, is_admin=False) is False

    def test_group_admin_mode_blocks_members(self):
        s = bg.board_settings({"movement": {"manual_roller": "group_admin"}})
        assert bg.can_trigger_roll(s, is_team_member=True, is_admin=False) is False
        assert bg.can_trigger_roll(s, is_team_member=False, is_admin=True) is True

    def test_admin_always_allowed(self):
        s = bg.board_settings(None)
        assert bg.can_trigger_roll(s, is_team_member=False, is_admin=True) is True


class TestInstanceDetection:
    def test_flagged_config(self):
        t = SimpleNamespace(config='{"board_instance": true}')
        assert bg._is_board_instance(t) is True

    def test_plain_or_corrupt_config(self):
        assert bg._is_board_instance(SimpleNamespace(config=None)) is False
        assert bg._is_board_instance(SimpleNamespace(config="{bad")) is False
        assert bg._is_board_instance(SimpleNamespace(config='{"kind": "any_of"}')) is False


# =========================================================================== #
# Effect framework (services/boardgame_effects.py) + movement integration
# =========================================================================== #
class TestEffectRegistry:
    def test_roadblock_spec(self):
        spec = fx.EFFECT_REGISTRY["roadblock"]
        assert spec.is_tile_bound is True
        assert spec.item_type == "defensive"
        assert spec.default_behavior == {
            "break_on": "pass", "stall_turns": 1, "visible_to_all": True,
            "expire_on_placer_move": True}

    def test_every_registered_effect_is_live(self):
        # All 7 handlers exist today; the registry is the single source.
        assert fx.live_effects() == frozenset(fx.EFFECT_REGISTRY)

    def test_tile_bound_set(self):
        assert fx.tile_bound_effects() == ("roadblock",)

    def test_default_behaviors_mirror_engine_settings(self):
        # DEFAULT_BOARD_SETTINGS carries the same values (the web contract);
        # a drift here means the merged settings doc lies about defaults.
        engine_defaults = bg.DEFAULT_BOARD_SETTINGS["items"]["behaviors"]
        for key, behavior in engine_defaults.items():
            assert behavior == fx.EFFECT_REGISTRY[key].default_behavior


class TestBehaviorResolution:
    def test_registry_defaults_when_nothing_else(self):
        b = fx.resolve_effect_behavior("roadblock")
        assert b == {"break_on": "pass", "stall_turns": 1, "visible_to_all": True,
                     "expire_on_placer_move": True}

    def test_item_config_overrides_registry(self):
        item = SimpleNamespace(effect_config='{"stall_turns": 2, "break_on": "land"}')
        b = fx.resolve_effect_behavior("roadblock", item=item)
        assert b["stall_turns"] == 2 and b["break_on"] == "land"
        assert b["visible_to_all"] is True  # untouched layer-1 default

    def test_event_override_beats_item_config(self):
        item = SimpleNamespace(effect_config='{"stall_turns": 2}')
        b = fx.resolve_effect_behavior(
            "roadblock", item=item,
            overrides={"roadblock": {"stall_turns": 0, "break_on": "both"}})
        assert b == {"break_on": "both", "stall_turns": 0, "visible_to_all": True,
                     "expire_on_placer_move": True}

    def test_settings_document_accepted(self):
        b = fx.resolve_effect_behavior(
            "roadblock",
            settings={"items": {"behaviors": {"roadblock": {"break_on": "land"}}}})
        assert b["break_on"] == "land"

    def test_garbage_sanitized_to_defaults(self):
        b = fx.resolve_effect_behavior(
            "roadblock",
            overrides={"roadblock": {"break_on": "banana", "stall_turns": "x",
                                     "visible_to_all": 0}})
        assert b["break_on"] == "pass"
        assert b["stall_turns"] == 1        # registry default, not 0
        assert b["visible_to_all"] is False  # falsy honored as an explicit choice

    def test_stall_clamped(self):
        b = fx.resolve_effect_behavior(
            "roadblock", overrides={"roadblock": {"stall_turns": 99}})
        assert b["stall_turns"] == 10

    def test_legacy_config_backfilled(self):
        # Pre-web49a placements stored only {"stall_turns": 1}.
        b = fx.sanitize_behavior("roadblock", {"stall_turns": 1})
        assert b == {"break_on": "pass", "stall_turns": 1, "visible_to_all": True,
                     "expire_on_placer_move": True}


class TestTileTrigger:
    def _trig(self, break_on, *, landed, stall=1):
        return fx.resolve_tile_trigger(
            "roadblock", {"break_on": break_on, "stall_turns": stall},
            landed=landed)

    def test_pass_mode(self):
        t = self._trig("pass", landed=False)
        assert t.stops and t.consumed and t.stall_turns == 1
        t = self._trig("pass", landed=True)
        assert not t.stops and not t.consumed and t.stall_turns == 0

    def test_land_mode(self):
        t = self._trig("land", landed=False)
        assert t.stops and not t.consumed and t.stall_turns == 1
        t = self._trig("land", landed=True)
        assert not t.stops and t.consumed and t.stall_turns == 0

    def test_both_mode(self):
        assert self._trig("both", landed=False).consumed is True
        assert self._trig("both", landed=True).consumed is True

    def test_landing_never_stalls(self):
        assert self._trig("both", landed=True, stall=3).stall_turns == 0


# --------------------------------------------------------------------------- #
# In-memory session fake with REAL filter evaluation (so the freeze query
# can't accidentally match a roadblock row, etc.).
# --------------------------------------------------------------------------- #
class _F:
    """Column stand-in whose comparisons yield evaluatable condition tuples."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):  # noqa: D105
        return ("eq", self.name, other)

    def __ne__(self, other):
        return ("ne", self.name, other)

    def __hash__(self):
        return hash(self.name)

    def in_(self, vals):
        return ("in", self.name, list(vals))


class FakePositionModel:
    team_id = _F("team_id")
    event_id = _F("event_id")


class FakeTileModel:
    event_id = _F("event_id")
    idx = _F("idx")


class FakeEffectModel:
    event_id = _F("event_id")
    effect_type = _F("effect_type")
    status = _F("status")
    target_tile_idx = _F("target_tile_idx")
    target_team_id = _F("target_team_id")
    source_team_id = _F("source_team_id")


class FakeConfigModel:
    event_id = _F("event_id")


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def with_for_update(self):
        # Row-lock is a no-op in the fake; perform_roll/award_coins lock the
        # position/team row (P0-2/P0-3) and this keeps the chain fluent.
        return self

    def filter(self, *conds):
        out = []
        for r in self.rows:
            ok = True
            for c in conds:
                if not isinstance(c, tuple) or len(c) != 3:
                    continue
                op, name, val = c
                v = getattr(r, name, None)
                if op == "eq" and v != val:
                    ok = False
                elif op == "ne" and v == val:
                    ok = False
                elif op == "in" and v not in val:
                    ok = False
            if ok:
                out.append(r)
        return FakeQuery(out)

    def order_by(self, *keys):
        rows = self.rows
        for k in reversed(keys):
            if isinstance(k, _F):
                rows = sorted(rows, key=lambda r, _k=k: getattr(r, _k.name))
        return FakeQuery(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class FakeSession:
    def __init__(self, positions=(), tiles=(), effects=()):
        self._map = {
            FakePositionModel: list(positions),
            FakeTileModel: list(tiles),
            FakeEffectModel: list(effects),
        }

    def query(self, model):
        return FakeQuery(self._map.get(model, []))

    def add(self, obj):
        pass

    def flush(self):
        pass


EVENT_ID = 10
TEAM = 1
RIVAL = 2


def _tiles(n=10):
    """A rest-tile track (no tasks/difficulty — landing never hits the DB)."""
    return [SimpleNamespace(
        idx=i, event_id=EVENT_ID,
        tile_kind="start" if i == 0 else ("finish" if i == n - 1 else "normal"),
        task_id=None, difficulty=None, x=0.0, y=0.0, label=None,
    ) for i in range(n)]


def _pos(tile=0, status="awaiting_roll", turns=0, blocked_until=None):
    return SimpleNamespace(
        team_id=TEAM, event_id=EVENT_ID, tile_idx=tile, current_task_id=None,
        turns_completed=turns, status=status, last_roll=None,
        task_assigned_at=None, mercy_deadline=None, mercy_count=0,
        blocked_until_turn=blocked_until,
    )


def _roadblock(tile_idx, source=RIVAL, behavior=None, status="active"):
    if behavior is None:
        behavior = {"break_on": "pass", "stall_turns": 0, "visible_to_all": True}
    return SimpleNamespace(
        id=77, event_id=EVENT_ID, source_team_id=source, target_team_id=None,
        target_tile_idx=tile_idx, effect_type="roadblock",
        effect_config=json.dumps(behavior), status=status,
    )


def _fixed(step):
    return bg.board_settings({"movement": {"mode": "fixed_step",
                                           "fixed_step": step}})


@pytest.fixture
def board_models(monkeypatch):
    """Point the stubbed db.models at evaluatable fakes for the engine's
    lazy per-call imports."""
    dbm = sys.modules["db.models"]
    monkeypatch.setattr(dbm, "EventBoardPosition", FakePositionModel, raising=False)
    monkeypatch.setattr(dbm, "EventBoardTile", FakeTileModel, raising=False)
    monkeypatch.setattr(dbm, "EventBoardEffect", FakeEffectModel, raising=False)
    monkeypatch.setattr(dbm, "EventBoardConfig", FakeConfigModel, raising=False)


class TestMovePieceTileEffects:
    """_move_piece is the shared core: perform_roll AND the advance item
    (services/boardgame_shop._use_advance) both call it — every behavior
    here holds for teleports too."""

    def test_placer_is_not_immune(self, board_models):
        block = _roadblock(3, source=TEAM)  # the mover's OWN bulwark
        s = FakeSession(effects=[block])
        pos = _pos()
        summary = bg._move_piece(s, EVENT_ID, TEAM, pos, _tiles(), 0, 6, _fixed(6))
        assert summary["to"] == 3
        assert pos.tile_idx == 3
        assert summary["roadblock"]["placed_by_team_id"] == TEAM

    def test_pass_mode_stops_and_consumes_on_pass(self, board_models):
        block = _roadblock(3, behavior={"break_on": "pass", "stall_turns": 0})
        s = FakeSession(effects=[block])
        pos = _pos()
        summary = bg._move_piece(s, EVENT_ID, TEAM, pos, _tiles(), 0, 6, _fixed(6))
        assert summary["to"] == 3
        assert block.status == "consumed"
        assert summary["roadblock"]["consumed"] is True
        # stall 0 → no blocked state; the tile assigns as normal (rest tile).
        assert pos.status == "awaiting_roll"
        assert pos.blocked_until_turn is None

    def test_pass_mode_persists_on_exact_landing(self, board_models):
        block = _roadblock(3, behavior={"break_on": "pass", "stall_turns": 1})
        s = FakeSession(effects=[block])
        pos = _pos()
        summary = bg._move_piece(s, EVENT_ID, TEAM, pos, _tiles(), 0, 3, _fixed(3))
        assert summary["to"] == 3 and pos.tile_idx == 3
        assert block.status == "active"          # landing does not break it
        assert pos.status == "awaiting_roll"     # and never stalls the lander
        assert "blocked" not in summary

    def test_land_mode_stops_but_persists_on_pass(self, board_models):
        block = _roadblock(3, behavior={"break_on": "land", "stall_turns": 0})
        s = FakeSession(effects=[block])
        pos = _pos()
        summary = bg._move_piece(s, EVENT_ID, TEAM, pos, _tiles(), 0, 6, _fixed(6))
        assert summary["to"] == 3                # the blocker still blocks
        assert block.status == "active"          # ...but survives the proc
        assert summary["roadblock"]["consumed"] is False

    def test_land_mode_consumes_on_exact_landing(self, board_models):
        block = _roadblock(3, behavior={"break_on": "land", "stall_turns": 0})
        s = FakeSession(effects=[block])
        pos = _pos()
        summary = bg._move_piece(s, EVENT_ID, TEAM, pos, _tiles(), 0, 3, _fixed(3))
        assert summary["to"] == 3
        assert block.status == "consumed"
        assert summary["roadblock"]["consumed"] is True

    def test_both_mode_consumes_either_way(self, board_models):
        for steps in (6, 3):  # pass-through, then exact landing
            block = _roadblock(3, behavior={"break_on": "both", "stall_turns": 0})
            s = FakeSession(effects=[block])
            summary = bg._move_piece(s, EVENT_ID, TEAM, _pos(), _tiles(), 0,
                                     steps, _fixed(steps))
            assert summary["to"] == 3
            assert block.status == "consumed"

    def test_persistent_block_stops_successive_teams(self, board_models):
        block = _roadblock(3, behavior={"break_on": "land", "stall_turns": 0})
        s = FakeSession(effects=[block])
        for _ in range(2):  # two crossings, block survives both
            summary = bg._move_piece(s, EVENT_ID, TEAM, _pos(), _tiles(), 0,
                                     6, _fixed(6))
            assert summary["to"] == 3
            assert block.status == "active"

    def test_stall_parks_team_blocked(self, board_models):
        block = _roadblock(3, behavior={"break_on": "pass", "stall_turns": 1})
        s = FakeSession(effects=[block])
        pos = _pos(turns=1)  # perform_roll would have ticked this already
        summary = bg._move_piece(s, EVENT_ID, TEAM, pos, _tiles(), 0, 6, _fixed(6))
        assert summary["blocked"] is True
        assert summary["to"] == 3
        assert pos.status == "blocked"
        assert pos.blocked_until_turn == 2       # turns(1) + stall(1)
        assert pos.current_task_id is None
        assert pos.mercy_deadline is None

    def test_legacy_effect_config_still_blocks(self, board_models):
        # A pre-web49a row: only {"stall_turns": 1} — sanitize backfills.
        block = SimpleNamespace(
            id=5, event_id=EVENT_ID, source_team_id=RIVAL, target_team_id=None,
            target_tile_idx=4, effect_type="roadblock",
            effect_config='{"stall_turns": 1}', status="active")
        s = FakeSession(effects=[block])
        pos = _pos(turns=1)
        summary = bg._move_piece(s, EVENT_ID, TEAM, pos, _tiles(), 0, 6, _fixed(6))
        assert summary["to"] == 4 and pos.status == "blocked"
        assert block.status == "consumed"        # legacy default = pass mode

    def test_nearest_block_wins(self, board_models):
        near = _roadblock(2, behavior={"break_on": "pass", "stall_turns": 0})
        far = _roadblock(5, behavior={"break_on": "pass", "stall_turns": 0})
        s = FakeSession(effects=[far, near])
        summary = bg._move_piece(s, EVENT_ID, TEAM, _pos(), _tiles(), 0, 6, _fixed(6))
        assert summary["to"] == 2
        assert near.status == "consumed"
        assert far.status == "active"            # never reached


class TestPerformRollBlocked:
    def test_full_stall_arc(self, board_models):
        """Roll into the bulwark → blocked; next attempt is consumed clearing
        the stall (no movement); the one after moves normally."""
        block = _roadblock(3, behavior={"break_on": "pass", "stall_turns": 1})
        pos = _pos()
        s = FakeSession(positions=[pos], tiles=_tiles(), effects=[block])
        settings = _fixed(6)

        first = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=settings)
        assert first["blocked"] is True and first["to"] == 3
        assert pos.status == "blocked"
        assert pos.turns_completed == 1
        assert pos.blocked_until_turn == 2

        second = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=settings)
        assert second["blocked"] is True
        assert second.get("blocked_cleared") is True
        assert second["from"] == second["to"] == 3   # the attempt never moves
        assert second["dice"] == []
        assert pos.status == "awaiting_roll"          # rest tile resumes play
        assert pos.blocked_until_turn is None
        assert pos.turns_completed == 2

        third = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=settings)
        assert "blocked" not in third
        assert third["to"] == 9 and third["won"] is True  # block already spent

    def test_two_turn_stall_needs_two_attempts(self, board_models):
        block = _roadblock(3, behavior={"break_on": "pass", "stall_turns": 2})
        pos = _pos()
        s = FakeSession(positions=[pos], tiles=_tiles(), effects=[block])
        settings = _fixed(6)

        bg.perform_roll(s, None, EVENT_ID, TEAM, settings=settings)
        assert pos.blocked_until_turn == 3

        held = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=settings)
        assert held["blocked"] is True and "blocked_cleared" not in held
        assert held["stall_remaining"] == 1
        assert pos.status == "blocked"

        cleared = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=settings)
        assert cleared.get("blocked_cleared") is True
        assert pos.status == "awaiting_roll"

    def test_corrupt_block_marker_fails_open(self, board_models):
        pos = _pos(tile=3, status="blocked", turns=1, blocked_until=None)
        s = FakeSession(positions=[pos], tiles=_tiles())
        summary = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=_fixed(6))
        assert summary.get("blocked_cleared") is True
        assert pos.status == "awaiting_roll"

    def test_other_statuses_still_refused(self, board_models):
        pos = _pos(status="active")
        s = FakeSession(positions=[pos], tiles=_tiles())
        assert bg.perform_roll(s, None, EVENT_ID, TEAM, settings=_fixed(6)) is None


class TestRollSSEFrame:
    def test_board_roll_frame_carries_destination(self, board_models, monkeypatch):
        """Regression: perform_roll's SSE publish referenced an undefined
        ``dest`` — the NameError was swallowed and the board_roll frame was
        silently never sent."""
        frames = []
        rt = types.ModuleType("services.realtime")
        rt.publish_event_update = lambda eid, frame: frames.append(frame)
        monkeypatch.setitem(sys.modules, "services.realtime", rt)

        pos = _pos()
        s = FakeSession(positions=[pos], tiles=_tiles())
        summary = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=_fixed(6))
        assert summary["to"] == 6
        rolls = [f for f in frames if f.get("kind") == "board_roll"]
        assert len(rolls) == 1
        assert rolls[0]["from"] == 0 and rolls[0]["to"] == 6
        assert rolls[0]["dice"] == [6]


def _team_effect(effect_type, team=TEAM, status="active", config=None, eid=88):
    return SimpleNamespace(
        id=eid, event_id=EVENT_ID, source_team_id=team, target_team_id=team,
        target_tile_idx=None, effect_type=effect_type,
        effect_config=json.dumps(config) if config is not None else None,
        status=status,
    )


class TestAutoAdvance:
    """auto_advance (P1a): an auto game must never dead-end on a rest/empty
    landing or a roadblock stall, since members can't manually roll in auto
    mode and mercy only sweeps 'active'."""

    def _auto(self, step=6, trigger="auto"):
        return bg.board_settings({"movement": {"mode": "fixed_step",
                                               "fixed_step": step,
                                               "trigger": trigger}})

    def test_auto_mode_chains_rest_tiles_to_finish(self, board_models):
        pos = _pos()  # awaiting_roll at the start tile, no task (rest board)
        s = FakeSession(positions=[pos], tiles=_tiles(), effects=[])
        last = bg.auto_advance(s, None, EVENT_ID, TEAM, self._auto())
        assert last is not None and last["won"] is True
        assert pos.tile_idx == 9 and pos.status == "finished"

    def test_manual_mode_is_a_noop(self, board_models):
        pos = _pos()
        s = FakeSession(positions=[pos], tiles=_tiles(), effects=[])
        assert bg.auto_advance(s, None, EVENT_ID, TEAM,
                               self._auto(trigger="manual")) is None
        assert pos.tile_idx == 0 and pos.status == "awaiting_roll"

    def test_auto_serves_a_roadblock_stall_then_continues(self, board_models):
        block = _roadblock(3, behavior={"break_on": "pass", "stall_turns": 1})
        pos = _pos()
        s = FakeSession(positions=[pos], tiles=_tiles(), effects=[block])
        last = bg.auto_advance(s, None, EVENT_ID, TEAM, self._auto())
        # 0->3 blocked; the next attempt serves the stall; then 3->9 finish.
        assert last["won"] is True and pos.status == "finished"


class TestExtraDiceFixedStep:
    """E2: an armed extra_dice must not be burned in fixed_step mode (where it
    can add nothing) — only drained when it can actually add dice."""

    def test_fixed_step_does_not_consume_extra_dice(self, board_models):
        eff = _team_effect("extra_dice", config={"extra_dice": 2})
        pos = _pos()
        s = FakeSession(positions=[pos], tiles=_tiles(), effects=[eff])
        summary = bg.perform_roll(s, None, EVENT_ID, TEAM,
                                  settings=_fixed(3))
        assert eff.status == "active"   # NOT burned for nothing
        assert summary["dice"] == [3]   # no extra dice added

    def test_dice_mode_consumes_extra_dice(self, board_models):
        eff = _team_effect("extra_dice", config={"extra_dice": 1})
        pos = _pos()
        s = FakeSession(positions=[pos], tiles=_tiles(), effects=[eff])
        settings = bg.board_settings({"movement": {"dice_count": 1, "dice_sides": 6}})
        summary = bg.perform_roll(s, None, EVENT_ID, TEAM, settings=settings,
                                  rng=random.Random(1))
        assert eff.status == "consumed"     # drained in dice mode
        assert len(summary["dice"]) == 2    # 1 base + 1 extra die
