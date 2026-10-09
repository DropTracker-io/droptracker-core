"""Reaction roles: reacting to a set message grants a role, live and by backfill.

The conftest stubs the ``services`` package, so both modules are loaded from
their file paths, the same way test_discord_roles.py loads its module.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"services.{name}", _ROOT / "services" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"services.{name}"] = module
    spec.loader.exec_module(module)
    return module


_load("discord_roles")
rr = _load("reaction_roles")

SPEC = rr.ReactionRole(key="test", guild_id="1", channel_id="2", message_id="3",
                       emoji="droptracker:44", role_id="55")


def _member(user_id, *roles):
    return {"user": {"id": str(user_id)}, "roles": [str(r) for r in roles]}


class FakeHttp:
    def __init__(self, reactors, members, fail=None):
        self.reactors = [{"id": str(u), **({"bot": True} if str(u).startswith("bot") else {})}
                         for u in reactors]
        self.members = members
        self.fail = fail or {}
        self.added = []
        self.reaction_calls = []

    async def get_reactions(self, channel_id, message_id, emoji, limit=100, after=None):
        self.reaction_calls.append((channel_id, message_id, emoji, after))
        start = 0
        if after is not None:
            start = next(i for i, u in enumerate(self.reactors) if u["id"] == after) + 1
        return self.reactors[start:start + limit]

    async def list_members(self, guild_id, limit=1000, after=None):
        return [] if after else self.members

    async def add_guild_member_role(self, guild_id, user_id, role_id, reason=None):
        if user_id in self.fail:
            raise HttpError(self.fail[user_id])
        self.added.append((guild_id, user_id, role_id, reason))


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def test_configured_event_viewer_role():
    spec = rr.REACTION_ROLES[0]
    assert spec.guild_id == "1172737525069135962"
    assert spec.message_id == "1558095248679375013"
    assert spec.role_id == "1347273738575679600"
    # The reactions endpoint wants name:id with no "a:" animated prefix.
    assert spec.emoji == "droptracker:1346787143778963497"
    assert spec.emoji_id == "1346787143778963497"


def test_match_needs_both_message_and_emoji():
    assert rr.match(3, 44, [SPEC]) is SPEC
    assert rr.match("3", "44", [SPEC]) is SPEC
    assert rr.match(3, 45, [SPEC]) is None
    assert rr.match(4, 44, [SPEC]) is None
    # A unicode emoji has no id.
    assert rr.match(3, None, [SPEC]) is None


def test_plan_grants_only_members_without_the_role():
    members = [_member(10), _member(11, 55), _member(12, 99)]
    # 13 reacted but left the server; 10 is listed twice.
    assert rr.plan_grants(["10", "11", "12", "13", "10"], members, "55") == ["10", "12"]


def test_reactors_paginate_and_skip_bots():
    http = FakeHttp([str(i) for i in range(250)] + ["bot1"], [])
    reactors = asyncio.run(rr.fetch_reactor_ids(http, SPEC, page_pause=0))
    assert reactors == [str(i) for i in range(250)]
    assert [c[3] for c in http.reaction_calls] == [None, "99", "199"]
    assert http.reaction_calls[0][:3] == ("2", "3", "droptracker:44")


def test_reconcile_grants_retroactively():
    http = FakeHttp(["10", "11", "12"], [_member(10), _member(11, 55), _member(12)])
    stats = asyncio.run(rr.reconcile(http, SPEC, pause=0))
    assert [a[1] for a in http.added] == ["10", "12"]
    assert all(a[0] == "1" and a[2] == "55" for a in http.added)
    assert stats["added"] == 2 and stats["reactors"] == 3


def test_reconcile_dry_run_changes_nothing():
    http = FakeHttp(["10"], [_member(10)])
    stats = asyncio.run(rr.reconcile(http, SPEC, dry_run=True, pause=0))
    assert stats["to_grant"] == 1 and http.added == []


def test_reconcile_stops_on_forbidden():
    http = FakeHttp(["10", "11", "12"], [_member(10), _member(11), _member(12)], fail={"10": 403})
    stats = asyncio.run(rr.reconcile(http, SPEC, pause=0))
    assert stats["forbidden"] == 1 and http.added == []


def test_grant_maps_missing_member():
    http = FakeHttp([], [], fail={"10": 404})
    assert asyncio.run(rr.grant(http, SPEC, 10)) == "not_member"
    assert asyncio.run(rr.grant(http, SPEC, 11)) == "added"
