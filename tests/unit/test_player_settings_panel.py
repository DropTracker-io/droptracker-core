"""Unit tests for the pure selection→storage mapping of the /settings panel
(services/player_settings_panel.py). Storage semantics must mirror the
website settings API (web_api/routes/me.py) exactly."""

import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# conftest stubs `interactions` as a flat MagicMock (not a package), so the
# module's `from interactions.api.events import ...` needs the subpath stubbed.
from unittest.mock import MagicMock  # noqa: E402

for _name in ("interactions.api", "interactions.api.events"):
    sys.modules.setdefault(_name, MagicMock())

_spec = importlib.util.spec_from_file_location(
    "_player_settings_panel_ut",
    os.path.join(_ROOT, "services", "player_settings_panel.py"))
panel = importlib.util.module_from_spec(_spec)
sys.modules["_player_settings_panel_ut"] = panel
_spec.loader.exec_module(panel)


def test_dm_selection_writes_every_key_explicitly():
    updates = panel.dm_updates_from_selection({"dm_drops", "dm_monthly_recap"})
    assert updates["dm_drops"] == "true"
    assert updates["dm_monthly_recap"] == "true"
    # Unselected keys are explicit "false" rows — including the default-on
    # opt-out key, whose absent-row state means enabled.
    assert updates["dm_clan_invites"] == "false"
    assert updates["dm_account_changes"] == "false"
    assert set(updates) == {k for k, *_ in panel.DM_OPTIONS}


def test_event_prefs_persist_disabled_only():
    types = ("event_completion", "event_line", "event_started")
    raw = panel.event_prefs_from_selection({"event_completion"}, types)
    assert json.loads(raw) == {"event_line": False, "event_started": False}
    # Everything selected -> empty object, so future types default on.
    assert json.loads(panel.event_prefs_from_selection(set(types), types)) == {}


def test_event_pref_labels_cover_all_types():
    # The panel's label table must keep up with WEB_PREF_TYPES (a new type
    # would otherwise render as its raw key).
    spec = importlib.util.spec_from_file_location(
        "_plugin_notifications_labels_ut",
        os.path.join(_ROOT, "services", "plugin_notifications.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.WEB_PREF_TYPES) == set(panel.EVENT_PREF_LABELS)
