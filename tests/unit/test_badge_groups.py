"""Badge-driven group membership (db/badge_groups.py).

The dev instance's Bug Testers group has no WOM group: its members are every
account of every Bug Tester badge holder. Pinned here: the env parsing is
strict, only player rows are managed, and a WOM-managed or missing group is
never touched (two membership sources for one group would fight every hour).
"""
from __future__ import annotations

import pytest

from tests.unit import _tester_db as tdb

bg = tdb.load("_badge_groups_under_test", "db", "badge_groups.py")

GID = 10000001


class TestConfiguredBadgeGroups:
    @pytest.mark.parametrize("raw,expected", [
        ('{"10000001": "bug_tester_helper"}', {GID: "bug_tester_helper"}),
        ("'{\"10000001\":\"bug_tester_helper\"}'", {GID: "bug_tester_helper"}),
        ("10000001:bug_tester_helper", {GID: "bug_tester_helper"}),
        ("10000001:bug_tester_helper, 10000003:other", {GID: "bug_tester_helper", 10000003: "other"}),
        ('"10000001:bug_tester_helper"', {GID: "bug_tester_helper"}),
        ("", {}),
        ("   ", {}),
        ("{not json", {}),
        ('["10000001"]', {}),
        ('{"abc": "bug_tester_helper"}', {}),
        ('{"10000001": ""}', {}),
        ('{"2": "bug_tester_helper", "1": "x"}', {}),
    ])
    def test_parsing(self, raw, expected):
        assert bg.configured_badge_groups(raw) == expected

    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("BADGE_GROUPS", f"{GID}:bug_tester_helper")
        assert bg.configured_badge_groups() == {GID: "bug_tester_helper"}

    def test_unset_is_nothing(self, monkeypatch):
        monkeypatch.delenv("BADGE_GROUPS", raising=False)
        assert bg.configured_badge_groups() == {}


@pytest.fixture()
def db(monkeypatch):
    factory = tdb.make_sessionmaker()
    tdb.install_models(monkeypatch, factory)
    with factory() as s:
        s.add_all([
            tdb.User(user_id=1, discord_id="1", auth_token="a" * 16),
            tdb.User(user_id=2, discord_id="2", auth_token="b" * 16),
            tdb.Player(player_id=10, player_name="t-main", user_id=1),
            tdb.Player(player_id=11, player_name="t-alt", user_id=1),
            tdb.Player(player_id=20, player_name="not-a-tester", user_id=2),
            tdb.Player(player_id=30, player_name="unlinked"),
            tdb.Group(group_id=GID, group_name="Bug Testers", guild_id="1"),
        ])
        s.flush()
        badge = tdb.add_badge(s)
        tdb.award(s, badge, 10)
        tdb.award(s, badge, 30)  # a badge on an account with no user makes nobody a tester
        s.commit()
    return factory


def _members(factory, gid=GID):
    with factory() as s:
        rows = s.execute(tdb.user_group_association.select()
                         .where(tdb.user_group_association.c.group_id == gid)).fetchall()
        return sorted(((r.player_id, r.user_id) for r in rows),
                      key=lambda pair: (pair[0] or 0, pair[1] or 0))


class TestSync:
    def test_every_account_of_a_holder_joins(self, db):
        with db() as s:
            assert bg.sync_badge_group(s, GID, "bug_tester_helper") == (2, 0)
            s.commit()
        assert _members(db) == [(10, None), (11, None)]

    def test_is_idempotent(self, db):
        with db() as s:
            bg.sync_badge_group(s, GID, "bug_tester_helper")
            assert bg.sync_badge_group(s, GID, "bug_tester_helper") == (0, 0)

    def test_non_holders_are_removed_but_user_rows_are_kept(self, db):
        uga = tdb.user_group_association
        with db() as s:
            s.execute(uga.insert(), [
                {"player_id": 20, "user_id": None, "group_id": GID},
                {"player_id": None, "user_id": 2, "group_id": GID},
            ])
            assert bg.sync_badge_group(s, GID, "bug_tester_helper") == (2, 1)
            s.commit()
        assert _members(db) == [(None, 2), (10, None), (11, None)], "the user row stays"

    def test_a_revoked_badge_empties_the_group(self, db):
        with db() as s:
            bg.sync_badge_group(s, GID, "bug_tester_helper")
            s.query(tdb.PlayerBadge).update({"status": "revoked", "active_key": None})
            assert bg.sync_badge_group(s, GID, "bug_tester_helper") == (0, 2)

    def test_an_inactive_badge_empties_the_group(self, db):
        with db() as s:
            bg.sync_badge_group(s, GID, "bug_tester_helper")
            s.query(tdb.Badge).update({"active": False})
            assert bg.sync_badge_group(s, GID, "bug_tester_helper") == (0, 2)

    def test_a_wom_group_is_never_managed_here(self, db):
        with db() as s:
            s.get(tdb.Group, GID).wom_id = 25493
            assert bg.sync_badge_group(s, GID, "bug_tester_helper") == (0, 0)
        assert _members(db) == []

    def test_a_missing_group_is_left_alone(self, db):
        with db() as s:
            assert bg.sync_badge_group(s, 999, "bug_tester_helper") == (0, 0)

    def test_sync_configured_commits(self, db, monkeypatch):
        monkeypatch.setenv("BADGE_GROUPS", f"{GID}:bug_tester_helper")
        with db() as s:
            assert bg.sync_configured(s) == {GID: (2, 0)}
        assert _members(db) == [(10, None), (11, None)]

    def test_sync_configured_does_nothing_when_unset(self, db, monkeypatch):
        monkeypatch.delenv("BADGE_GROUPS", raising=False)
        with db() as s:
            assert bg.sync_configured(s) == {}
        assert _members(db) == []


class TestOpsHook:
    """db/ops.update_group_members runs the rule next to group 2's, both paths."""

    def test_both_full_syncs_call_it(self):
        import ast

        source = open(f"{tdb.REPO_ROOT}/db/ops.py", encoding="utf-8").read()
        tree = ast.parse(source)
        callers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                calls = {c.func.id for c in ast.walk(node)
                         if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                if "_sync_badge_groups" in calls:
                    callers.add(node.name)
        assert {"update_group_members", "update_group_members_silent"} <= callers
