"""Unit tests for db/group_profile.py.

The bug this guards: the website's Description field wrote only
``group_configurations.group_description``, while the group page (and every
other surface) reads ``groups.description``, so an edited description saved
and changed nothing anyone could see.
"""

from unittest.mock import MagicMock

# Direct submodule import: `from db import group_profile` would resolve against
# the MagicMock that conftest stubs `db` with, not the real module.
from db.group_profile import (
    GROUP_DESCRIPTION_CONFIG_KEY,
    MAX_GROUP_DESCRIPTION_LENGTH,
    normalize_group_description,
    set_group_description,
)


class FakeGroup:
    def __init__(self, description="An Old School RuneScape group."):
        self.group_id = 363
        self.description = description


def _session(group):
    s = MagicMock()
    s.query.return_value.filter.return_value.first.return_value = group
    return s


class TestSetGroupDescription:
    def test_writes_the_column_and_returns_the_old_value(self):
        group = FakeGroup()
        before = set_group_description(_session(group), 363, "A pack of degens")
        assert before == "An Old School RuneScape group."
        assert group.description == "A pack of degens"

    def test_trims_surrounding_whitespace(self):
        group = FakeGroup()
        set_group_description(_session(group), 363, "  PvM clan \n")
        assert group.description == "PvM clan"

    def test_blank_clears_the_column(self):
        group = FakeGroup()
        set_group_description(_session(group), 363, "   ")
        assert group.description is None

    def test_missing_group_is_a_no_op(self):
        assert set_group_description(_session(None), 1, "x") is None


class TestRegistry:
    def test_key_matches_the_settings_field(self):
        from web_api.config_registry import get_config_field

        field = get_config_field(GROUP_DESCRIPTION_CONFIG_KEY)
        assert field is not None
        # Longer than the column would fail (or truncate) on save.
        assert field["max_length"] == MAX_GROUP_DESCRIPTION_LENGTH == 255

    def test_normalize_blank_is_none(self):
        assert normalize_group_description(None) is None
        assert normalize_group_description("") is None
