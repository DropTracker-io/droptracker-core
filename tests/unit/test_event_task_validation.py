"""Unit tests for event-task goal validation (item_collection kinds).

Focus: the ``any_of`` quantity goal and the ``groups`` combined-requirements
kind (all-of + any-of sub-lists, e.g. all godsword shards + any one hilt).
The conftest stubs ``db``; item/NPC canonicalization is monkeypatched to a
fixed known set, so no session is exercised.
"""
from __future__ import annotations

import json

import pytest

import web_api.routes.event_task_validation as etv
from web_api.common import ProblemException

KNOWN_ITEMS = {
    "Boater", "Red boater", "Orange boater",
    "Godsword shard 1", "Godsword shard 2", "Godsword shard 3",
    "Armadyl hilt", "Bandos hilt", "Saradomin hilt", "Zamorak hilt",
    "Justiciar faceguard", "Justiciar chestguard", "Justiciar legguards",
}
_BY_NORM = {n.lower(): n for n in KNOWN_ITEMS}


@pytest.fixture(autouse=True)
def _stub_lookups(monkeypatch):
    monkeypatch.setattr(
        etv, "_canonical_item",
        lambda s, name: _BY_NORM.get((name or "").strip().lower()),
    )


def _validate(body):
    return etv.validate_task_payload(None, body)


def _cfg(payload) -> dict:
    return json.loads(payload["config"])


# ── any_of quantity ──────────────────────────────────────────────────────────

def test_any_of_defaults_to_one():
    out = _validate({
        "type": "item_collection",
        "config": {"kind": "any_of", "items": ["Boater", "Red boater"]},
    })
    assert out["target_value"] == 1


def test_any_of_honors_quantity():
    # "2 boaters": any two qualifying drops from the list complete the task.
    out = _validate({
        "type": "item_collection",
        "target_value": 2,
        "config": {"kind": "any_of", "items": ["Boater", "Red boater", "Orange boater"]},
    })
    assert out["target_value"] == 2
    assert _cfg(out)["kind"] == "any_of"


def test_any_of_rejects_zero_quantity():
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection",
            "target_value": 0,
            "config": {"kind": "any_of", "items": ["Boater", "Red boater"]},
        })
    assert exc.value.status == 422


# ── groups (combined requirements) ───────────────────────────────────────────

GODSWORD_BODY = {
    "type": "item_collection",
    "config": {
        "kind": "groups",
        "groups": [
            {"mode": "all_of",
             "items": ["Godsword shard 1", "Godsword shard 2", "Godsword shard 3"]},
            {"mode": "any_of",
             "items": ["Armadyl hilt", "Bandos hilt", "Saradomin hilt", "Zamorak hilt"]},
        ],
    },
}


def test_groups_normalizes_and_sums_threshold():
    out = _validate(GODSWORD_BODY)
    cfg = _cfg(out)
    assert cfg["kind"] == "groups"
    assert [g["mode"] for g in cfg["groups"]] == ["all_of", "any_of"]
    assert cfg["groups"][0]["need"] == 3      # all three shards
    assert cfg["groups"][1]["need"] == 1      # any one hilt (default)
    assert out["target_value"] == 4           # sum of group needs
    assert out["target"] is None


def test_groups_any_of_need_carries_through():
    body = json.loads(json.dumps(GODSWORD_BODY))
    body["config"]["groups"][1]["need"] = 2
    out = _validate(body)
    assert _cfg(out)["groups"][1]["need"] == 2
    assert out["target_value"] == 5


def test_groups_reject_unknown_items():
    body = {
        "type": "item_collection",
        "config": {"kind": "groups",
                   "groups": [{"mode": "all_of", "items": ["Nonexistent thing"]}]},
    }
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert exc.value.status == 422
    assert "Nonexistent thing" in (exc.value.detail or "")


def test_groups_reject_item_in_two_groups():
    body = {
        "type": "item_collection",
        "config": {"kind": "groups", "groups": [
            {"mode": "all_of", "items": ["Godsword shard 1", "Godsword shard 2"]},
            {"mode": "any_of", "items": ["godsword shard 1", "Armadyl hilt"]},
        ]},
    }
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert exc.value.status == 422
    assert "more than one requirement group" in (exc.value.detail or "")


def test_groups_reject_bad_mode_and_empty():
    with pytest.raises(ProblemException):
        _validate({"type": "item_collection",
                   "config": {"kind": "groups", "groups": []}})
    with pytest.raises(ProblemException):
        _validate({"type": "item_collection",
                   "config": {"kind": "groups",
                              "groups": [{"mode": "some_of", "items": ["Boater"]}]}})


def test_groups_reject_too_many_groups():
    body = {
        "type": "item_collection",
        "config": {"kind": "groups", "groups": [
            {"mode": "any_of", "items": ["Boater"]} for _ in range(etv.MAX_CONFIG_GROUPS + 1)
        ]},
    }
    # (Duplicate-item rejection would also fire — group count is checked first.)
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert "requirement groups" in (exc.value.detail or "")


# ── any_path (either-or "dryness protection", suggestion #52) ─────────────────

JUSTICIAR_BODY = {
    "type": "item_collection",
    "config": {
        "kind": "any_path",
        "paths": [
            {"label": "Full set",
             "groups": [{"mode": "all_of",
                         "items": ["Justiciar faceguard", "Justiciar chestguard",
                                   "Justiciar legguards"]}]},
            {"label": "Any 5 pieces",
             "groups": [{"mode": "any_of", "need": 5,
                         "items": ["Justiciar faceguard", "Justiciar chestguard",
                                   "Justiciar legguards"]}]},
        ],
    },
}


def test_any_path_normalizes_and_pins_percentage_threshold():
    out = _validate(json.loads(json.dumps(JUSTICIAR_BODY)))
    cfg = _cfg(out)
    assert cfg["kind"] == "any_path"
    assert [p["label"] for p in cfg["paths"]] == ["Full set", "Any 5 pieces"]
    assert cfg["paths"][0]["groups"][0]["need"] == 3
    assert cfg["paths"][1]["groups"][0]["need"] == 5
    assert out["target_value"] == etv.ANY_PATH_THRESHOLD
    assert out["target"] is None


def test_any_path_allows_items_to_repeat_across_paths():
    # The same drop advancing every path is the whole point — only
    # within-path duplicates are rejected.
    out = _validate(json.loads(json.dumps(JUSTICIAR_BODY)))
    assert len(_cfg(out)["paths"]) == 2


def test_any_path_rejects_within_path_duplicates():
    with pytest.raises(ProblemException):
        _validate({
            "type": "item_collection",
            "config": {"kind": "any_path", "paths": [
                {"groups": [{"mode": "all_of", "items": ["Boater"]},
                            {"mode": "any_of", "items": ["Boater"]}]},
                {"groups": [{"mode": "any_of", "items": ["Red boater"]}]},
            ]},
        })


def test_any_path_requires_two_paths():
    for paths in ([], [{"groups": [{"mode": "any_of", "items": ["Boater"]}]}]):
        with pytest.raises(ProblemException) as exc:
            _validate({"type": "item_collection",
                       "config": {"kind": "any_path", "paths": paths}})
        assert "at least two paths" in (exc.value.detail or "")


def test_any_path_rejects_too_many_paths():
    body = {
        "type": "item_collection",
        "config": {"kind": "any_path", "paths": [
            {"groups": [{"mode": "any_of", "items": ["Boater"]}]}
            for _ in range(etv.MAX_CONFIG_PATHS + 1)
        ]},
    }
    with pytest.raises(ProblemException) as exc:
        _validate(body)
    assert "paths per task" in (exc.value.detail or "")


def test_any_path_rejects_unknown_items():
    with pytest.raises(ProblemException) as exc:
        _validate({
            "type": "item_collection",
            "config": {"kind": "any_path", "paths": [
                {"groups": [{"mode": "any_of", "items": ["Nonexistent thing"]}]},
                {"groups": [{"mode": "any_of", "items": ["Boater"]}]},
            ]},
        })
    assert exc.value.status == 422
