"""Who a key may read, and who nobody may read.

The global scope exists so a partner site can pull every clan. The thing that
must survive it is the *other* gate: a player who hid themselves stays hidden
from a partner site exactly as they are from a logged-out visitor. Scope
widens which rows a key may ask for; it never overrides someone's own choice.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("_real_scope", _ROOT / "data_api" / "scope.py")
scope = importlib.util.module_from_spec(_spec)
sys.modules["_real_scope"] = scope
_spec.loader.exec_module(scope)


def _key(scope_name, *, group_id=None, owner_user_id=None):
    return {
        "scope": scope_name,
        "owner_type": scope_name,
        "group_id": group_id,
        "owner_user_id": owner_user_id,
    }


class TestGroupGate:
    def test_a_group_key_reads_only_its_own_group(self):
        key = _key("group", group_id=19)
        assert scope.key_may_read_group(key, 19)
        assert not scope.key_may_read_group(key, 84)

    def test_a_global_key_reads_any_real_group(self):
        key = _key("global")
        for gid in (19, 84, 275, 99001):
            assert scope.key_may_read_group(key, gid), gid

    def test_group_2_is_refused_to_every_scope(self):
        # It is every player on the site, not a clan. Allowing it would make
        # "read a group" a database dump with a page size.
        assert not scope.key_may_read_group(_key("global"), scope.GLOBAL_GROUP_ID)
        assert not scope.key_may_read_group(
            _key("group", group_id=scope.GLOBAL_GROUP_ID), scope.GLOBAL_GROUP_ID
        )

    def test_a_user_key_reads_no_group_roster(self):
        assert not scope.key_may_read_group(_key("user", owner_user_id=1), 19)


class TestPlayerGate:
    def test_a_global_key_short_circuits_to_true(self):
        # No session is touched: global needs no membership lookup, which is
        # also why passing None here is safe.
        assert scope.key_may_read(None, _key("global"), 12345)


class TestVisibilityIsNotPartOfScope:
    """The hidden filter must live in the queries, not in the callers."""

    def _source(self):
        return (_ROOT / "data_api" / "scope.py").read_text()

    def test_every_enumeration_query_filters_hidden_players(self):
        source = self._source()
        for fn in ("all_players_page", "group_roster_page"):
            body = source.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
            assert "p.hidden" in body, f"{fn} does not filter hidden players"
            assert "u.hidden" in body, f"{fn} does not filter hidden owners"

    def test_the_group_listing_counts_only_visible_members(self):
        body = self._source().split("def group_page(", 1)[1].split("\ndef ", 1)[0]
        assert "p.hidden" in body and "u.hidden" in body, (
            "a partner site's member count would exceed what the website shows"
        )

    def test_no_scope_can_skip_the_visibility_helper(self):
        # visible_player_ids takes no key/scope argument, so there is nowhere
        # for a scope to be honoured inside it.
        import inspect

        params = inspect.signature(scope.visible_player_ids).parameters
        assert "key" not in params and "scope" not in params
