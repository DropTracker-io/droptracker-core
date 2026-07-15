"""services/event_types.py — the site-wide event-kind creation gate (web43a).

The conftest stubs ``services.event_types`` (routes import it lazily), so
these tests load the REAL module by file path — the db.entitlements pattern —
and drive it through a pre-warmed registry cache (no DB)."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent.parent / "services" / "event_types.py"
_spec = importlib.util.spec_from_file_location("_real_event_types", _PATH)
et = importlib.util.module_from_spec(_spec)
sys.modules["_real_event_types"] = et
_spec.loader.exec_module(et)


def _warm(rows: dict) -> None:
    """Pre-warm the TTL cache so no session is ever touched."""
    et._cache["rows"] = rows
    et._cache["ts"] = time.monotonic()


def _row(key, *, enabled=True, admin_only=False, test_groups=(), sort=0):
    return {
        "key": key,
        "label": key.title(),
        "description": None,
        "enabled": enabled,
        "admin_only": admin_only,
        "sort": sort,
        "test_group_ids": set(test_groups),
    }


@pytest.fixture(autouse=True)
def _reset_cache():
    yield
    et.invalidate_cache()


S = object()  # session is never used with a warm cache


class TestCreationRestricted:
    def test_enabled_public_kind_is_unrestricted(self):
        _warm({"bingo": _row("bingo")})
        assert et.creation_restricted(S, "bingo", group_id=5) is False

    def test_disabled_kind_is_restricted(self):
        _warm({"bingo": _row("bingo", enabled=False)})
        assert et.creation_restricted(S, "bingo", group_id=5) is True

    def test_admin_only_kind_is_restricted_even_while_enabled(self):
        _warm({"board_game": _row("board_game", admin_only=True)})
        assert et.creation_restricted(S, "board_game", group_id=5) is True

    def test_test_group_bypasses_disabled(self):
        _warm({"board_game": _row("board_game", enabled=False, test_groups={5})})
        assert et.creation_restricted(S, "board_game", group_id=5) is False
        assert et.creation_restricted(S, "board_game", group_id=6) is True

    def test_test_group_bypasses_admin_only(self):
        _warm({"board_game": _row("board_game", admin_only=True, test_groups={5})})
        assert et.creation_restricted(S, "board_game", group_id=5) is False

    def test_global_event_never_matches_allowlist(self):
        # group_id None (global) — restricted kinds stay superadmin-only.
        _warm({"board_game": _row("board_game", admin_only=True, test_groups={5})})
        assert et.creation_restricted(S, "board_game", group_id=None) is True

    def test_unknown_kind_is_restricted(self):
        _warm({"standard": _row("standard")})
        assert et.creation_restricted(S, "banana", group_id=5) is True


class TestIsCreatable:
    def test_superadmin_always_creatable(self):
        _warm({"board_game": _row("board_game", enabled=False)})
        assert et.is_event_type_creatable(
            S, "board_game", is_superadmin=True, group_id=None
        ) is True

    def test_non_superadmin_follows_restriction(self):
        _warm({"board_game": _row("board_game", admin_only=True, test_groups={9})})
        assert et.is_event_type_creatable(
            S, "board_game", is_superadmin=False, group_id=9
        ) is True
        assert et.is_event_type_creatable(
            S, "board_game", is_superadmin=False, group_id=10
        ) is False


class TestCreatableKinds:
    def test_annotates_every_row_sorted(self):
        _warm({
            "board_game": _row("board_game", admin_only=True, sort=2),
            "standard": _row("standard", sort=0),
            "bingo": _row("bingo", sort=1),
        })
        out = et.creatable_kinds(S, is_superadmin=False, group_id=5)
        assert [r["key"] for r in out] == ["standard", "bingo", "board_game"]
        by_key = {r["key"]: r for r in out}
        assert by_key["standard"]["creatable"] is True
        assert by_key["bingo"]["creatable"] is True
        assert by_key["board_game"]["creatable"] is False
        assert by_key["board_game"]["admin_only"] is True

    def test_superadmin_sees_everything_creatable(self):
        _warm({
            "standard": _row("standard"),
            "board_game": _row("board_game", enabled=False),
        })
        out = et.creatable_kinds(S, is_superadmin=True, group_id=None)
        assert all(r["creatable"] for r in out)


class TestCacheInvalidation:
    def test_invalidate_clears_rows(self):
        _warm({"standard": _row("standard")})
        et.invalidate_cache()
        assert et._cache["rows"] is None
