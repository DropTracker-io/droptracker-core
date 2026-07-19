"""Engine-integration tests for the loot_sweep wiring in
services/event_engine.py — the record gate (``_row_advances_progress``) and the
breakdown helper (``_loot_sweep_score``), driven through the REAL
services/loot_sweep scoring (injected past the conftest ``services`` stub).

The full ``_apply_loot_sweep`` / ``_revoke_loot_sweep`` side-effect paths
(notifications, SSE, redis) are covered by the pure scoring tests
(test_loot_sweep.py) + the shared apply/revoke bookkeeping they reuse; here we
pin the loot_sweep-specific engine decisions.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Inject the REAL scoring module as services.loot_sweep so the engine's lazy
# `from services.loot_sweep import ...` resolves to it instead of the MagicMock.
_LS_PATH = os.path.join(_ROOT, "services", "loot_sweep.py")
_ls_spec = importlib.util.spec_from_file_location("services.loot_sweep", _LS_PATH)
_ls = importlib.util.module_from_spec(_ls_spec)
sys.modules["services.loot_sweep"] = _ls
if "services" in sys.modules:            # the conftest stub — hang the real submodule off it
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


# One boss set: 3 gear items (points 9, capped at 2 receipts each) + a pet that
# scores but doesn't gate the set; full set pays 40, once.
TASK = {
    "id": 3, "type": "loot_sweep",
    "config": {
        "kind": "loot_sweep", "decay_percent": 20, "default_max_awards": 2,
        "set_bonus_points": 40, "set_bonus_max": 1,
        "items": [
            {"item_name": "Armadyl helmet", "points": 9},
            {"item_name": "Armadyl chestplate", "points": 9},
            {"item_name": "Armadyl hilt", "points": 13},
            {"item_name": "Pet kree'arra", "points": 60, "counts_for_set": False},
        ],
    },
}


class TestLootSweepScore:
    def test_score_from_session_rows(self):
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl chestplate", rid=2),
                      _row("Armadyl hilt", rid=3)])
        r = engine._loot_sweep_score(s, TASK, team_id=4)
        assert r["item_total"] == 9 + 9 + 13
        assert r["set_total"] == 40           # full set
        assert r["total"] == 71

    def test_exclude_id_drops_a_row(self):
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl hilt", rid=3)])
        # Excluding the hilt breaks the set and removes its item points.
        r = engine._loot_sweep_score(s, TASK, team_id=4, exclude_id=3)
        assert r["set_total"] == 0
        assert r["item_total"] == 9

    def test_include_unsaved_candidate(self):
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl chestplate", rid=2)])
        cand = _row("Armadyl hilt", rid=None)
        r = engine._loot_sweep_score(s, TASK, team_id=4, include=cand)
        assert r["set_total"] == 40


class TestLootSweepRowAdvances:
    def test_new_item_advances(self):
        s = _Session([_row("Armadyl helmet", rid=1)])
        assert engine._row_advances_progress(s, TASK, 4, _row("Armadyl hilt", rid=None)) is True

    def test_capped_item_is_dead_weight(self):
        # helmet already at its 2-receipt cap → a 3rd scores nothing and
        # completes no set (chest/hilt missing).
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl helmet", rid=2)])
        assert engine._row_advances_progress(s, TASK, 4, _row("Armadyl helmet", rid=None)) is False

    def test_second_receipt_under_cap_advances(self):
        s = _Session([_row("Armadyl helmet", rid=1)])
        assert engine._row_advances_progress(s, TASK, 4, _row("Armadyl helmet", rid=None)) is True

    def test_set_completing_receipt_advances(self):
        # helmet capped, but this hilt completes the first set → +40, advances.
        s = _Session([_row("Armadyl helmet", rid=1), _row("Armadyl helmet", rid=2),
                      _row("Armadyl chestplate", rid=3)])
        assert engine._row_advances_progress(s, TASK, 4, _row("Armadyl hilt", rid=None)) is True
