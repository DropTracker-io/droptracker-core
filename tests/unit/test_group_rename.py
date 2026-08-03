"""Unit tests for db/group_rename.py — the shared "rename everywhere" service.

The bug this guards: the website's group-name field wrote only
``group_configurations.group_name``, while every visible surface reads the
``groups.group_name`` column, so renames appeared to save and changed nothing.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Direct submodule import: `from db import group_rename` would resolve against
# the MagicMock that conftest stubs `db` with, not the real module it registers.
from db.group_rename import (
    GROUP_NAME_CONFIG_KEY,
    MAX_GROUP_NAME_LENGTH,
    GroupRenameError,
    invalidate_group_name_caches,
    normalize_group_name,
    rename_group,
)


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeGroup:
    def __init__(self, group_id=7, group_name="Old Name"):
        self.group_id = group_id
        self.group_name = group_name


class FakeConfigRow:
    def __init__(self, group_id=7, config_value="Old Name"):
        self.group_id = group_id
        self.config_key = GROUP_NAME_CONFIG_KEY
        self.config_value = config_value


class FakeSession:
    """Enough SQLAlchemy surface for rename_group: a group lookup, a config-row
    lookup, add(), execute() and a no-op SAVEPOINT."""

    def __init__(self, group=None, config_row=None, xf_raises=False):
        self._group = group
        self._config_row = config_row
        self.added = []
        self.executed = []
        self.xf_raises = xf_raises

    # query(Model).filter(...).first()
    def query(self, model):
        # rename_group queries Group first, then GroupConfiguration.
        result = self._group if not self._group_consumed else self._config_row
        self._group_consumed = True
        chain = MagicMock()
        chain.filter.return_value.first.return_value = result
        return chain

    _group_consumed = False

    def add(self, obj):
        self.added.append(obj)

    def execute(self, statement, params=None):
        if self.xf_raises:
            raise RuntimeError("xenforo unreachable")
        self.executed.append((str(statement), params))

    def begin_nested(self):
        session = self

        class _Savepoint:
            def __enter__(self_inner):
                return session

            def __exit__(self_inner, exc_type, exc, tb):
                return False  # propagate; rename_group catches it

        return _Savepoint()


# ── normalize_group_name ──────────────────────────────────────────────────────

class TestNormalizeGroupName:
    def test_trims_surrounding_whitespace(self):
        assert normalize_group_name("  Sailing warriors  ") == "Sailing warriors"

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_rejects_empty(self, raw):
        with pytest.raises(GroupRenameError):
            normalize_group_name(raw)

    def test_rejects_longer_than_the_column(self):
        with pytest.raises(GroupRenameError):
            normalize_group_name("x" * (MAX_GROUP_NAME_LENGTH + 1))

    def test_accepts_exactly_the_column_width(self):
        name = "x" * MAX_GROUP_NAME_LENGTH
        assert normalize_group_name(name) == name

    def test_trailing_space_does_not_count_toward_the_limit(self):
        name = "x" * MAX_GROUP_NAME_LENGTH
        assert normalize_group_name(name + "   ") == name


# ── rename_group ──────────────────────────────────────────────────────────────

class TestRenameGroup:
    def test_writes_the_canonical_column(self):
        group = FakeGroup(group_name="Noobs With Keyboards")
        s = FakeSession(group=group, config_row=FakeConfigRow())
        result = rename_group(s, 7, "NWK")

        assert group.group_name == "NWK"
        assert result.before == "Noobs With Keyboards"
        assert result.after == "NWK"
        assert result.changed is True

    def test_updates_the_existing_config_row(self):
        row = FakeConfigRow(config_value="stale")
        s = FakeSession(group=FakeGroup(), config_row=row)
        rename_group(s, 7, "New Name")
        assert row.config_value == "New Name"
        assert s.added == []  # updated in place, not duplicated

    def test_creates_the_config_row_when_missing(self):
        # Most groups have never had one — the field rendered blank.
        s = FakeSession(group=FakeGroup(), config_row=None)
        rename_group(s, 7, "New Name")
        assert len(s.added) == 1

    def test_updates_the_xenforo_mirror(self):
        s = FakeSession(group=FakeGroup(), config_row=FakeConfigRow())
        rename_group(s, 7, "New Name")
        assert len(s.executed) == 1
        sql, params = s.executed[0]
        assert "xenforo.dt_player_group" in sql
        assert params["name"] == "New Name"
        assert params["group_id"] == 7

    def test_xenforo_failure_does_not_abort_the_rename(self):
        # The forum is secondary: a permissions/connectivity problem there must
        # not roll back the name the admin actually asked for.
        group = FakeGroup()
        s = FakeSession(group=group, config_row=FakeConfigRow(), xf_raises=True)
        result = rename_group(s, 7, "New Name")
        assert group.group_name == "New Name"
        assert result.after == "New Name"

    def test_trims_before_storing(self):
        group = FakeGroup()
        s = FakeSession(group=group, config_row=FakeConfigRow())
        rename_group(s, 7, "  Spaced Out  ")
        assert group.group_name == "Spaced Out"

    def test_same_name_still_resyncs_the_mirrors(self):
        # How groups that drifted before this existed get healed on next save.
        row = FakeConfigRow(config_value="something else entirely")
        s = FakeSession(group=FakeGroup(group_name="Real Name"), config_row=row)
        result = rename_group(s, 7, "Real Name")
        assert result.changed is False
        assert row.config_value == "Real Name"
        assert len(s.executed) == 1

    def test_rejects_unknown_group(self):
        s = FakeSession(group=None, config_row=None)
        with pytest.raises(GroupRenameError):
            rename_group(s, 999, "New Name")

    def test_rejects_invalid_name_before_touching_the_session(self):
        group = FakeGroup(group_name="Untouched")
        s = FakeSession(group=group, config_row=FakeConfigRow())
        with pytest.raises(GroupRenameError):
            rename_group(s, 7, "   ")
        assert group.group_name == "Untouched"
        assert s.executed == []


# ── invalidate_group_name_caches ──────────────────────────────────────────────

class TestInvalidateCaches:
    def test_invalidates_the_group_config_cache(self):
        with patch("utils.group_config.invalidate") as invalidate:
            invalidate_group_name_caches(7)
        invalidate.assert_called_once_with(7)

    def test_invalidates_the_web_api_slug_cache_when_in_that_process(self):
        fake_common = MagicMock()
        with patch.dict(sys.modules, {"web_api.common": fake_common}):
            invalidate_group_name_caches(7)
        fake_common.cache_delete.assert_called_once_with("canonslug:group:7")

    def test_never_imports_the_web_stack_outside_it(self):
        # Bot/worker processes call this too; it must not pull web_api in.
        with patch.dict(sys.modules):
            sys.modules.pop("web_api.common", None)
            invalidate_group_name_caches(7)
            assert "web_api.common" not in sys.modules
