"""services/boardgame_shop.py (web50a) + the engine's movement-mod/economy
integration — the shop expansion's trickiest paths against an in-memory ORM
fake with real filter evaluation.

Load order mirrors test_boardgame_engine.py: the real effects + engine modules
register under their REAL dotted names so boardgame_shop's top-level
``from services.boardgame_engine import ...`` resolves to the real functions
(not the conftest ``services`` MagicMock), then the shop module loads by path.
"""
from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BASE = Path(__file__).resolve().parent.parent.parent / "services"


def _load(dotted, filename):
    spec = importlib.util.spec_from_file_location(dotted, _BASE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


fx = _load("services.boardgame_effects", "boardgame_effects.py")
bg = _load("services.boardgame_engine", "boardgame_engine.py")
shop = _load("services.boardgame_shop", "boardgame_shop.py")


# --------------------------------------------------------------------------- #
# In-memory ORM fake (real filter/order evaluation, add-routing, count/delete).
# --------------------------------------------------------------------------- #
class _F:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return ("eq", self.name, other)

    def __ne__(self, other):
        return ("ne", self.name, other)

    def __hash__(self):
        return hash(self.name)

    def in_(self, vals):
        return ("in", self.name, list(vals))

    def is_(self, val):
        return ("eq", self.name, val)


def _model(name, cols):
    ns = {c: _F(c) for c in cols}
    ns["__init__"] = lambda self, **kw: self.__dict__.update(kw)
    ns["__repr__"] = lambda self: f"<{name} {self.__dict__}>"
    return type(name, (), ns)


EventBoardEffect = _model("EventBoardEffect", [
    "id", "event_id", "source_team_id", "target_team_id", "target_tile_idx",
    "effect_type", "effect_config", "status", "inventory_id"])
EventBoardPosition = _model("EventBoardPosition", [
    "team_id", "event_id", "tile_idx", "current_task_id", "turns_completed",
    "status", "blocked_until_turn", "last_roll", "task_assigned_at",
    "mercy_deadline", "mercy_count", "pending_choice"])
EventBoardTile = _model("EventBoardTile", [
    "id", "event_id", "idx", "difficulty", "task_id", "tile_kind", "x", "y",
    "label"])
EventTeam = _model("EventTeam", ["id", "event_id", "coins", "name", "score"])
EventTeamInventory = _model("EventTeamInventory", [
    "id", "event_id", "team_id", "shop_item_id", "status", "price_paid",
    "acquired_turn", "used_turn", "used_at", "used_by_user_id", "used_on",
    "created_at"])
EventCoinLedger = _model("EventCoinLedger", [
    "id", "event_id", "team_id", "delta", "reason", "ref_type", "ref_id",
    "balance_after", "acted_by_user_id", "note"])
BoardgameShopItem = _model("BoardgameShopItem", [
    "id", "key", "name", "description", "icon_item_id", "item_type", "effect",
    "effect_config", "cost_coins", "type_cooldown_turns", "sort", "active"])
EventShopRotation = _model("EventShopRotation", [
    "id", "event_id", "shop_item_id", "price_override", "stock", "enabled",
    "stock_per_refresh", "per_team_cap", "available_from_turn",
    "available_until_turn"])
EventBoardConfig = _model("EventBoardConfig", [
    "id", "event_id", "settings", "background_url", "bg_width", "bg_height",
    "shop_refreshed_at", "shop_refreshed_turn"])
EventTask = _model("EventTask", [
    "id", "event_id", "type", "label", "target", "target_value", "points",
    "requires_confirmation", "config", "visibility", "difficulty"])
EventCompletion = _model("EventCompletion", ["id", "task_id"])
EventProgress = _model("EventProgress", [
    "id", "event_id", "task_id", "team_id", "progress", "completed",
    "completed_at"])

_ALL_MODELS = {
    "EventBoardEffect": EventBoardEffect, "EventBoardPosition": EventBoardPosition,
    "EventBoardTile": EventBoardTile, "EventTeam": EventTeam,
    "EventTeamInventory": EventTeamInventory, "EventCoinLedger": EventCoinLedger,
    "BoardgameShopItem": BoardgameShopItem, "EventShopRotation": EventShopRotation,
    "EventBoardConfig": EventBoardConfig, "EventTask": EventTask,
    "EventCompletion": EventCompletion, "EventProgress": EventProgress,
}


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *conds):
        out = []
        for r in self.rows:
            ok = True
            for c in conds:
                if not (isinstance(c, tuple) and len(c) == 3):
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

    def with_for_update(self):
        return self

    def order_by(self, *keys):
        rows = self.rows
        for k in reversed(keys):
            if isinstance(k, _F):
                rows = sorted(
                    rows,
                    key=lambda r, _k=k: (getattr(r, _k.name) is None,
                                         getattr(r, _k.name) or 0))
        return FakeQuery(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)

    def count(self):
        return len(self.rows)

    def delete(self, synchronize_session=False):
        return len(self.rows)


class FakeSession:
    def __init__(self, **data):
        self.data = {}
        self.committed = 0
        for name, rows in data.items():
            self.data[_ALL_MODELS[name]] = list(rows)

    def query(self, model):
        return FakeQuery(self.data.get(model, []))

    def add(self, obj):
        self.data.setdefault(type(obj), []).append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def wire_models(monkeypatch):
    """Point the stubbed db.models at the evaluatable fakes for the lazy
    per-call imports in both the shop and engine modules."""
    dbm = sys.modules["db.models"]
    for name, cls in _ALL_MODELS.items():
        monkeypatch.setattr(dbm, name, cls, raising=False)


E = 10  # event id


def _fixed(step):
    return bg.board_settings({"movement": {"mode": "fixed_step",
                                           "fixed_step": step}})


def _tiles(n=10):
    return [SimpleNamespace(
        idx=i, event_id=E,
        tile_kind="start" if i == 0 else ("finish" if i == n - 1 else "normal"),
        task_id=None, difficulty=None, x=0.0, y=0.0, label=None,
    ) for i in range(n)]


def _pos(team=1, tile=0, status="awaiting_roll", turns=0, task=None):
    return EventBoardPosition(
        team_id=team, event_id=E, tile_idx=tile, current_task_id=task,
        turns_completed=turns, status=status, last_roll=None,
        task_assigned_at=None, mercy_deadline=None, mercy_count=0,
        blocked_until_turn=None, pending_choice=None)


def _eff(**kw):
    kw.setdefault("id", 1)
    kw.setdefault("event_id", E)
    kw.setdefault("source_team_id", 1)
    kw.setdefault("target_team_id", None)
    kw.setdefault("target_tile_idx", None)
    kw.setdefault("effect_config", None)
    kw.setdefault("status", "active")
    return EventBoardEffect(**kw)


# =========================================================================== #
# Pure helpers
# =========================================================================== #
class TestPureHelpers:
    def test_shifted_difficulty(self):
        assert shop._shifted_difficulty("earth", -1) == "water"
        assert shop._shifted_difficulty("air", -1) == "air"        # clamp low
        assert shop._shifted_difficulty("fire", 2) == "fire"       # clamp high
        assert shop._shifted_difficulty("earth", 0) == "earth"     # no shift
        assert shop._shifted_difficulty("banana", -1) == "banana"  # unknown

    def test_distinct_tier_order(self):
        assert shop._distinct_tier_order("earth")[0] == "earth"
        assert set(shop._distinct_tier_order("earth")) == set(shop._DIFFICULTY_ORDER)

    def test_shop_refresh_due_turns(self):
        import datetime as dt
        now = dt.datetime(2026, 1, 1)
        assert shop.shop_refresh_due("turns", 3, None, 0, now, 3) is True
        assert shop.shop_refresh_due("turns", 3, None, 0, now, 2) is False
        # NULL marker = clock not started → never due (baseline set elsewhere).
        assert shop.shop_refresh_due("turns", 3, None, None, now, 99) is False

    def test_shop_refresh_due_hours(self):
        import datetime as dt
        base = dt.datetime(2026, 1, 1, 0, 0)
        assert shop.shop_refresh_due(
            "hours", 2, base, None, dt.datetime(2026, 1, 1, 2, 0), 0) is True
        assert shop.shop_refresh_due(
            "hours", 2, base, None, dt.datetime(2026, 1, 1, 1, 59), 0) is False

    def test_shop_refresh_due_zero_interval_and_none_mode(self):
        import datetime as dt
        now = dt.datetime(2026, 1, 1)
        assert shop.shop_refresh_due("turns", 0, None, 0, now, 99) is False
        assert shop.shop_refresh_due("none", 3, None, 0, now, 99) is False


# =========================================================================== #
# _absorb_defense: shield + ward
# =========================================================================== #
class TestAbsorbDefense:
    def test_shield_absorbs_any_offensive(self):
        shield = _eff(id=1, target_team_id=2, effect_type="shield")
        s = FakeSession(EventBoardEffect=[shield])
        res = shop._absorb_defense(s, E, 2, "knockback")
        assert res["absorbed_by"] == "shield"
        assert shield.status == "consumed"

    def test_ward_absorbs_listed_key(self):
        ward = _eff(id=1, target_team_id=2, effect_type="ward",
                    effect_config='{"blocks": ["steal_item"]}')
        s = FakeSession(EventBoardEffect=[ward])
        res = shop._absorb_defense(s, E, 2, "steal_item")
        assert res["absorbed_by"] == "ward"
        assert ward.status == "consumed"

    def test_ward_ignores_uncovered_key(self):
        ward = _eff(id=1, target_team_id=2, effect_type="ward",
                    effect_config='{"blocks": ["steal_item"]}')
        s = FakeSession(EventBoardEffect=[ward])
        assert shop._absorb_defense(s, E, 2, "knockback") is None
        assert ward.status == "active"  # untouched — didn't cover the effect

    def test_ward_offensive_token_absorbs_any(self):
        ward = _eff(id=1, target_team_id=2, effect_type="ward",
                    effect_config='{"blocks": ["offensive"]}')
        s = FakeSession(EventBoardEffect=[ward])
        res = shop._absorb_defense(s, E, 2, "reroll_opponent_task")
        assert res["absorbed_by"] == "ward"

    def test_no_defense_returns_none(self):
        s = FakeSession(EventBoardEffect=[])
        assert shop._absorb_defense(s, E, 2, "steal_item") is None

    def test_freeze_absorbed_by_ward_end_to_end(self):
        ward = _eff(id=1, target_team_id=2, effect_type="ward",
                    effect_config='{"blocks": ["offensive"]}')
        tpos = _pos(team=2, status="active")
        s = FakeSession(EventBoardEffect=[ward], EventBoardPosition=[tpos])
        item = SimpleNamespace(effect="freeze_opponent", effect_config='{"turns": 2}')
        res = shop._use_freeze_opponent(s, None, E, 1, _pos(team=1), item,
                                        _fixed(6), target={"target_team_id": 2})
        assert res["absorbed_by"] == "ward"
        assert ward.status == "consumed"
        # No freeze effect was armed (the ward ate it).
        assert not [e for e in s.data.get(EventBoardEffect, [])
                    if e.effect_type == "freeze_opponent"]


# =========================================================================== #
# Offensive handlers: steal_item, knockback, reroll_opponent_task
# =========================================================================== #
class TestStealItem:
    def test_steal_reassigns_owned_item(self):
        tpos = _pos(team=2, status="active")
        inv = EventTeamInventory(id=55, event_id=E, team_id=2, shop_item_id=7,
                                 status="owned")
        s = FakeSession(EventBoardPosition=[tpos], EventBoardEffect=[],
                        EventTeamInventory=[inv])
        item = SimpleNamespace(effect="steal_item", effect_config=None)
        res = shop._use_steal_item(s, None, E, 1, _pos(team=1), item, _fixed(6),
                                   rng=random.Random(1),
                                   target={"target_team_id": 2})
        assert res["stolen_shop_item_id"] == 7
        assert inv.team_id == 1  # reassigned to the acting team

    def test_steal_nothing_owned_409(self):
        tpos = _pos(team=2, status="active")
        s = FakeSession(EventBoardPosition=[tpos], EventBoardEffect=[],
                        EventTeamInventory=[])
        item = SimpleNamespace(effect="steal_item", effect_config=None)
        with pytest.raises(shop.ShopError) as ei:
            shop._use_steal_item(s, None, E, 1, _pos(team=1), item, _fixed(6),
                                 target={"target_team_id": 2})
        assert ei.value.status == 409

    def test_steal_absorbed_by_shield(self):
        tpos = _pos(team=2, status="active")
        shield = _eff(id=9, target_team_id=2, effect_type="shield")
        inv = EventTeamInventory(id=55, event_id=E, team_id=2, shop_item_id=7,
                                 status="owned")
        s = FakeSession(EventBoardPosition=[tpos], EventBoardEffect=[shield],
                        EventTeamInventory=[inv])
        item = SimpleNamespace(effect="steal_item", effect_config=None)
        res = shop._use_steal_item(s, None, E, 1, _pos(team=1), item, _fixed(6),
                                   target={"target_team_id": 2})
        assert res["absorbed_by"] == "shield"
        assert inv.team_id == 2  # not stolen


class TestKnockback:
    def test_knockback_never_below_zero(self):
        tpos = _pos(team=2, tile=1, status="active")
        s = FakeSession(EventBoardPosition=[tpos], EventBoardEffect=[],
                        EventBoardTile=_tiles(10))
        item = SimpleNamespace(effect="knockback", effect_config='{"tiles": 3}')
        res = shop._use_knockback(s, None, E, 1, _pos(team=1), item, _fixed(6),
                                  rng=random.Random(1),
                                  target={"target_team_id": 2})
        assert res["to"] == 0        # max(0, 1 - 3) never goes negative
        assert tpos.tile_idx == 0

    def test_knockback_moves_back_n(self):
        tpos = _pos(team=2, tile=8, status="active")
        s = FakeSession(EventBoardPosition=[tpos], EventBoardEffect=[],
                        EventBoardTile=_tiles(10))
        item = SimpleNamespace(effect="knockback", effect_config='{"tiles": 3}')
        res = shop._use_knockback(s, None, E, 1, _pos(team=1), item, _fixed(6),
                                  target={"target_team_id": 2})
        assert res["from"] == 8 and res["to"] == 5
        assert tpos.tile_idx == 5


class TestRerollOpponent:
    def test_reroll_opponent_absorbed(self):
        tpos = _pos(team=2, tile=3, status="active", task=100)
        shield = _eff(id=9, target_team_id=2, effect_type="shield")
        s = FakeSession(EventBoardPosition=[tpos], EventBoardEffect=[shield])
        item = SimpleNamespace(effect="reroll_opponent_task", effect_config=None)
        res = shop._use_reroll_opponent_task(
            s, None, E, 1, _pos(team=1), item, _fixed(6),
            target={"target_team_id": 2})
        assert res["absorbed_by"] == "shield"


# =========================================================================== #
# reroll_task difficulty_shift  (draw-tier is what matters here)
# =========================================================================== #
class TestRerollDifficultyShift:
    def _setup(self, monkeypatch):
        captured = {}

        def fake_pool(session, event_id, difficulty):
            captured["difficulty"] = difficulty
            return [SimpleNamespace(id=200, label="drawn", difficulty=difficulty)]

        monkeypatch.setattr(bg, "_task_pool", fake_pool)
        monkeypatch.setattr(
            bg, "_materialize_instance",
            lambda s, e, t, src, turn: SimpleNamespace(
                id=999, label="inst", difficulty=src.difficulty))
        monkeypatch.setattr(bg, "_mercy_deadline", lambda *a, **k: None)
        monkeypatch.setattr(shop, "_discard_task_instance", lambda *a, **k: None)
        return captured

    def test_shift_minus_one_draws_easier_tier(self, monkeypatch):
        captured = self._setup(monkeypatch)
        tile = EventBoardTile(event_id=E, idx=3, difficulty="earth", task_id=None)
        old = EventTask(id=100, config='{}', event_id=E)
        pos = _pos(tile=3, status="active", task=100)
        s = FakeSession(EventBoardTile=[tile], EventTask=[old])
        item = SimpleNamespace(effect="reroll_task",
                               effect_config='{"difficulty_shift": -1}')
        res = shop._use_reroll_task(s, None, E, 1, pos, item, _fixed(6),
                                    rng=random.Random(1))
        assert captured["difficulty"] == "water"   # earth shifted one easier
        assert res["task_id"] == 999
        assert pos.current_task_id == 999

    def test_no_shift_same_tier(self, monkeypatch):
        captured = self._setup(monkeypatch)
        tile = EventBoardTile(event_id=E, idx=3, difficulty="fire", task_id=None)
        old = EventTask(id=100, config='{}', event_id=E)
        pos = _pos(tile=3, status="active", task=100)
        s = FakeSession(EventBoardTile=[tile], EventTask=[old])
        item = SimpleNamespace(effect="reroll_task",
                               effect_config='{"difficulty_shift": 0}')
        shop._use_reroll_task(s, None, E, 1, pos, item, _fixed(6),
                              rng=random.Random(1))
        assert captured["difficulty"] == "fire"


# =========================================================================== #
# Engine movement modifiers: extra_dice / choose_roll drained in perform_roll
# =========================================================================== #
class TestMovementModifiers:
    def test_consume_extra_dice(self):
        eff = _eff(id=1, target_team_id=1, effect_type="extra_dice",
                   effect_config='{"extra_dice": 2}')
        s = FakeSession(EventBoardEffect=[eff])
        assert bg._consume_extra_dice(s, E, 1) == 2
        assert eff.status == "consumed"
        assert bg._consume_extra_dice(s, E, 1) == 0  # nothing left

    def test_consume_choose_roll(self):
        eff = _eff(id=1, target_team_id=1, effect_type="choose_roll",
                   effect_config='{"value": 5}')
        s = FakeSession(EventBoardEffect=[eff])
        assert bg._consume_choose_roll(s, E, 1) == 5
        assert eff.status == "consumed"
        assert bg._consume_choose_roll(s, E, 1) is None

    def test_perform_roll_choose_roll_forces_value(self):
        pos = _pos(team=1, tile=0, status="awaiting_roll")
        eff = _eff(id=1, target_team_id=1, effect_type="choose_roll",
                   effect_config='{"value": 5}')
        s = FakeSession(EventBoardPosition=[pos], EventBoardEffect=[eff])
        # A fixed_step board would ignore extra dice; choose_roll still forces.
        settings = bg.board_settings({"movement": {"dice_count": 1, "dice_sides": 6}})
        # Monkeypatch tiles via engine's load_tiles -> query EventBoardTile.
        s.data[EventBoardTile] = _tiles(10)
        summary = bg.perform_roll(s, None, E, 1, settings=settings)
        assert summary["dice"] == [5]
        assert summary["to"] == 5
        assert eff.status == "consumed"

    def test_perform_roll_extra_dice_adds_die(self):
        pos = _pos(team=1, tile=0, status="awaiting_roll")
        eff = _eff(id=1, target_team_id=1, effect_type="extra_dice",
                   effect_config='{"extra_dice": 1}')
        s = FakeSession(EventBoardPosition=[pos], EventBoardEffect=[eff],
                        EventBoardTile=_tiles(30))
        settings = bg.board_settings({"movement": {"dice_count": 1, "dice_sides": 6}})
        summary = bg.perform_roll(s, None, E, 1, settings=settings,
                                  rng=random.Random(7))
        assert len(summary["dice"]) == 2   # base die + one extra
        assert eff.status == "consumed"


# =========================================================================== #
# coin_toll: passed-team debit + roadblock coexistence
# =========================================================================== #
class TestCoinToll:
    def test_apply_coin_toll_debits_passed_teams(self):
        mover = EventTeam(id=1, event_id=E, coins=100)
        victim = EventTeam(id=2, event_id=E, coins=100)
        toll = _eff(id=9, source_team_id=1, target_team_id=1,
                    effect_type="coin_toll", effect_config='{"coins_per_team": 25}')
        vpos = _pos(team=2, tile=3)
        s = FakeSession(EventTeam=[mover, victim], EventBoardEffect=[toll],
                        EventBoardPosition=[vpos], EventCoinLedger=[])
        res = bg._apply_coin_toll(s, E, 1, 0, 5)
        assert res["total"] == 25
        assert victim.coins == 75
        assert mover.coins == 125
        assert toll.status == "consumed"
        # Both sides get a ledger row.
        assert len(s.data[EventCoinLedger]) == 2

    def test_coin_toll_respects_victim_balance(self):
        mover = EventTeam(id=1, event_id=E, coins=0)
        victim = EventTeam(id=2, event_id=E, coins=10)
        toll = _eff(id=9, source_team_id=1, target_team_id=1,
                    effect_type="coin_toll", effect_config='{"coins_per_team": 25}')
        vpos = _pos(team=2, tile=2)
        s = FakeSession(EventTeam=[mover, victim], EventBoardEffect=[toll],
                        EventBoardPosition=[vpos], EventCoinLedger=[])
        res = bg._apply_coin_toll(s, E, 1, 0, 4)
        assert res["total"] == 10          # capped at the victim's balance
        assert victim.coins == 0
        assert mover.coins == 10

    def test_no_toll_armed_returns_none(self):
        s = FakeSession(EventBoardEffect=[], EventBoardPosition=[], EventTeam=[])
        assert bg._apply_coin_toll(s, E, 1, 0, 5) is None

    def test_coin_toll_and_roadblock_coexist(self):
        # Rival roadblock at tile 3 stops the mover; the mover's armed toll
        # still tolls the victim it passed on tile 2 before stopping.
        rb = _eff(id=5, source_team_id=9, effect_type="roadblock",
                  target_tile_idx=3,
                  effect_config='{"break_on": "pass", "stall_turns": 0}')
        toll = _eff(id=6, source_team_id=1, target_team_id=1,
                    effect_type="coin_toll", effect_config='{"coins_per_team": 25}')
        mover = EventTeam(id=1, event_id=E, coins=0)
        victim = EventTeam(id=2, event_id=E, coins=100)
        vpos = _pos(team=2, tile=2)
        pos = _pos(team=1, tile=0, status="awaiting_roll")
        s = FakeSession(EventBoardEffect=[rb, toll], EventTeam=[mover, victim],
                        EventBoardPosition=[vpos], EventCoinLedger=[])
        summary = bg._move_piece(s, E, 1, pos, _tiles(10), 0, 6, _fixed(6))
        assert summary["to"] == 3                 # stopped by the roadblock
        assert summary["roadblock"]["consumed"] is True
        assert summary["coin_toll"]["total"] == 25
        assert victim.coins == 75 and mover.coins == 25


# =========================================================================== #
# Dinh's Bulwark: expire_on_placer_move + default placement tile
# =========================================================================== #
class TestBulwarkExpiry:
    def test_placer_own_bulwark_expires_on_move(self):
        # The mover placed a land-mode bulwark off-path (tile 7); moving 0->3
        # never triggers it, but the mover's move expires it anyway.
        rb = _eff(id=5, source_team_id=1, effect_type="roadblock",
                  target_tile_idx=7,
                  effect_config=('{"break_on": "land", "stall_turns": 0, '
                                 '"visible_to_all": true, '
                                 '"expire_on_placer_move": true}'))
        pos = _pos(team=1, tile=0)
        s = FakeSession(EventBoardEffect=[rb], EventBoardPosition=[])
        summary = bg._move_piece(s, E, 1, pos, _tiles(10), 0, 3, _fixed(3))
        assert summary["to"] == 3
        assert rb.status == "expired"
        assert summary.get("expired_roadblocks") == [7]

    def test_other_teams_bulwark_not_expired(self):
        rb = _eff(id=5, source_team_id=9, effect_type="roadblock",
                  target_tile_idx=7,
                  effect_config=('{"break_on": "land", "stall_turns": 0, '
                                 '"expire_on_placer_move": true}'))
        pos = _pos(team=1, tile=0)
        s = FakeSession(EventBoardEffect=[rb], EventBoardPosition=[])
        bg._move_piece(s, E, 1, pos, _tiles(10), 0, 3, _fixed(3))
        assert rb.status == "active"  # not the mover's — untouched

    def test_expire_disabled_survives(self):
        rb = _eff(id=5, source_team_id=1, effect_type="roadblock",
                  target_tile_idx=7,
                  effect_config=('{"break_on": "land", "stall_turns": 0, '
                                 '"expire_on_placer_move": false}'))
        pos = _pos(team=1, tile=0)
        s = FakeSession(EventBoardEffect=[rb], EventBoardPosition=[])
        bg._move_piece(s, E, 1, pos, _tiles(10), 0, 3, _fixed(3))
        assert rb.status == "active"

    def test_roadblock_defaults_to_placer_tile(self, monkeypatch):
        # No target_tile_idx -> placed on the placer's current tile.
        monkeypatch.setattr(bg, "load_tiles", lambda s, e: _tiles(10))
        cfg = EventBoardConfig(event_id=E, settings=None)
        pos = _pos(team=1, tile=4, status="active")
        s = FakeSession(EventBoardEffect=[], EventBoardConfig=[cfg])
        item = SimpleNamespace(effect="roadblock",
                               effect_config='{"stall_turns": 1}')
        res = shop._use_roadblock(s, None, E, 1, pos, item, _fixed(6), target=None)
        assert res["roadblock_tile_idx"] == 4
        placed = [e for e in s.data[EventBoardEffect]
                  if e.effect_type == "roadblock"]
        assert placed and placed[0].target_tile_idx == 4


# =========================================================================== #
# cleanse
# =========================================================================== #
class TestCleanse:
    def test_cleanse_clears_freeze_only(self):
        freeze = _eff(id=1, target_team_id=1, effect_type="freeze_opponent",
                      effect_config='{"remaining": 2}')
        boost = _eff(id=2, target_team_id=1, effect_type="boost_coins",
                     effect_config='{"multiplier": 2}')
        pos = _pos(team=1, status="awaiting_roll")
        s = FakeSession(EventBoardEffect=[freeze, boost], EventBoardPosition=[pos],
                        EventBoardTile=_tiles(10))
        item = SimpleNamespace(effect="cleanse", effect_config=None)
        res = shop._use_cleanse(s, None, E, 1, pos, item, _fixed(6))
        assert "freeze_opponent" in res["cleansed"]
        assert freeze.status == "consumed"
        assert boost.status == "active"   # positive self-buff untouched

    def test_cleanse_leaves_own_coin_toll_buff(self):
        """E1 regression: coin_toll is armed on the owner (target_team_id ==
        the caster), so it is a SELF-buff — cleanse must not destroy it."""
        toll = _eff(id=3, target_team_id=1, effect_type="coin_toll",
                    effect_config='{"coins_per_team": 25}')
        pos = _pos(team=1, status="awaiting_roll")
        s = FakeSession(EventBoardEffect=[toll], EventBoardPosition=[pos],
                        EventBoardTile=_tiles(10))
        item = SimpleNamespace(effect="cleanse", effect_config=None)
        res = shop._use_cleanse(s, None, E, 1, pos, item, _fixed(6))
        assert "coin_toll" not in res["cleansed"]
        assert toll.status == "active"


# =========================================================================== #
# choose_task pending pick + apply_task_choice
# =========================================================================== #
class TestChooseTask:
    def test_choose_task_stores_pending(self, monkeypatch):
        pool = [SimpleNamespace(id=i, label=f"t{i}", difficulty="earth")
                for i in (200, 201, 202, 203)]
        monkeypatch.setattr(bg, "_task_pool", lambda s, e, d: list(pool))
        tile = EventBoardTile(event_id=E, idx=3, difficulty="earth", task_id=None)
        pos = _pos(tile=3, status="active", task=100)
        s = FakeSession(EventBoardTile=[tile])
        item = SimpleNamespace(effect="choose_task",
                               effect_config='{"candidates": 3, "same_difficulty": true}')
        res = shop._use_choose_task(s, None, E, 1, pos, item, _fixed(6),
                                    rng=random.Random(3))
        assert len(res["pending_choice"]) == 3
        stored = json.loads(pos.pending_choice)
        assert {c["difficulty"] for c in stored} == {"earth"}
        assert [c["index"] for c in stored] == [0, 1, 2]

    def test_choose_task_distinct_tiers(self, monkeypatch):
        def pool(s, e, difficulty):
            return [SimpleNamespace(id=hash(difficulty) & 0xffff,
                                    label=difficulty, difficulty=difficulty)]

        monkeypatch.setattr(bg, "_task_pool", pool)
        tile = EventBoardTile(event_id=E, idx=3, difficulty="earth", task_id=None)
        pos = _pos(tile=3, status="active", task=100)
        s = FakeSession(EventBoardTile=[tile])
        item = SimpleNamespace(effect="choose_task",
                               effect_config='{"candidates": 2, "distinct_difficulty": true}')
        res = shop._use_choose_task(s, None, E, 1, pos, item, _fixed(6),
                                    rng=random.Random(3))
        diffs = [c["difficulty"] for c in res["pending_choice"]]
        assert len(diffs) == 2 and len(set(diffs)) == 2  # distinct tiers
        assert diffs[0] == "earth"                       # current tier first

    def test_apply_task_choice_assigns(self, monkeypatch):
        monkeypatch.setattr(
            bg, "_materialize_instance",
            lambda s, e, t, src, turn: SimpleNamespace(
                id=999, label=src.label, difficulty=src.difficulty))
        monkeypatch.setattr(bg, "_mercy_deadline", lambda *a, **k: None)
        monkeypatch.setattr(shop, "_discard_task_instance", lambda *a, **k: None)
        pending = [{"index": 0, "label": "A", "task_id": 200, "difficulty": "earth"},
                   {"index": 1, "label": "B", "task_id": 201, "difficulty": "water"}]
        pos = _pos(team=1, tile=3, status="active", task=100)
        pos.pending_choice = json.dumps(pending)
        source = EventTask(id=201, event_id=E, label="B", difficulty="water",
                           config='{}')
        cfg = EventBoardConfig(event_id=E, settings=None)
        s = FakeSession(EventBoardPosition=[pos], EventTask=[source],
                        EventBoardConfig=[cfg])
        res = shop.apply_task_choice(s, None, E, 1, 1)
        assert res["task_id"] == 999
        assert res["task_label"] == "B"
        assert pos.current_task_id == 999
        assert pos.pending_choice is None

    def test_apply_task_choice_bad_index(self, monkeypatch):
        monkeypatch.setattr(shop, "_discard_task_instance", lambda *a, **k: None)
        pending = [{"index": 0, "label": "A", "task_id": 200, "difficulty": "earth"}]
        pos = _pos(team=1, tile=3, status="active", task=100)
        pos.pending_choice = json.dumps(pending)
        cfg = EventBoardConfig(event_id=E, settings=None)
        s = FakeSession(EventBoardPosition=[pos], EventBoardConfig=[cfg])
        with pytest.raises(shop.ShopError) as ei:
            shop.apply_task_choice(s, None, E, 1, 5)
        assert ei.value.status == 422


# =========================================================================== #
# buy_item: per-team cap + stock decrement
# =========================================================================== #
def _catalog_item(**kw):
    kw.setdefault("id", 1)
    kw.setdefault("key", "thing")
    kw.setdefault("name", "Thing")
    kw.setdefault("description", None)
    kw.setdefault("icon_item_id", 1)
    kw.setdefault("item_type", "utility")
    kw.setdefault("effect", "skip_task")
    kw.setdefault("effect_config", None)
    kw.setdefault("cost_coins", 100)
    kw.setdefault("type_cooldown_turns", 0)
    kw.setdefault("sort", 0)
    kw.setdefault("active", True)
    return BoardgameShopItem(**kw)


class TestBuyCapAndStock:
    def _session(self, rotation, inventory, coins=1000):
        item = _catalog_item(id=1)
        team = EventTeam(id=1, event_id=E, coins=coins)
        pos = _pos(team=1)
        cfg = EventBoardConfig(event_id=E, settings=None)
        return FakeSession(
            BoardgameShopItem=[item], EventShopRotation=list(rotation),
            EventTeam=[team], EventBoardPosition=[pos], EventBoardConfig=[cfg],
            EventTeamInventory=list(inventory), EventCoinLedger=[]), team

    def test_per_team_cap_blocks_at_limit(self):
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=None, stock=None, enabled=True,
                                stock_per_refresh=None, per_team_cap=1)
        already = EventTeamInventory(id=1, event_id=E, team_id=1, shop_item_id=1,
                                     status="owned")
        s, team = self._session([rot], [already])
        with pytest.raises(shop.ShopError) as ei:
            shop.buy_item(s, E, 1, 1, user_id=None)
        assert ei.value.status == 409
        assert "limit" in ei.value.title.lower()

    def test_under_cap_succeeds_and_debits(self):
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=None, stock=None, enabled=True,
                                stock_per_refresh=None, per_team_cap=2)
        s, team = self._session([rot], [])
        res = shop.buy_item(s, E, 1, 1, user_id=None)
        assert res["coins"] == 900
        assert team.coins == 900
        # inventory row inserted
        assert len(s.data[EventTeamInventory]) == 1

    def test_stock_decrements(self):
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=None, stock=3, enabled=True,
                                stock_per_refresh=5, per_team_cap=None)
        s, team = self._session([rot], [])
        shop.buy_item(s, E, 1, 1, user_id=None)
        assert rot.stock == 2

    def test_disabled_override_hides_item(self):
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=None, stock=None, enabled=False,
                                stock_per_refresh=None, per_team_cap=None)
        s, team = self._session([rot], [])
        # available_items drops it -> buy_item 404 not-for-sale
        with pytest.raises(shop.ShopError) as ei:
            shop.buy_item(s, E, 1, 1, user_id=None)
        assert ei.value.status == 404


class TestBuyGateAndRefund:
    def _session(self, item, *, coins=1000, inventory=(), settings=None):
        team = EventTeam(id=1, event_id=E, coins=coins)
        pos = _pos(team=1, status="active", task=100)
        cfg = EventBoardConfig(event_id=E, settings=settings)
        return FakeSession(
            BoardgameShopItem=[item], EventShopRotation=[],
            EventTeam=[team], EventBoardPosition=[pos], EventBoardConfig=[cfg],
            EventTeamInventory=list(inventory), EventCoinLedger=[]), team

    def test_buy_gate_rejects_unusable_effect(self):
        """SH2: an item whose effect has no live handler can't be bought — no
        coins are burned on a power-up that could never be used."""
        item = _catalog_item(id=1, effect="not_a_real_effect", cost_coins=100)
        s, team = self._session(item)
        with pytest.raises(shop.ShopError) as ei:
            shop.buy_item(s, E, 1, 1, user_id=None)
        assert ei.value.status == 409
        assert team.coins == 1000   # nothing spent

    def test_use_disabled_item_refunds(self):
        """SH1: an item disabled by the event's kill switch after purchase is
        refunded on use instead of stranding the coins."""
        item = _catalog_item(id=1, effect="skip_task", cost_coins=100)
        inv = EventTeamInventory(id=5, event_id=E, team_id=1, shop_item_id=1,
                                 status="owned", price_paid=100)
        settings = json.dumps({"items": {"disabled_effects": ["skip_task"]}})
        s, team = self._session(item, coins=0, inventory=[inv], settings=settings)
        res = shop.use_item(s, None, E, 1, 5, user_id=None)
        assert res["refunded"] is True and res["price_refunded"] == 100
        assert inv.status == "refunded"
        assert team.coins == 100   # coins credited back


# =========================================================================== #
# availability override semantics + maybe_refresh_shop restock
# =========================================================================== #
class TestAvailabilityAndRefresh:
    def test_item_with_no_row_is_available_at_catalog_default(self):
        item = _catalog_item(id=1, cost_coins=100)
        cfg = EventBoardConfig(event_id=E, settings=None)
        s = FakeSession(BoardgameShopItem=[item], EventShopRotation=[],
                        EventBoardConfig=[cfg])
        items = shop.available_items(s, E)
        assert len(items) == 1
        assert items[0]["cost_coins"] == 100
        assert items[0]["per_team_cap"] is None
        assert items[0]["stock"] is None

    def test_override_row_reprices_and_caps(self):
        item = _catalog_item(id=1, cost_coins=100)
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=40, stock=2, enabled=True,
                                stock_per_refresh=2, per_team_cap=3)
        cfg = EventBoardConfig(event_id=E, settings=None)
        s = FakeSession(BoardgameShopItem=[item], EventShopRotation=[rot],
                        EventBoardConfig=[cfg])
        items = shop.available_items(s, E, team_id=1)
        assert items[0]["cost_coins"] == 40
        assert items[0]["per_team_cap"] == 3
        assert items[0]["stock"] == 2
        assert items[0]["bought_by_team"] == 0

    def test_sold_out_override_hidden(self):
        item = _catalog_item(id=1)
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=None, stock=0, enabled=True,
                                stock_per_refresh=5, per_team_cap=None)
        cfg = EventBoardConfig(event_id=E, settings=None)
        s = FakeSession(BoardgameShopItem=[item], EventShopRotation=[rot],
                        EventBoardConfig=[cfg])
        assert shop.available_items(s, E) == []

    def test_maybe_refresh_restocks_when_due(self):
        cfg = EventBoardConfig(event_id=E, settings=None,
                               shop_refreshed_at=None, shop_refreshed_turn=0)
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=None, stock=0, enabled=True,
                                stock_per_refresh=5, per_team_cap=None)
        pos = _pos(team=1, turns=3)
        s = FakeSession(EventBoardConfig=[cfg], EventShopRotation=[rot],
                        EventBoardPosition=[pos])
        settings = {"shop": {"refresh_mode": "turns", "refresh_interval": 2}}
        assert shop.maybe_refresh_shop(s, E, settings) is True
        assert rot.stock == 5
        assert cfg.shop_refreshed_turn == 3

    def test_maybe_refresh_baseline_sets_marker_only(self):
        cfg = EventBoardConfig(event_id=E, settings=None,
                               shop_refreshed_at=None, shop_refreshed_turn=None)
        rot = EventShopRotation(id=1, event_id=E, shop_item_id=1,
                                price_override=None, stock=0, enabled=True,
                                stock_per_refresh=5, per_team_cap=None)
        pos = _pos(team=1, turns=3)
        s = FakeSession(EventBoardConfig=[cfg], EventShopRotation=[rot],
                        EventBoardPosition=[pos])
        settings = {"shop": {"refresh_mode": "turns", "refresh_interval": 2}}
        # First observation: baseline only, no restock.
        assert shop.maybe_refresh_shop(s, E, settings) is False
        assert rot.stock == 0
        assert cfg.shop_refreshed_turn == 3

    def test_refresh_none_mode_no_op(self):
        cfg = EventBoardConfig(event_id=E, settings=None)
        s = FakeSession(EventBoardConfig=[cfg], EventShopRotation=[])
        assert shop.maybe_refresh_shop(s, E, {"shop": {"refresh_mode": "none"}}) is False
