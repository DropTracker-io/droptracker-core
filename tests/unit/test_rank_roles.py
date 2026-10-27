"""services/rank_roles: clan rank → Discord role sync.

The rules the owner settled (dev tracker #38): highest rank wins across a
member's accounts, an unmapped rank falls back to the next mapped rank below,
single vs ladder, strict removal with an exempt role, roles only ever created
or deleted when the group asked, deletion only for roles we created and only
after a grace period, and bad rank data never strips a clan.
"""
import asyncio
import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    # conftest stubs the `services` package, so load the real module by path.
    name = "_rank_roles_ut"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, "services", "rank_roles.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load()

GUILD = "900000000000000001"
R_OWNER = "100000000000000001"
R_GENERAL = "100000000000000002"
R_RECRUIT = "100000000000000003"
R_MEMBER = "100000000000000004"
R_EXEMPT = "100000000000000009"
U1, U2, U3 = "200000000000000001", "200000000000000002", "200000000000000003"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cfg(**over):
    cfg = {
        "enabled": True, "mode": "single", "create_roles": False, "exempt_role_id": None,
        "ranks": [
            {"rank": "owner", "role_id": R_OWNER},
            {"rank": "general", "role_id": R_GENERAL},
            {"rank": "captain", "role_id": None},          # unmapped: falls back below
            {"rank": "recruit", "role_id": R_RECRUIT},
            {"rank": "member", "role_id": None},
        ],
    }
    cfg.update(over)
    return rr.parse_config(cfg)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_parse_config_tolerates_garbage_and_defaults_off():
    for raw in (None, "", "not json", "[]", b"{}", {"ranks": "x"}):
        cfg = rr.parse_config(raw)
        assert cfg["enabled"] is False and cfg["ranks"] == [] and cfg["mode"] == "single"


def test_parse_config_normalizes_ranks_and_drops_duplicates():
    cfg = rr.parse_config({"ranks": [{"rank": "Deputy Owner", "role_id": "123"},
                                     {"rank": "deputy_owner", "role_id": R_OWNER}]})
    assert [e["rank"] for e in cfg["ranks"]] == ["deputy_owner"]
    assert cfg["ranks"][0]["role_id"] is None  # "123" is not a snowflake
    assert cfg["ranks"][0]["name"] == "Deputy Owner"


def test_rank_label():
    assert rr.rank_label("deputy_owner") == "Deputy Owner"
    assert rr.rank_label("gnome_child") == "Gnome Child"
    assert rr.rank_label("member") == "Clan Member"


def _errors(data, **kw):
    with pytest.raises(rr.RankRoleConfigError) as exc:
        rr.validate_config(data, **kw)
    return " ".join(exc.value.errors)


def test_validate_refuses_one_role_on_two_ranks_and_everyone():
    msg = _errors({"ranks": [{"rank": "owner", "role_id": R_OWNER},
                             {"rank": "general", "role_id": R_OWNER}]})
    assert "same role" in msg
    assert "@everyone" in _errors({"ranks": [{"rank": "owner", "role_id": GUILD}]}, guild_id=GUILD)


def test_validate_refuses_exempt_role_that_is_also_mapped():
    assert "exempt" in _errors({"exempt_role_id": R_OWNER,
                                "ranks": [{"rank": "owner", "role_id": R_OWNER}]})


def test_validate_create_needs_the_switch():
    assert "Create missing roles" in _errors({"ranks": [{"rank": "owner", "create": True}]})
    cfg = rr.validate_config({"create_roles": True, "ranks": [{"rank": "owner", "create": True}]})
    assert cfg["ranks"][0]["create"] is True and cfg["ranks"][0]["role_id"] is None


def test_validate_role_checks_against_the_cache():
    roles = [{"id": R_OWNER, "name": "Owner", "position": 3, "permissions": str(1 << 3)},
             {"id": R_GENERAL, "name": "General", "position": 2, "permissions": "0"}]
    assert "Administrator" in _errors({"ranks": [{"rank": "owner", "role_id": R_OWNER}]}, roles=roles)
    assert "no longer exists" in _errors({"ranks": [{"rank": "owner", "role_id": R_RECRUIT}]},
                                         roles=roles)
    ok = rr.validate_config({"ranks": [{"rank": "general", "role_id": R_GENERAL}]}, roles=roles)
    assert ok["ranks"][0]["role_id"] == R_GENERAL


def test_created_flag_is_bot_owned():
    """A client can't mark a role it picked as one we created (and so
    deletable); a created role keeps the flag while its id is unchanged."""
    stored = {"ranks": [{"rank": "owner", "role_id": R_OWNER, "created": True}]}
    forged = rr.validate_config({"ranks": [{"rank": "general", "role_id": R_GENERAL, "created": True,
                                            "delete_when_empty": True}]}, stored=stored)
    assert forged["ranks"][0]["created"] is False
    assert forged["ranks"][0]["delete_when_empty"] is False  # only roles we made
    kept = rr.validate_config({"ranks": [{"rank": "owner", "role_id": R_OWNER,
                                          "delete_when_empty": True}]}, stored=stored)
    assert kept["ranks"][0]["created"] is True and kept["ranks"][0]["delete_when_empty"] is True


def test_fingerprint_follows_the_map_not_cosmetics():
    a = _cfg()
    b = _cfg(create_roles=True)
    assert rr.config_fingerprint(a) == rr.config_fingerprint(b)
    c = _cfg(mode="cumulative")
    assert rr.config_fingerprint(a) != rr.config_fingerprint(c)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
RANKS = {"alpha": "owner", "bravo": "general", "charlie": "captain", "delta": "recruit",
         "echo": "gnome_child"}


def test_highest_rank_wins_across_accounts():
    roster = [(U1, "Delta"), (U1, "Bravo")]
    assert rr.desired_roles(_cfg(), roster, RANKS) == {U1: {R_GENERAL}}


def test_unmapped_rank_falls_back_to_next_mapped_below():
    assert rr.desired_roles(_cfg(), [(U1, "Charlie")], RANKS) == {U1: {R_RECRUIT}}


def test_cumulative_gives_the_ladder():
    got = rr.desired_roles(_cfg(mode="cumulative"), [(U1, "Bravo")], RANKS)
    assert got == {U1: {R_GENERAL, R_RECRUIT}}


def test_unlisted_rank_and_unlinked_members():
    roster = [(U1, "Echo"), (None, "Alpha"), ("", "Bravo")]
    # gnome_child isn't in the leader's list: no role, but U1 is still planned
    # (empty), so a demotion takes the old role off.
    assert rr.desired_roles(_cfg(), roster, RANKS) == {U1: set()}


def test_roster_member_without_a_rank_is_member():
    cfg = _cfg()
    cfg["ranks"][-1]["role_id"] = R_MEMBER
    assert rr.desired_roles(cfg, [(U1, "Foxtrot")], RANKS) == {U1: {R_MEMBER}}
    assert rr.rank_counts(["Foxtrot", "Alpha"], RANKS)["member"] == 1


def test_names_match_through_display_equivalence():
    assert rr.desired_roles(_cfg(), [(U1, "Iron_Man")], {"iron man": "owner"}) == {U1: {R_OWNER}}


def test_roles_in_use_covers_unlinked_clanmates():
    used = rr.roles_in_use(_cfg(), [], {"zed": "owner"})
    assert used == {R_OWNER}
    pending = _cfg(create_roles=True)
    pending["ranks"][2]["create"] = True
    assert "pending:captain" in rr.roles_in_use(pending, [], {"zed": "captain"}, include_pending=True)


def test_plan_member_is_strict_but_only_for_mapped_roles():
    managed = {R_OWNER, R_GENERAL, R_RECRUIT}
    change = rr.plan_member([R_RECRUIT, "555555555555555555"], {R_GENERAL}, managed)
    assert change.adds == [R_GENERAL] and change.removes == [R_RECRUIT]
    assert rr.plan_member([R_EXEMPT, R_OWNER], set(), managed, R_EXEMPT) is None


def test_removal_guard():
    assert rr.removals_held_back(30, 100, config_changed=False, forced=False)
    assert not rr.removals_held_back(25, 100, config_changed=False, forced=False)
    assert not rr.removals_held_back(9, 12, config_changed=False, forced=False)
    assert not rr.removals_held_back(30, 100, config_changed=True, forced=False)
    assert not rr.removals_held_back(30, 100, config_changed=False, forced=True)


def test_members_to_visit():
    desired = {U1: {R_OWNER}, U2: set(), U3: {R_GENERAL}}
    applied = {U1: R_OWNER, U2: "", U3: rr.NOT_IN_SERVER + R_GENERAL, "300000000000000001": R_OWNER}
    # Routine: U1/U2 settled, U3 known to be outside the server, the
    # off-roster member still holds a role we gave.
    assert rr.members_to_visit(desired, applied, full=False) == ["300000000000000001"]
    assert rr.members_to_visit(desired, {}, full=False) == sorted([U1, U2, U3])
    assert len(rr.members_to_visit(desired, applied, full=True)) == 4


def test_role_problems():
    roles = [{"id": GUILD, "position": 0, "permissions": "0"},
             {"id": R_OWNER, "position": 10, "permissions": "0"},
             {"id": R_GENERAL, "position": 2, "permissions": str(1 << 28)},
             {"id": R_RECRUIT, "position": 1, "permissions": "0", "managed": True}]
    cfg = _cfg()
    cfg["ranks"][-1]["role_id"] = R_MEMBER
    problems = rr.role_problems(cfg, GUILD, roles, bot_top_position=5)
    assert "above" in problems[R_OWNER]
    assert "Manage Roles" in problems[R_GENERAL]
    assert "integration" in problems[R_RECRUIT]
    assert "no longer exists" in problems[R_MEMBER]


def test_bot_capabilities():
    roles = [{"id": GUILD, "position": 0, "permissions": "1024"},
             {"id": "7", "position": 8, "permissions": str(1 << 28)}]
    perms, top = rr.bot_capabilities(GUILD, roles, ["7"])
    assert perms & rr.MANAGE_ROLES and perms & 1024 and top == 8


def test_default_rank_order():
    order = rr.default_rank_order({"recruit": 40, "general": 3, "owner": 1, "deputy_owner": 2,
                                   "member": 9})
    assert order == ["owner", "deputy_owner", "general", "recruit", "member"]
    assert rr.default_rank_order({"recruit": 4, "general": 3}, ["recruit"])[:2] == ["recruit", "general"]


# --------------------------------------------------------------------------- #
# A pass against a fake Discord
# --------------------------------------------------------------------------- #
class FakeRedis:
    def __init__(self):
        self.h = {}

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hset(self, key, mapping=None, **_):
        self.h.setdefault(key, {}).update(mapping or {})

    def hdel(self, key, *fields):
        for f in fields:
            self.h.get(key, {}).pop(f, None)

    def delete(self, key):
        self.h.pop(key, None)

    def pipeline(self):
        return self

    def execute(self):
        return []


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeHttp:
    def __init__(self, roles, members, bot_roles=("BOT",)):
        self.roles = roles
        self.members = members  # discord_id -> list of role ids
        self.bot_roles = list(bot_roles)
        self.calls = []

    async def get_roles(self, guild_id):
        return [dict(r) for r in self.roles]

    async def get_member(self, guild_id, user_id):
        if str(user_id) == "1":
            return {"roles": self.bot_roles}
        if str(user_id) not in self.members:
            raise HTTPError(404)
        return {"roles": list(self.members[str(user_id)])}

    async def add_guild_member_role(self, guild_id, user_id, role_id, reason=None):
        self.calls.append(("add", str(user_id), str(role_id)))
        self.members[str(user_id)].append(str(role_id))

    async def remove_guild_member_role(self, guild_id, user_id, role_id, reason=None):
        self.calls.append(("remove", str(user_id), str(role_id)))
        self.members[str(user_id)].remove(str(role_id))

    async def create_guild_role(self, guild_id, payload, reason=None):
        new_id = "100000000000000077"
        self.calls.append(("create", payload["name"], payload["permissions"]))
        self.roles.append({"id": new_id, "name": payload["name"], "position": 1, "permissions": "0"})
        return {"id": new_id, "position": 1, "permissions": "0"}

    async def delete_guild_role(self, guild_id, role_id, reason=None):
        self.calls.append(("delete", str(role_id)))


def _guild_roles():
    return [{"id": GUILD, "name": "@everyone", "position": 0, "permissions": "0"},
            {"id": "BOT", "name": "DropTracker", "position": 20, "permissions": str(1 << 28)},
            {"id": R_OWNER, "name": "Owner", "position": 5, "permissions": "0"},
            {"id": R_GENERAL, "name": "General", "position": 4, "permissions": "0"},
            {"id": R_RECRUIT, "name": "Recruit", "position": 3, "permissions": "0"}]


@pytest.fixture
def harness(monkeypatch):
    redis = FakeRedis()
    state = {"status": None, "patches": [], "ranks": dict(RANKS), "ctx": None}
    monkeypatch.setattr(rr, "OP_PAUSE_SECONDS", 0)
    monkeypatch.setattr(rr, "FETCH_PAUSE_SECONDS", 0)
    monkeypatch.setattr(rr, "_redis", lambda: redis)
    monkeypatch.setattr(rr, "read_status", lambda gid: state["status"])
    monkeypatch.setattr(rr, "_write_status", lambda gid, st: state.__setitem__("status", json.loads(json.dumps(st))))
    monkeypatch.setattr(rr, "read_rank_map", lambda wom: state["ranks"])
    monkeypatch.setattr(rr, "load_group_context", lambda gid: state["ctx"])
    monkeypatch.setattr(rr, "patch_rank_roles", lambda gid, p: state["patches"].append(p))
    state["redis"] = redis
    return state


def _ctx(config, roster, owner=7):
    return rr.GroupContext(group_id=7, guild_id=GUILD, wom_id=55, config=config,
                           roster=roster, owner_group=owner)


def _bot(http):
    return SimpleNamespace(http=http, user=SimpleNamespace(id=1))


def test_pass_adds_and_strictly_removes(harness):
    harness["ctx"] = _ctx(_cfg(), [(U1, "Bravo"), (U2, "Delta")])
    http = FakeHttp(_guild_roles(), {U1: [R_RECRUIT], U2: [R_OWNER]})
    status = _run(rr.sync_group(_bot(http), 7, "full"))
    assert sorted(http.calls) == sorted([("add", U1, R_GENERAL), ("remove", U1, R_RECRUIT),
                                         ("add", U2, R_RECRUIT), ("remove", U2, R_OWNER)])
    assert status["state"] == "ok" and status["added"] == 2 and status["removed"] == 2
    assert harness["redis"].h[rr.applied_key(7)] == {U1: R_GENERAL, U2: R_RECRUIT}
    # A routine pass afterwards touches nobody.
    http.calls.clear()
    _run(rr.sync_group(_bot(http), 7, "changed"))
    assert http.calls == []


def test_missing_rank_cache_never_strips(harness):
    harness["ranks"] = None
    harness["ctx"] = _ctx(_cfg(), [(U1, "Bravo")])
    http = FakeHttp(_guild_roles(), {U1: [R_GENERAL]})
    status = _run(rr.sync_group(_bot(http), 7, "full"))
    assert status["state"] == "waiting_for_ranks" and http.calls == []


def test_second_group_in_a_server_does_not_sync(harness):
    harness["ctx"] = _ctx(_cfg(), [(U1, "Bravo")], owner=3)
    http = FakeHttp(_guild_roles(), {U1: []})
    assert _run(rr.sync_group(_bot(http), 7, "full"))["state"] == "conflict"
    assert http.calls == []


def test_missing_manage_roles(harness):
    harness["ctx"] = _ctx(_cfg(), [(U1, "Bravo")])
    roles = _guild_roles()
    roles[1]["permissions"] = "0"
    assert _run(rr.sync_group(_bot(FakeHttp(roles, {U1: []})), 7, "full"))["state"] == "missing_permission"


def test_exempt_member_is_left_alone(harness):
    harness["ctx"] = _ctx(_cfg(exempt_role_id=R_EXEMPT), [(U1, "Bravo")])
    http = FakeHttp(_guild_roles(), {U1: [R_EXEMPT, R_OWNER]})
    status = _run(rr.sync_group(_bot(http), 7, "full"))
    assert http.calls == [] and status["exempt"] == 1


def test_member_outside_the_server_is_marked_not_retried(harness):
    harness["ctx"] = _ctx(_cfg(), [(U1, "Bravo")])
    http = FakeHttp(_guild_roles(), {})
    status = _run(rr.sync_group(_bot(http), 7, "full"))
    assert status["not_in_server"] == 1
    assert harness["redis"].h[rr.applied_key(7)][U1] == rr.NOT_IN_SERVER + R_GENERAL
    assert rr.members_to_visit({U1: {R_GENERAL}}, harness["redis"].h[rr.applied_key(7)], full=False) == []


def test_mass_removal_is_held_back_until_forced(harness):
    roster = [(f"2000000000000001{i:02d}", f"P{i}") for i in range(20)]
    members = {did: [R_GENERAL] for did, _ in roster}
    harness["ranks"] = {f"p{i}": "general" for i in range(20)}
    harness["ctx"] = _ctx(_cfg(), roster)
    http = FakeHttp(_guild_roles(), members)
    _run(rr.sync_group(_bot(http), 7, "full"))
    assert http.calls == []
    # WOM now reports nobody with a rank (a bad sync, not 20 demotions).
    harness["ranks"] = {"someone_else": "owner"}
    harness["ctx"] = _ctx(_cfg(), roster)
    status = _run(rr.sync_group(_bot(http), 7, "changed"))
    assert status["state"] == "held_back" and status["held_back"] == 20
    assert not any(c[0] == "remove" for c in http.calls)
    status = _run(rr.sync_group(_bot(http), 7, "force"))
    assert status["removed"] == 20


def test_create_missing_role_once_in_use(harness):
    cfg = _cfg(create_roles=True)
    cfg["ranks"][2].update({"create": True, "name": "Captain"})
    harness["ctx"] = _ctx(cfg, [(U1, "Charlie")])
    http = FakeHttp(_guild_roles(), {U1: [R_RECRUIT]})
    status = _run(rr.sync_group(_bot(http), 7, "full"))
    assert ("create", "Captain", "0") in http.calls
    assert status["created"] == ["Captain"]
    assert ("add", U1, "100000000000000077") in http.calls
    assert ("remove", U1, R_RECRUIT) in http.calls
    assert harness["patches"][-1]["captain"] == {"expect_role_id": None,
                                                 "role_id": "100000000000000077", "created": True}


def test_create_reuses_a_same_named_role(harness):
    cfg = _cfg(create_roles=True)
    cfg["ranks"][2].update({"create": True, "name": "recruit"})
    cfg["ranks"][3]["role_id"] = None
    harness["ctx"] = _ctx(cfg, [(U1, "Charlie")])
    http = FakeHttp(_guild_roles(), {U1: []})
    _run(rr.sync_group(_bot(http), 7, "full"))
    assert not any(c[0] == "create" for c in http.calls)
    assert harness["patches"][-1]["captain"]["role_id"] == R_RECRUIT
    assert harness["patches"][-1]["captain"]["created"] is False


def test_empty_created_role_deleted_only_after_grace(harness, monkeypatch):
    cfg = _cfg()
    cfg["ranks"][0].update({"created": True, "delete_when_empty": True})
    cfg["ranks"][1].update({"delete_when_empty": True})  # picked, not created: never deleted
    harness["ranks"] = {"delta": "recruit"}
    harness["ctx"] = _ctx(cfg, [(U1, "Delta")])
    http = FakeHttp(_guild_roles(), {U1: [R_RECRUIT]})
    clock = [1_000_000.0]
    monkeypatch.setattr(rr.time, "time", lambda: clock[0])
    _run(rr.sync_group(_bot(http), 7, "full"))
    assert not any(c[0] == "delete" for c in http.calls)
    clock[0] += rr.DELETE_GRACE_SECONDS + 1
    harness["ctx"] = _ctx(cfg, [(U1, "Delta")])
    status = _run(rr.sync_group(_bot(http), 7, "full"))
    assert ("delete", R_OWNER) in http.calls
    assert ("delete", R_GENERAL) not in http.calls
    assert status["deleted"] == ["Owner"]
    assert harness["patches"][-1]["owner"] == {"expect_role_id": R_OWNER, "role_id": None, "created": False}


def test_empty_clock_resets_when_the_rank_returns(harness, monkeypatch):
    cfg = _cfg()
    cfg["ranks"][0].update({"created": True, "delete_when_empty": True})
    harness["ranks"] = {"delta": "recruit"}
    harness["ctx"] = _ctx(cfg, [(U1, "Delta")])
    http = FakeHttp(_guild_roles(), {U1: [R_RECRUIT]})
    clock = [1_000_000.0]
    monkeypatch.setattr(rr.time, "time", lambda: clock[0])
    _run(rr.sync_group(_bot(http), 7, "full"))
    assert "owner" in harness["redis"].h[rr.empty_key(7)]
    harness["ranks"] = {"delta": "recruit", "zed": "owner"}
    clock[0] += 60
    _run(rr.sync_group(_bot(http), 7, "full"))
    assert "owner" not in harness["redis"].h.get(rr.empty_key(7), {})
