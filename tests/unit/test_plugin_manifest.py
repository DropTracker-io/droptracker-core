"""Assembly and versioning rules for the plugin manifest."""
import json

import pytest

from services.plugin_manifest import (
    DEFAULT_SECTIONS,
    assemble_sections,
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


def test_unknown_section_is_served():
    """A section added to the table should reach clients without a code change —
    that is the entire point of the table."""
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
    assert payload["version"] == manifest_version(assemble_sections([]))
    assert "combat_achievement_varps" in payload


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
