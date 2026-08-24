"""Assembly and versioning rules for the plugin manifest."""
import json

import pytest

from services.plugin_manifest import (
    CLIENT_SECTIONS,
    DEFAULT_SECTIONS,
    assemble_sections,
    client_sections,
    manifest_payload,
    manifest_version,
)


class FakeRow:
    """Stands in for a PluginManifestSection without needing a database."""

    def __init__(self, key, payload, *, raw=None):
        self.key = key
        self.payload = raw if raw is not None else json.dumps(payload)


def test_defaults_used_when_no_rows():
    sections = assemble_sections([])
    for key, spec in DEFAULT_SECTIONS.items():
        assert sections[key] == spec["payload"]


def test_rows_override_defaults():
    sections = assemble_sections([FakeRow("quest_ids", [1, 2, 3])])
    assert sections["quest_ids"] == [1, 2, 3]
    # Untouched sections still come from the defaults.
    assert sections["sync"] == DEFAULT_SECTIONS["sync"]["payload"]


def test_unknown_section_is_assembled():
    """A section added to the table is picked up without a code change.

    Assembly, note, not delivery: what reaches a *client* is narrowed by
    CLIENT_SECTIONS (see below). Editing the payload of a section clients
    already receive still needs no deploy, which is the property that matters.
    """
    sections = assemble_sections([FakeRow("future_thing", {"a": 1})])
    assert sections["future_thing"] == {"a": 1}


def test_corrupt_row_falls_back_to_default_without_killing_manifest():
    rows = [FakeRow("quest_ids", None, raw="{not json"), FakeRow("sync", {"enabled": False})]
    sections = assemble_sections(rows)
    assert sections["quest_ids"] == DEFAULT_SECTIONS["quest_ids"]["payload"]
    # The readable row is still applied.
    assert sections["sync"] == {"enabled": False}


def test_version_is_stable_and_order_independent():
    a = manifest_version({"x": [1, 2], "y": {"p": 1, "q": 2}})
    b = manifest_version({"y": {"q": 2, "p": 1}, "x": [1, 2]})
    assert a == b


def test_version_changes_when_payload_changes():
    before = manifest_version(assemble_sections([]))
    after = manifest_version(assemble_sections([FakeRow("quest_ids", [1])]))
    assert before != after


def test_payload_includes_version_and_sections():
    payload = manifest_payload([])
    assert payload["version"] == manifest_version(client_sections([]))
    assert "combat_achievement_varps" in payload


# --------------------------------------------------------------------------- #
# What actually goes over the wire
# --------------------------------------------------------------------------- #
def test_server_only_sections_never_reach_clients():
    """The regression that motivated the allowlist.

    ``combat_achievement_tasks`` is 144KB of task registry and ``collection_log``
    another 15KB; the plugin reads neither, and both are read server-side
    straight from the database. Together they were 99.8% of a 160KB response.
    """
    payload = manifest_payload([
        FakeRow("combat_achievement_tasks", {"tasks": [{"name": "Noxious Foe"}]}),
        FakeRow("collection_log", [{"name": "Bosses", "pages": []}]),
    ])
    assert "combat_achievement_tasks" not in payload
    assert "collection_log" not in payload
    # ...and the sections clients do read are unaffected by their presence.
    assert payload["combat_achievement_varps"] == DEFAULT_SECTIONS[
        "combat_achievement_varps"]["payload"]


def test_a_new_section_is_not_served_until_it_is_opted_in():
    """Default-deny. A section the client has no field for cannot be used by it
    however we send it, so the wire is opt-in rather than opt-out."""
    payload = manifest_payload([FakeRow("future_thing", {"a": 1})])
    assert "future_thing" not in payload
    assert assemble_sections([FakeRow("future_thing", {"a": 1})])["future_thing"] == {"a": 1}


def test_version_ignores_sections_clients_cannot_see():
    """Guards the ETag against churn.

    The collection log sync rewrites its section on its own cadence. If that
    moved the version, every client would re-download the manifest for data none
    of them receive — which is the cache validator doing the opposite of its job.
    """
    before = manifest_payload([])["version"]
    after = manifest_payload([FakeRow("collection_log", [{"name": "Bosses"}])])["version"]
    assert before == after


def test_version_still_changes_when_a_served_section_changes():
    before = manifest_payload([])["version"]
    after = manifest_payload([FakeRow("sync", {"enabled": False})])["version"]
    assert before != after


def test_every_client_section_has_a_default():
    """A served key with no default would be absent whenever its row is missing,
    which reads to the plugin as "feature off" rather than "not built yet"."""
    for key in CLIENT_SECTIONS:
        assert key in DEFAULT_SECTIONS, f"{key} is served but has no default"


def test_combat_achievement_varps_are_not_a_contiguous_range():
    """Guards the reason this manifest exists.

    The CA completion varps are 3116-3128 and then a scattering of much higher
    ids. If someone ever "tidies" this into a range, tasks in the appended varps
    silently stop being tracked with no error anywhere — so assert the shape.
    """
    varps = DEFAULT_SECTIONS["combat_achievement_varps"]["payload"]
    assert len(varps) == len(set(varps)), "duplicate varp ids"
    assert varps == sorted(varps), "varps should be stored in ascending order"
    assert max(varps) - min(varps) + 1 > len(varps), "expected gaps, not a contiguous range"
    # Spot-check the boundaries against RuneLite's VarPlayerID (1.12.35).
    assert varps[0] == 3116
    assert 5673 in varps


@pytest.mark.parametrize("key", ["combat_achievement_varps", "quest_ids", "sync"])
def test_every_default_section_documents_itself(key):
    assert DEFAULT_SECTIONS[key].get("description"), f"{key} needs a description"
