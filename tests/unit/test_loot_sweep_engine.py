"""Engine-integration tests for the loot_sweep v2 wiring in
services/event_engine.py — NPC-scoped matching (``match_task``), the record
gate (``_row_advances_progress``), and the breakdown helper
(``_loot_sweep_score``), driven through the REAL services/loot_sweep scoring
(injected past the conftest ``services`` stub).
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_LS_PATH = os.path.join(_ROOT, "services", "loot_sweep.py")
_ls_spec = importlib.util.spec_from_file_location("services.loot_sweep", _LS_PATH)
_ls = importlib.util.module_from_spec(_ls_spec)
sys.modules["services.loot_sweep"] = _ls
if "services" in sys.modules:
    setattr(sys.modules["services"], "loot_sweep", _ls)
_ls_spec.loader.exec_module(_ls)

_ENGINE_PATH = os.path.join(_ROOT, "services", "event_engine.py")
_spec = importlib.util.spec_from_file_location("_loot_sweep_engine_ut", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_loot_sweep_engine_ut"] = engine
_spec.loader.exec_module(engine)


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []

    def query(self, *a):
        return _Q(self.rows)


def _row(item, qty=1, rid=1, source_type="drop"):
    return SimpleNamespace(id=rid, matched_target=item, quantity=qty,
                           source_type=source_type)


# One boss group: 3 gear items (capped at 2 receipts) + a pet that scores but
# doesn't gate; group bonus 40 when all 3 gear collected once.
CONFIG = {
    "kind": "loot_sweep", "decay_percent": 20, "set_bonus_points": 0,
    "groups": [{
        "label": "Kree'arra", "npcs": ["Kree'arra"], "bonus_points": 40,
        "items": [
            {"item_name": "Armadyl helmet", "points": 9, "max_awards": 2},
            {"item_name": "Armadyl chestplate", "points": 9, "max_awards": 2},
            {"item_name": "Armadyl hilt", "points": 13, "max_awards": 2},
            {"item_name": "Pet kree'arra", "points": 60, "counts_for_group": False},
        ],
    }],
}
TASK = {"id": 3, "type": "loot_sweep", "config": CONFIG,
        "loot_sweep_index": _ls.LootSweepConfig(CONFIG).matcher_index()}


class TestMatchNpcScoping:
    def _env(self, item, npc):
        return {"kind": "drop", "data": {"item_name": item, "npc_name": npc, "quantity": 1}}

    def test_matches_from_target_npc(self):
        m = engine.match_task(TASK, self._env("Armadyl helmet", "Kree'arra"))
        assert m and m["quantity"] == 1 and m["matched_target"] == "Armadyl helmet"

    def test_rejected_from_wrong_npc(self):
        assert engine.match_task(TASK, self._env("Armadyl helmet", "Zulrah")) is None

    def test_item_not_in_set(self):
        assert engine.match_task(TASK, self._env("Twisted bow", "Kree'arra")) is None

    def test_clog_never_matches(self):
        env = {"kind": "clog", "data": {"item_name": "Armadyl helmet", "npc_name": "Kree'arra"}}
        assert engine.match_task(TASK, env) is None


PET_CONFIG = {
    "kind": "loot_sweep", "decay_percent": 20,
    "groups": [{"label": "Kree'arra", "npcs": ["Kree'arra"], "bonus_points": 40, "items": [
        {"item_name": "Armadyl helmet", "points": 9},
        {"item_name": "Pet kree'arra", "points": 60, "source": "pet", "counts_for_group": False},
    ]}],
}
PET_TASK = {"id": 5, "type": "loot_sweep", "config": PET_CONFIG,
            "loot_sweep_index": _ls.LootSweepConfig(PET_CONFIG).matcher_index()}


class TestPetSource:
    def test_pet_submission_matches_pet_item(self):
        env = {"kind": "pet", "data": {"pet_name": "Pet kree'arra"}}
        m = engine.match_task(PET_TASK, env)
        assert m and m["matched_target"] == "Pet kree'arra"

    def test_drop_does_not_match_pet_item(self):
        env = {"kind": "drop", "data": {"item_name": "Pet kree'arra", "npc_name": "Kree'arra"}}
        assert engine.match_task(PET_TASK, env) is None

    def test_pet_submission_ignores_non_pet_items(self):
        env = {"kind": "pet", "data": {"pet_name": "Armadyl helmet"}}
        assert engine.match_task(PET_TASK, env) is None


class TestLootSweepScore:
    def test_group_complete_scores_bonus(self):
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl chestplate", rid=2),
                      _row("Armadyl hilt", rid=3)])
        r = engine._loot_sweep_score(s, TASK, team_id=4)
        assert r["item_total"] == 9 + 9 + 13
        assert r["group_bonus_total"] == 40
        assert r["total"] == 71

    def test_exclude_breaks_group(self):
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl hilt", rid=3)])
        r = engine._loot_sweep_score(s, TASK, team_id=4, exclude_id=3)
        assert r["group_bonus_total"] == 0
        assert r["item_total"] == 9


class TestRowAdvances:
    def test_new_item_advances(self):
        s = _Session([_row("Armadyl helmet", rid=1)])
        assert engine._row_advances_progress(s, TASK, 4, _row("Armadyl hilt", rid=None)) is True

    def test_capped_item_dead_weight(self):
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl helmet", rid=2)])
        assert engine._row_advances_progress(s, TASK, 4, _row("Armadyl helmet", rid=None)) is False

    def test_group_completing_receipt_advances(self):
        # helmet capped, but this hilt completes the group -> +40, advances.
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl helmet", rid=2),
                      _row("Armadyl chestplate", rid=3)])
        assert engine._row_advances_progress(s, TASK, 4, _row("Armadyl hilt", rid=None)) is True
