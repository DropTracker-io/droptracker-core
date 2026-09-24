"""Request validation for the task generator routes
(``web_api.routes.event_task_generator._clean_criteria``).

The generate endpoint is a preview, but its body still drives how much work
one request does (count, list sizes), so the bounds are pinned here."""

import pytest

from web_api.common import ProblemException
from web_api.routes.event_task_generator import _clean_criteria


def test_defaults_fill_in():
    out = _clean_criteria({})
    assert out["count"] == 12
    assert out["clan_focus"] == "off"
    assert out["categories"] is None and out["kinds"] is None
    assert isinstance(out["seed"], int)
    assert out["capacity"] == pytest.approx(5 * 7 * 1.5)


def test_capacity_follows_team_days_and_activity():
    out = _clean_criteria({"team_size": 10, "days": 14, "activity": "casual"})
    assert out["capacity"] == pytest.approx(10 * 14 * 0.75)


def test_lists_become_sets_and_keep_order_for_must_include():
    out = _clean_criteria({"categories": ["raids"], "kinds": ["kc"],
                           "must_include": ["zulrah", "nex"]})
    assert out["categories"] == {"raids"}
    assert out["kinds"] == {"kc"}
    assert out["must_include"] == ["zulrah", "nex"]


@pytest.mark.parametrize("body", [
    {"count": 0},
    {"count": 101},
    {"count": True},
    {"days": 0},
    {"team_size": 501},
    {"activity": "sweaty"},
    {"mix": {"air": -1}},
    {"mix": "lots"},
    {"categories": ["moon_base"]},
    {"kinds": ["vibes"]},
    {"must_include": ["not_a_boss"]},
    {"clan_focus": "maybe"},
    {"only_tier": "diamond"},
    {"seed": -3},
    {"exclude_keys": "kc:zulrah"},
])
def test_rejects_bad_input(body):
    with pytest.raises(ProblemException):
        _clean_criteria(body)
