"""Bot-side admin entries become website grants (ticket 479).

An owner who adds someone by Discord ID before that person has an account
writes only the bot's ``authed_users`` list. These pin the matching that
decides which of those entries a signing-in user is owed a grant for.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from web_api import admin_grants as ag


def _row(gid, value=None, long_value=None):
    return SimpleNamespace(group_id=gid, config_value=value, long_value=long_value)


DID = "136602347999592448"


class TestParseAuthedIds:
    @pytest.mark.parametrize("raw,expected", [
        ('["1", "2"]', ["1", "2"]),
        ("[136602347999592448]", [DID]),          # bot-era ints
        ("136602347999592448", [DID]),            # bare int
        (136602347999592448, [DID]),
        ('["1", null, ""]', ["1"]),
        ("", []), (None, []), ("not json", []), ('{"a": 1}', []),
    ])
    def test_shapes(self, raw, expected):
        assert ag.parse_authed_ids(raw) == expected

    def test_long_value_used_when_config_value_empty(self):
        assert ag.row_authed_ids(_row(1, "", f'["{DID}"]')) == [DID]


class TestPendingGroupIds:
    def test_listed_and_ungranted_is_pending(self):
        rows = [_row(339, f'["269658379981553674", "{DID}"]')]
        assert ag.pending_group_ids(DID, rows, granted_group_ids=[]) == [339]

    def test_existing_grant_is_left_alone(self):
        # An owner row must never be shadowed by a second "admin" row.
        rows = [_row(339, f'["{DID}"]')]
        assert ag.pending_group_ids(DID, rows, granted_group_ids=[339]) == []

    def test_substring_of_a_longer_id_does_not_match(self):
        # The SQL LIKE is only a prefilter; the exact match happens here.
        rows = [_row(5, f'["{DID}9"]'), _row(6, f'["9{DID}"]')]
        assert ag.pending_group_ids(DID, rows, granted_group_ids=[]) == []

    def test_deleted_groups_skipped(self):
        rows = [_row(1, f'["{DID}"]'), _row(2, f'["{DID}"]')]
        assert ag.pending_group_ids(DID, rows, [], live_group_ids=[2]) == [2]

    def test_int_entries_match(self):
        rows = [_row(7, f"[{DID}]")]
        assert ag.pending_group_ids(DID, rows, []) == [7]


class TestFindPendingGrants:
    def test_no_discord_id_never_queries(self):
        class Boom:
            def query(self, *a, **k):
                raise AssertionError("must not query")

        assert ag.find_pending_grants(Boom(), SimpleNamespace(user_id=0, discord_id=None)) == []


class TestGrantPending:
    def test_adds_admin_grant_and_audit_per_group(self):
        added = []
        s = SimpleNamespace(add=added.append)
        user = SimpleNamespace(user_id=0, discord_id=DID)  # user_id 0 is real
        assert ag.grant_pending(s, user, [339, 12]) == [339, 12]
        assert len(added) == 4  # one GroupAdmin + one AuditLog each
