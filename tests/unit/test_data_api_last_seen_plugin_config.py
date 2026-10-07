"""identity.last_seen and the plugin_config section of the data API.

Both were added after the API had live integrations, so beyond their own
behaviour these pin the backwards-compatibility promises:

* every identity field that existed before is still there, unchanged;
* include=all expands to exactly the sections it did before (plugin_config
  is opt-in by name, like discord);
* identity stays free, so no existing caller's budget moves;
* last_seen can never take identity down: if its table or grant is missing,
  callers get last_seen: null and everything else as before.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sect = _load("_real_sections_last_seen", "data_api/sections.py")

# What include=all meant before plugin_config existed. Changing this list
# changes every include=all caller's payload and cost.
ALL_BEFORE = [
    "identity", "stats", "clog", "clog_slots", "combat_achievements", "quests",
    "diaries", "personal_bests", "points", "badges", "pets", "deaths", "meta",
    "loot", "loot_npcs", "loot_items",
]
IDENTITY_FIELDS_BEFORE = {
    "player_id", "name", "account_type", "combat_level", "total_level", "ehb",
    "first_seen", "last_synced",
}


class _Session:
    """Canned result sets, one per execute() call, in order. An Exception
    in the list is raised by that call instead."""

    def __init__(self, *results):
        self.results = list(results)
        self.queries = []
        self.rolled_back = 0

    def execute(self, statement):
        self.queries.append(str(statement))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return list(result)

    def rollback(self):
        self.rolled_back += 1


def _identity_row(pid, synced=None):
    # player_id, name, total_level, ehb, log_slots, date_added,
    # players.account_type, state.account_type, combat_level, last_synced_at
    return (pid, f"P{pid}", 2000, 12.5, 900, datetime(2024, 1, 1), None, 0, 126, synced)


class TestIdentityLastSeen:
    def test_existing_fields_are_unchanged_and_last_seen_is_added(self):
        session = _Session([_identity_row(1)], [(1, datetime(2026, 10, 7, 12, 5))])
        out = sect._load_identity(session, [1], {})[1]
        assert IDENTITY_FIELDS_BEFORE <= set(out)
        assert set(out) - IDENTITY_FIELDS_BEFORE == {"last_seen"}
        assert out["first_seen"] == "2024-01-01T00:00:00"
        assert out["last_synced"] is None
        assert out["last_seen"] == "2026-10-07T12:05:00"

    def test_the_newer_of_the_tracker_and_the_account_sync_wins(self):
        synced = datetime(2026, 10, 7, 13, 0)
        session = _Session([_identity_row(1, synced), _identity_row(2, synced)],
                           [(1, datetime(2026, 10, 7, 12, 0)),
                            (2, datetime(2026, 10, 7, 14, 0))])
        out = sect._load_identity(session, [1, 2], {})
        assert out[1]["last_seen"] == "2026-10-07T13:00:00"
        assert out[2]["last_seen"] == "2026-10-07T14:00:00"

    def test_never_seen_is_null(self):
        out = sect._load_identity(_Session([_identity_row(1)], []), [1], {})
        assert out[1]["last_seen"] is None

    def test_a_zero_date_sync_is_ignored(self):
        session = _Session([_identity_row(1, "0000-00-00 00:00:00")],
                           [(1, datetime(2026, 10, 1))])
        out = sect._load_identity(session, [1], {})[1]
        assert out["last_seen"] == "2026-10-01T00:00:00"
        assert out["last_synced"] is None

    def test_a_missing_table_costs_last_seen_not_identity(self):
        session = _Session([_identity_row(1, datetime(2026, 9, 1))],
                           RuntimeError("Table 'data.player_last_seen' doesn't exist"))
        out = sect._load_identity(session, [1], {})[1]
        assert out["name"] == "P1"
        # The account sync still answers when the tracker cannot.
        assert out["last_seen"] == "2026-09-01T00:00:00"
        assert session.rolled_back == 1

    def test_two_queries_for_the_whole_page(self):
        session = _Session([_identity_row(i) for i in range(1, 51)], [])
        sect._load_identity(session, list(range(1, 51)), {})
        assert len(session.queries) == 2


SNAPSHOT = {
    "v": 1,
    "settings": {"Tracking": {"lootEmbeds": True}, "Events": {"eventDisplayMode": "POPUP"}},
    "customized": ["eventDisplayMode"],
    "env": {"runelite_version": "1.13.1", "resizable": True,
            "custom_api_endpoint": "https://private.example"},
    "hash": "abc",
}


def _config_row(pid, used_api=1):
    return (pid, json.dumps(SNAPSHOT), "6.0.16", "1.13.1", used_api, datetime(2026, 10, 6, 9, 30))


class TestPluginConfigSection:
    def test_shape_for_a_group_key(self):
        out = sect._load_plugin_config(_Session([_config_row(5)]), [5], {"key_scope": "group"})
        assert out[5] == {
            "captured_at": "2026-10-06T09:30:00",
            "plugin_version": "6.0.16",
            "runelite_version": "1.13.1",
            "transport": "api",
            "settings": SNAPSHOT["settings"],
            "customized": ["eventDisplayMode"],
            "env": SNAPSHOT["env"],
        }

    def test_no_previous_snapshot_and_no_hash(self):
        out = sect._load_plugin_config(_Session([_config_row(5)]), [5], {"key_scope": "user"})
        assert "previous_captured_at" not in out[5]
        assert "changed_since_previous" not in out[5]
        assert "hash" not in out[5]

    def test_user_and_group_keys_get_the_custom_api_endpoint(self):
        for scope in ("user", "group"):
            out = sect._load_plugin_config(_Session([_config_row(5)]), [5], {"key_scope": scope})
            assert out[5]["env"]["custom_api_endpoint"] == "https://private.example"

    def test_global_keys_do_not(self):
        out = sect._load_plugin_config(_Session([_config_row(5)]), [5], {"key_scope": "global"})
        assert "custom_api_endpoint" not in out[5]["env"]
        assert out[5]["env"]["runelite_version"] == "1.13.1"

    def test_an_unknown_scope_fails_closed(self):
        out = sect._load_plugin_config(_Session([_config_row(5)]), [5], {})
        assert "custom_api_endpoint" not in out[5]["env"]

    def test_transport(self):
        rows = [_config_row(1, 1), _config_row(2, 0), _config_row(3, None)]
        out = sect._load_plugin_config(_Session(rows), [1, 2, 3], {"key_scope": "group"})
        assert [out[i]["transport"] for i in (1, 2, 3)] == ["api", "webhook", None]

    def test_a_player_without_a_snapshot_is_null_in_the_response(self):
        session = _Session([_config_row(5)])
        merged = sect.load_sections(session, ["plugin_config"], [5, 6], {"key_scope": "group"})
        assert merged[6]["plugin_config"] is None
        assert merged[5]["plugin_config"]["plugin_version"] == "6.0.16"

    def test_unreadable_json_degrades_to_empty_fields(self):
        row = (5, "{not json", "6.0.16", "1.13.1", 1, datetime(2026, 10, 6))
        out = sect._load_plugin_config(_Session([row]), [5], {"key_scope": "group"})
        assert out[5]["settings"] == {} and out[5]["env"] == {}


class TestBackwardsCompatibility:
    def test_include_all_is_exactly_what_it_was(self):
        assert sect.parse_include("all") == ALL_BEFORE

    def test_plugin_config_must_be_asked_for_by_name(self):
        assert sect.REGISTRY["plugin_config"].in_all is False
        assert sect.parse_include("plugin_config") == ["identity", "plugin_config"]

    def test_the_default_is_still_identity_alone(self):
        assert sect.parse_include("") == ["identity"]

    def test_identity_is_still_free_and_plugin_config_is_priced(self):
        assert sect.REGISTRY["identity"].cost == 0
        assert sect.REGISTRY["plugin_config"].cost >= 1
        assert sect.cost_of(ALL_BEFORE, 100) == sum(
            sect.REGISTRY[k].cost for k in ALL_BEFORE) * 100


class TestSitePayloadUnchanged:
    def test_the_site_endpoint_body_is_byte_for_byte_what_it_was(self):
        """The shared formatter must not change the website's response."""
        from web_api.routes.plugin_config import payload

        row = SimpleNamespace(
            config_json=json.dumps(SNAPSHOT), previous_config_json=None,
            captured_at=datetime(2026, 10, 6, 9, 30), previous_captured_at=None,
            plugin_version="6.0.16", runelite_version="1.13.1", used_api=False)
        body = payload(SimpleNamespace(player_id=7, player_name="Zezima"), row)
        expected = {
            "player": {"id": 7, "name": "Zezima"},
            "snapshot": {
                "captured_at": "2026-10-06T09:30:00Z",
                "plugin_version": "6.0.16",
                "runelite_version": "1.13.1",
                "transport": "webhook",
                "settings": SNAPSHOT["settings"],
                "customized": ["eventDisplayMode"],
                # The site is staff and the player's own clan admins: it keeps
                # the custom endpoint.
                "env": SNAPSHOT["env"],
                "previous_captured_at": None,
                "changed_since_previous": [],
            },
        }
        assert json.dumps(body) == json.dumps(expected)
