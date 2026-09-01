"""The notify_points_awarded toggle (default ON): points are never awarded silently.

Every submission processor (drop, clog, ca, pb, pet) consults
``common.points_notify_enabled`` when group points landed but the group's other
settings would not have announced the submission. What must hold:

* an **absent** row means ON — the whole point is that groups get this without
  opting in, and only an explicit "0" turns it off;
* legacy truthiness spellings are read through the same rule as every other
  boolean config (``utils.group_config.is_truthy``);
* a config-read fault fails **open** (announce) — an infrastructure wobble must
  not silently re-darken point awards, which is the exact gap this closes.
"""

from unittest.mock import MagicMock

import pytest

import data.submissions.common as common
from utils import group_config as gc


@pytest.fixture(autouse=True)
def _no_config_cache(monkeypatch):
    # points_notify_enabled reads through utils.group_config.get; patch it per
    # test rather than seeding the module's TTL cache.
    yield


def _with_stored(monkeypatch, value):
    monkeypatch.setattr(gc, "get", lambda session, group_id, key, default=None: value)


def test_absent_row_means_on(monkeypatch):
    _with_stored(monkeypatch, None)
    assert common.points_notify_enabled(MagicMock(), 42) is True


@pytest.mark.parametrize("value", ["1", "true", "TRUE"])
def test_truthy_spellings_mean_on(monkeypatch, value):
    _with_stored(monkeypatch, value)
    assert common.points_notify_enabled(MagicMock(), 42) is True


@pytest.mark.parametrize("value", ["0", "false", "off", ""])
def test_explicit_off_means_off(monkeypatch, value):
    _with_stored(monkeypatch, value)
    assert common.points_notify_enabled(MagicMock(), 42) is False


def test_config_fault_fails_open(monkeypatch):
    def _boom(session, group_id, key, default=None):
        raise RuntimeError("database went away")

    monkeypatch.setattr(gc, "get", _boom)
    assert common.points_notify_enabled(MagicMock(), 42) is True


def test_reads_the_registry_key(monkeypatch):
    # The processors and the config registry must agree on the key's spelling.
    seen = {}

    def _get(session, group_id, key, default=None):
        seen["key"] = key
        return None

    monkeypatch.setattr(gc, "get", _get)
    common.points_notify_enabled(MagicMock(), 42)
    assert seen["key"] == "notify_points_awarded"

    from web_api.config_registry import get_config_field

    field = get_config_field("notify_points_awarded")
    assert field is not None
    assert field["type"] == "boolean"
    assert field["default"] is True
