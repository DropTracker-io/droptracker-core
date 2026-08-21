"""Caching rules for manifest-derived reference data (web_api/routes/player_state).

The collection log structure and the combat achievement registry are written by
scripts that run whenever someone gets round to running them -- long after the
web workers booted. The cache therefore has to survive being asked for a section
that does not exist yet, and the original one did not: it stored the empty list
forever, because ``[] is not None``.

That is not a hypothetical. On 2026-08-21 the workers started at 13:48, the wiki
sync wrote the structure at 14:10, and every profile went on rendering "No
collection log recorded" -- for players whose items were sitting in the database
the whole time -- because each worker had cached the empty answer at boot.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.player_state as ps


def _session(row):
    """A session whose ``query(...).filter(...).first()`` yields ``row``."""
    s = MagicMock()
    s.query.return_value.filter.return_value.first.return_value = row
    return s


def _row(payload):
    return SimpleNamespace(payload=json.dumps(payload))


@pytest.fixture(autouse=True)
def _clear_cache():
    ps._MANIFEST_SECTION_CACHE.clear()
    yield
    ps._MANIFEST_SECTION_CACHE.clear()


IDENTITY = lambda loaded: loaded if isinstance(loaded, list) else None


class TestEmptyIsNeverCached:
    def test_missing_row_yields_empty(self):
        assert ps._manifest_section(_session(None), "collection_log", IDENTITY) == []

    def test_a_later_write_is_picked_up_without_a_restart(self):
        # The whole bug: absent at boot, present once the sync runs.
        assert ps._manifest_section(_session(None), "collection_log", IDENTITY) == []
        assert ps._MANIFEST_SECTION_CACHE == {}

        structure = [{"name": "Bosses", "pages": [{"name": "Zulrah", "items": [12345]}]}]
        assert (
            ps._manifest_section(_session(_row(structure)), "collection_log", IDENTITY)
            == structure
        )

    def test_unparseable_payload_is_not_cached_either(self):
        s = _session(SimpleNamespace(payload="{not json"))
        assert ps._manifest_section(s, "collection_log", IDENTITY) == []
        assert ps._MANIFEST_SECTION_CACHE == {}

    def test_wrong_shape_is_treated_as_absent(self):
        # A dict where the caller wants a list: the extractor returns None.
        s = _session(_row({"tabs": []}))
        assert ps._manifest_section(s, "collection_log", IDENTITY) == []
        assert ps._MANIFEST_SECTION_CACHE == {}


class TestPopulatedIsCached:
    def test_second_call_does_not_hit_the_database(self):
        structure = [{"name": "Bosses", "pages": []}]
        s = _session(_row(structure))
        assert ps._manifest_section(s, "collection_log", IDENTITY) == structure
        s.query.reset_mock()

        assert ps._manifest_section(s, "collection_log", IDENTITY) == structure
        s.query.assert_not_called()

    def test_entry_expires_so_a_resync_propagates_on_its_own(self, monkeypatch):
        first = [{"name": "old", "pages": []}]
        s = _session(_row(first))
        monkeypatch.setattr(ps.time, "monotonic", lambda: 1000.0)
        assert ps._manifest_section(s, "collection_log", IDENTITY) == first

        # Past the TTL, the row is read again rather than served from the cache.
        monkeypatch.setattr(
            ps.time, "monotonic", lambda: 1000.0 + ps._MANIFEST_SECTION_TTL_SECONDS + 1
        )
        second = [{"name": "new", "pages": []}]
        assert (
            ps._manifest_section(_session(_row(second)), "collection_log", IDENTITY)
            == second
        )

    def test_sections_are_cached_independently(self):
        struct = [{"name": "Bosses", "pages": []}]
        assert ps._manifest_section(_session(_row(struct)), "collection_log", IDENTITY) == struct
        # A different key must not be served the collection log's value.
        tasks = ps._manifest_section(
            _session(None),
            "combat_achievement_tasks",
            lambda loaded: loaded.get("tasks") if isinstance(loaded, dict) else None,
        )
        assert tasks == []


class TestRealLoaders:
    def test_registry_extracts_the_tasks_list(self):
        payload = {"tasks": [{"name": "Noxious Foe", "varp": 3116, "bit": 0}], "varps": [3116]}
        assert ps._combat_achievement_registry(_session(_row(payload))) == payload["tasks"]

    def test_registry_empty_when_section_absent(self):
        assert ps._combat_achievement_registry(_session(None)) == []

    def test_structure_requires_a_list(self):
        assert ps._collection_log_structure(_session(_row({"tasks": []}))) == []
