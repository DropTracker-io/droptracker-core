"""Unit tests for web_api/event_breakdown.py — the per-(task, team) have/need
view must agree with the engine rollups in services/event_engine.py.

Regression focus: any_of quantity folding. The engine folds full row
quantities with no per-item cap (_grouped_progress_from_rows, and the flat
`progress += quantity` apply path), but the breakdown used to clamp each
item's contribution at its per-item `required` (defaulted to 1 for
bare-string config items) — so a 53-vial stack displayed as 1 toward a
"6000× Vial of blood" path.

Both modules are loaded directly from their file paths, with the real engine
seeded into sys.modules so the breakdown's lazy `from services.event_engine
import ...` resolves past the conftest MagicMock stubs.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, *relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine = _load("_event_engine_for_breakdown", "services", "event_engine.py")

_saved_engine = sys.modules.get("services.event_engine")
sys.modules["services.event_engine"] = engine
try:
    breakdown = _load("_event_breakdown_under_test", "web_api", "event_breakdown.py")
finally:
    if _saved_engine is None:
        sys.modules.pop("services.event_engine", None)
    else:
        sys.modules["services.event_engine"] = _saved_engine


@pytest.fixture(autouse=True)
def _real_engine_module():
    """The breakdown's function-level engine imports must hit the real module,
    not the conftest stub, while each test runs."""
    saved = sys.modules.get("services.event_engine")
    sys.modules["services.event_engine"] = engine
    yield
    if saved is None:
        sys.modules.pop("services.event_engine", None)
    else:
        sys.modules["services.event_engine"] = saved


def _row(quantity, target="Vial of blood", note=None, player_id=5, rid=1):
    return SimpleNamespace(
        id=rid, quantity=quantity, matched_target=target, source_type="drop",
        note=note, player_id=player_id, created_at=None,
    )


def _breakdown(task, rows, progress=0, completed=False, pending_rows=None):
    return breakdown.build_task_breakdown(
        task, None, rows,
        SimpleNamespace(progress=progress, completed=completed),
        {"id": 1, "name": "Team"}, {}, lambda dt: 0,
        pending_rows=pending_rows,
    )


def _task(config, target_value=100, target=None):
    return {"id": 421, "type": "item_collection", "label": "t",
            "target": target, "target_value": target_value, "config": config}


# The production task this regression came from (event 35 / task 421):
# Justiciar set OR 6000 Vials of blood.
ANY_PATH_CONFIG = {
    "kind": "any_path",
    "paths": [
        {"groups": [{"mode": "all_of", "need": 3, "items": [
            "Justiciar faceguard", "Justiciar chestguard", "Justiciar legguards"]}]},
        {"groups": [{"mode": "any_of", "need": 6000, "items": ["Vial of blood"]}]},
    ],
}


class TestAnyPathQuantityFold:
    def test_stacks_fold_full_quantity(self):
        rows = [_row(51, rid=1), _row(54, rid=2), _row(53, rid=3)]
        out = _breakdown(_task(ANY_PATH_CONFIG), rows, progress=2)
        vial_path = out["paths"][1]
        assert vial_path["got"] == 158
        assert vial_path["need"] == 6000
        assert vial_path["pct"] == 158 * 100 // 6000

    def test_single_item_any_of_surfaces_group_need(self):
        out = _breakdown(_task(ANY_PATH_CONFIG), [_row(53)])
        item = out["paths"][1]["groups"][0]["items"][0]
        assert item["required"] == 6000
        assert item["obtained"] == 53
        assert not item["satisfied"]

    def test_pct_matches_engine_rollup(self):
        rows = [_row(51, rid=1), _row(54, rid=2), _row(53, rid=3)]
        out = _breakdown(_task(ANY_PATH_CONFIG), rows)
        assert out["paths"][1]["pct"] == engine._anypath_progress_from_rows(
            rows, ANY_PATH_CONFIG, 100)

    def test_all_of_path_still_distinct(self):
        rows = [_row(3, target="Justiciar faceguard", rid=1),
                _row(1, target="Justiciar faceguard", rid=2)]
        out = _breakdown(_task(ANY_PATH_CONFIG), rows)
        set_path = out["paths"][0]
        assert set_path["got"] == 1  # duplicate pieces count once
        assert set_path["need"] == 3


class TestGroupsChecklistQuantityFold:
    CONFIG = {"kind": "groups", "groups": [
        {"mode": "any_of", "need": 10, "items": ["Blood rune", "Death rune"]},
    ]}

    def test_multi_item_any_of_folds_uncapped(self):
        rows = [_row(4, target="Blood rune", rid=1),
                _row(3, target="Death rune", rid=2)]
        out = _breakdown(_task(self.CONFIG, target_value=10), rows)
        group = out["groups"][0]
        assert group["obtained"] == 7
        assert not group["satisfied"]

    def test_fold_caps_at_group_need(self):
        out = _breakdown(_task(self.CONFIG, target_value=10),
                         [_row(25, target="Blood rune")])
        group = out["groups"][0]
        assert group["obtained"] == 10
        assert group["satisfied"]

    def test_matches_engine_grouped_rollup(self):
        rows = [_row(4, target="Blood rune", rid=1),
                _row(3, target="Death rune", rid=2)]
        out = _breakdown(_task(self.CONFIG, target_value=10), rows)
        assert out["groups"][0]["obtained"] == engine._grouped_progress_from_rows(
            rows, self.CONFIG, 10)

    def test_multi_item_required_stays_one(self):
        out = _breakdown(_task(self.CONFIG, target_value=10),
                         [_row(4, target="Blood rune")])
        items = {it["name"]: it for it in out["groups"][0]["items"]}
        assert items["blood rune"]["required"] == 1


class TestFlatAnyOf:
    CONFIG = {"kind": "any_of", "items": ["Vial of blood"]}

    def test_single_item_folds_and_shows_need(self):
        out = _breakdown(_task(self.CONFIG, target_value=6000),
                         [_row(51, rid=1), _row(54, rid=2)])
        group = out["groups"][0]
        assert group["obtained"] == 105
        item = group["items"][0]
        assert item["required"] == 6000
        assert item["obtained"] == 105

    def test_multi_item_stack_can_complete(self):
        config = {"kind": "any_of", "items": ["Blood rune", "Death rune"]}
        # Engine's flat apply is `progress += quantity`, so one stack of 3
        # completes an "any 3 of" — the display must agree.
        out = _breakdown(_task(config, target_value=3),
                         [_row(3, target="Blood rune")])
        group = out["groups"][0]
        assert group["obtained"] == 3
        assert group["satisfied"]


class TestPetCollection:
    """A pet task used to be a bare N/3 meter with no list of eligible pets —
    the participant could not tell which of the 39 listed pets counted."""

    PETS = ["Baby mole", "Vorki", "Scurry", "Beaver"]

    @staticmethod
    def _pet_task(config, target_value=3, target=None):
        return {"id": 99, "type": "pet_collection", "label": "Three pets",
                "target": target, "target_value": target_value, "config": config}

    def test_lists_every_eligible_pet_as_a_checklist_row(self):
        out = _breakdown(self._pet_task({"pets": self.PETS}), [])
        assert out["structure"] == "checklist"
        assert [i["name"] for i in out["groups"][0]["items"]] == sorted(self.PETS)
        assert all(not i["satisfied"] for i in out["groups"][0]["items"])

    def test_obtained_pets_tick_their_own_row(self):
        rows = [_row(1, target="Vorki", rid=1), _row(1, target="Beaver", rid=2)]
        out = _breakdown(self._pet_task({"pets": self.PETS}), rows, progress=2)
        got = {i["name"]: i["satisfied"] for i in out["groups"][0]["items"]}
        assert got == {"Baby mole": False, "Beaver": True,
                       "Scurry": False, "Vorki": True}
        group = out["groups"][0]
        assert group["obtained"] == 2 and group["need"] == 3
        assert not group["satisfied"]

    def test_distinct_pets_not_quantities_drive_the_group(self):
        """Each pet counts once — the matcher rejects duplicates, so two rows
        for the same pet must not read as 2/3."""
        rows = [_row(1, target="Vorki", rid=1), _row(1, target="Vorki", rid=2)]
        out = _breakdown(self._pet_task({"pets": self.PETS}), rows, progress=1)
        assert out["groups"][0]["obtained"] == 1

    def test_specific_pet_target_is_a_counted_goal(self):
        out = _breakdown(self._pet_task(None, target_value=2, target="Baby mole"),
                         [_row(1, target="Baby mole")], progress=1)
        item = out["groups"][0]["items"][0]
        assert item["name"] == "Baby mole"
        assert item["required"] == 2 and item["obtained"] == 1
        assert not item["satisfied"]

    def test_category_task_expands_the_taxonomy(self):
        out = _breakdown(self._pet_task({"categories": ["skilling"]}), [])
        names = [i["name"] for i in out["groups"][0]["items"]]
        assert "Beaver" in names and "Baby mole" not in names

    def test_pending_rows_overlay_the_matching_pet(self):
        out = _breakdown(self._pet_task({"pets": self.PETS}), [],
                         pending_rows=[_row(1, target="Scurry")])
        rows = {i["name"]: i for i in out["groups"][0]["items"]}
        assert rows["Scurry"].get("pending_satisfied") is True
        assert "pending_satisfied" not in rows["Vorki"]
