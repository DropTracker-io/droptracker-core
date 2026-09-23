"""Tier and Bug Tester Discord roles: who holds what, and what the sync may touch.

The rules the owner set (2026-09-14):
  * paying a live leg of a subscribed group makes you "Patron"/"Sponsor"/
    "Supporter"; being a member of one makes you "<Tier> (Member)";
  * one tier role per person, the highest, ranked Patron > Sponsor >
    Supporter > Patron (Member) > Sponsor (Member) > Supporter (Member);
  * Fanatic (personal subscription) and Bug Tester (the badge) are held
    alongside it.

The conftest stubs the ``services`` package, so the module is loaded from its
file path, the same way test_nitro_attribution.py loads its module.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("services.discord_roles", _ROOT / "services" / "discord_roles.py")
dr = importlib.util.module_from_spec(_spec)
sys.modules["services.discord_roles"] = dr
_spec.loader.exec_module(dr)

MAP = {s.key: str(9000 + i) for i, s in enumerate(dr.ROLE_SPECS)}
FULL_MAP = {s.key: str(9000 + i) for i, s in enumerate(dr.ALL_SPECS)}


def _inputs(**kwargs):
    inputs = dr.RoleInputs(**kwargs)
    if not inputs.discord_ids:
        users = set(inputs.user_tier) | set(inputs.bug_testers) | set(inputs.registered)
        for group in list(inputs.group_payers.values()) + list(inputs.group_members.values()):
            users |= set(group)
        inputs.discord_ids = {uid: str(100000 + uid) for uid in users}
    return inputs


def _d(uid):
    return str(100000 + uid)


class TestWhoHoldsWhat:
    def test_a_payer_gets_the_subscriber_role_and_not_the_member_role(self):
        inputs = _inputs(group_tier={10: "t3"}, group_payers={10: {1}}, group_members={10: {1, 2}})
        assert dr.desired_role_keys(inputs) == {_d(1): {"patron"}, _d(2): {"patron_member"}}

    def test_only_the_highest_tier_role_is_held(self):
        inputs = _inputs(
            group_tier={10: "t3", 11: "t2", 12: "basic"},
            group_payers={11: {1}, 12: {4}},
            group_members={10: {1, 3}, 11: {4}, 12: {3}},
        )
        desired = dr.desired_role_keys(inputs)
        assert desired[_d(1)] == {"sponsor"}          # pays Sponsor, member of Patron
        assert desired[_d(3)] == {"patron_member"}    # member of Patron and Supporter
        assert desired[_d(4)] == {"supporter"}        # pays Supporter, member of Sponsor

    def test_the_precedence_is_the_order_the_owner_chose(self):
        tier_keys = [s.key for s in dr.ROLE_SPECS if s.grant in (dr.GROUP_PAYER, dr.GROUP_MEMBER)]
        assert tier_keys == ["patron", "sponsor", "supporter",
                             "patron_member", "sponsor_member", "supporter_member"]

    def test_fanatic_and_bug_tester_sit_alongside_a_tier_role(self):
        inputs = _inputs(group_tier={10: "t3"}, group_payers={10: {1}},
                         user_tier={1: "supporter"}, bug_testers={1})
        assert dr.desired_role_keys(inputs) == {_d(1): {"patron", "fanatic", "bug_tester"}}

    def test_a_personal_subscription_to_another_tier_is_not_fanatic(self):
        inputs = _inputs(user_tier={5: "something_else"}, group_tier={10: "dragon"},
                         group_members={10: {6}})
        assert dr.desired_role_keys(inputs) == {}

    def test_users_without_a_discord_account_get_nothing(self):
        inputs = dr.RoleInputs(group_tier={10: "t3"}, group_members={10: {1, 2}},
                               discord_ids={1: "111"})
        assert dr.desired_role_keys(inputs) == {"111": {"patron_member"}}

    def test_the_system_groups_never_hand_out_roles(self):
        # Group 2 holds every player on the site.
        inputs = _inputs(group_tier={2: "t3", 1: "t2"}, group_members={2: {1, 2}, 1: {3}})
        assert dr.desired_role_keys(inputs) == {}


def _member(uid, roles=(), bot=False):
    return {"user": {"id": str(uid), "bot": bot, "username": f"u{uid}"}, "roles": [str(r) for r in roles]}


class TestPlan:
    def test_only_managed_roles_change(self):
        members = [_member(1, roles=["555", MAP["patron_member"]])]
        plan = dr.plan_role_changes({"1": {"sponsor_member"}}, members, MAP)
        assert plan.adds == [("1", "sponsor_member")]
        assert plan.removes == [("1", "patron_member")]

    def test_bots_are_never_touched(self):
        members = [_member(1, roles=[MAP["patron"]], bot=True)]
        plan = dr.plan_role_changes({"1": {"sponsor"}}, members, MAP)
        assert plan.adds == [] and plan.removes == []

    def test_people_not_on_the_server_are_counted_not_planned(self):
        plan = dr.plan_role_changes({"1": {"patron"}, "2": {"fanatic"}}, [_member(1)], MAP)
        assert plan.adds == [("1", "patron")]
        assert plan.not_in_guild == 1

    def test_a_role_missing_from_the_map_is_never_planned(self):
        partial = {k: v for k, v in MAP.items() if k != "patron_member"}
        plan = dr.plan_role_changes({"1": {"patron_member"}}, [_member(1)], partial)
        assert plan.adds == [] and plan.not_in_guild == 0

    def test_a_mass_removal_is_held_back_but_additions_still_happen(self):
        members = [_member(i, roles=[MAP["bug_tester"]]) for i in range(1, 6)] + [_member(9)]
        plan = dr.plan_role_changes({"9": {"patron"}}, members, MAP, max_removals=3)
        assert plan.adds == [("9", "patron")]
        assert plan.removes == []
        assert len(plan.held_back) == 5

    def test_up_to_the_limit_removals_go_ahead(self):
        members = [_member(i, roles=[MAP["bug_tester"]]) for i in range(1, 4)]
        plan = dr.plan_role_changes({}, members, MAP, max_removals=3)
        assert len(plan.removes) == 3 and plan.held_back == []


class TestRegistered:
    """Registered = at least one claimed RSN; Unregistered = everyone else here."""

    def test_a_claimed_rsn_makes_you_registered_alongside_other_roles(self):
        inputs = _inputs(group_tier={10: "t3"}, group_members={10: {1}}, registered={1, 2})
        assert dr.desired_role_keys(inputs) == {_d(1): {"patron_member", "registered"},
                                                _d(2): {"registered"}}

    def test_the_owner_user_zero_is_registered(self):
        inputs = _inputs(registered={0})
        assert dr.desired_role_keys(inputs) == {_d(0): {"registered"}}

    def test_everyone_else_on_the_server_is_unregistered(self):
        members = [_member(1), _member(2), _member(3, roles=[FULL_MAP["registered"]])]
        desired = {"1": {"registered"}}
        plan = dr.plan_role_changes(desired, members, FULL_MAP)
        assert sorted(plan.adds) == [("1", "registered"), ("2", "unregistered"), ("3", "unregistered")]
        assert plan.removes == [("3", "registered")]

    def test_claiming_swaps_unregistered_for_registered(self):
        members = [_member(1, roles=[FULL_MAP["unregistered"]])]
        plan = dr.plan_role_changes({"1": {"registered"}}, members, FULL_MAP)
        assert plan.adds == [("1", "registered")]
        assert plan.removes == [("1", "unregistered")]

    def test_a_converged_member_needs_nothing(self):
        members = [_member(1, roles=[FULL_MAP["registered"]]), _member(2, roles=[FULL_MAP["unregistered"]])]
        plan = dr.plan_role_changes({"1": {"registered"}}, members, FULL_MAP)
        assert plan.adds == [] and plan.removes == []

    def test_bots_are_never_unregistered(self):
        plan = dr.plan_role_changes({"1": {"registered"}}, [_member(5, bot=True)], FULL_MAP)
        assert plan.adds == []

    def test_an_empty_read_never_hands_the_whole_server_unregistered(self):
        members = [_member(1, roles=[FULL_MAP["unregistered"]]), _member(2)]
        plan = dr.plan_role_changes({}, members, FULL_MAP)
        assert plan.adds == [] and plan.removes == []

    def test_without_the_status_roles_in_the_map_nothing_changes(self):
        plan = dr.plan_role_changes({"1": {"registered"}}, [_member(1), _member(2)], MAP)
        assert plan.adds == [] and plan.removes == []

    def test_registered_players_off_the_server_are_not_counted_as_missing(self):
        plan = dr.plan_role_changes({"1": {"registered"}, "2": {"registered", "fanatic"}},
                                    [_member(3)], FULL_MAP)
        assert plan.not_in_guild == 1

    def test_the_status_roles_are_kept_out_of_the_tier_block(self):
        assert not {"registered", "unregistered"} & {s.key for s in dr.ROLE_SPECS}
        assert dr.SPECS_BY_KEY["registered"].adopt_role_id == "1210978844190711889"

    def test_the_role_map_keeps_the_status_roles(self, tmp_path):
        path = tmp_path / "roles.json"
        dr.write_role_map(FULL_MAP, path)
        assert dr.load_role_map(path) == FULL_MAP


class _Status(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class _FakeHttp:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    async def add_guild_member_role(self, guild_id, user_id, role_id, reason=None):
        self.calls.append(("add", str(user_id), str(role_id)))
        if str(user_id) in self.fail:
            raise _Status(self.fail[str(user_id)])

    async def remove_guild_member_role(self, guild_id, user_id, role_id, reason=None):
        self.calls.append(("remove", str(user_id), str(role_id)))
        if str(user_id) in self.fail:
            raise _Status(self.fail[str(user_id)])

    async def list_members(self, guild_id, limit, after=None):
        self.calls.append(("list", after))
        if after is None:
            return [_member(i) for i in range(1, limit + 1)]
        return [_member(limit + 1)]


class TestApply:
    def test_additions_go_first_and_failures_are_counted_not_raised(self):
        plan = dr.RolePlan(adds=[("1", "patron"), ("2", "sponsor"), ("3", "fanatic")],
                           removes=[("4", "bug_tester")])
        http = _FakeHttp(fail={"2": 404, "3": 403})
        stats = asyncio.run(dr.apply_role_plan(http, plan, MAP, pause=0))
        assert [c[0] for c in http.calls] == ["add", "add", "add", "remove"]
        assert stats == {"added": 1, "removed": 1, "not_member": 1, "forbidden": 1,
                         "errors": 0, "deferred": 0}

    def test_the_op_limit_defers_the_rest(self):
        plan = dr.RolePlan(adds=[(str(i), "patron") for i in range(5)])
        stats = asyncio.run(dr.apply_role_plan(_FakeHttp(), plan, MAP, op_limit=2, pause=0))
        assert stats["added"] == 2 and stats["deferred"] == 3

    def test_members_are_paged_until_a_short_page(self):
        http = _FakeHttp()
        members = asyncio.run(dr.fetch_guild_members(http, "1", page_limit=3, page_pause=0))
        assert len(members) == 4
        assert [c for c in http.calls if c[0] == "list"] == [("list", None), ("list", "3")]


class TestRoleMap:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "roles.json"
        dr.write_role_map({"patron": "1", "bug_tester": "2"}, path=path, guild_id="42")
        assert dr.load_role_map(path=path, guild_id="42") == {"patron": "1", "bug_tester": "2"}

    def test_a_map_for_another_guild_reads_as_empty(self, tmp_path):
        path = tmp_path / "roles.json"
        dr.write_role_map({"patron": "1"}, path=path, guild_id="42")
        assert dr.load_role_map(path=path, guild_id="43") == {}

    def test_unknown_keys_and_junk_ids_are_ignored(self, tmp_path):
        path = tmp_path / "roles.json"
        path.write_text(json.dumps({"guild_id": "42", "roles": {"patron": "abc", "nope": "5", "fanatic": "7"}}))
        assert dr.load_role_map(path=path, guild_id="42") == {"fanatic": "7"}

    def test_a_missing_map_reads_as_empty(self, tmp_path):
        assert dr.load_role_map(path=tmp_path / "absent.json", guild_id="42") == {}


def _role(rid, name, position, **extra):
    return {"id": str(rid), "name": name, "position": position, **extra}


class TestSetup:
    def test_ids_win_over_names_so_the_old_supporter_role_becomes_fanatic(self):
        specs = dr.SPECS_BY_KEY
        roles = [
            _role(specs["supporter"].adopt_role_id, "Supporter [T1]", 17),
            _role(specs["fanatic"].adopt_role_id, "Supporter", 28),
        ]
        resolved = dr.resolve_spec_roles(roles, {})
        assert resolved["supporter"]["name"] == "Supporter [T1]"
        assert resolved["fanatic"]["name"] == "Supporter"

    def test_the_new_supporter_role_never_takes_the_old_one_by_name(self):
        roles = [_role(dr.SPECS_BY_KEY["fanatic"].adopt_role_id, "Supporter", 28)]
        resolved = dr.resolve_spec_roles(roles, {})
        assert resolved["fanatic"] is not None
        assert resolved["supporter"] is None

    def test_an_integration_role_is_never_adopted_by_name(self):
        roles = [_role(77, "Patron (Member)", 5, managed=True)]
        assert dr.resolve_spec_roles(roles, {})["patron_member"] is None

    def test_a_recorded_id_wins(self):
        roles = [_role(77, "Renamed by staff", 5), _role(78, "Patron (Member)", 4)]
        assert dr.resolve_spec_roles(roles, {"patron_member": "77"})["patron_member"]["id"] == "77"

    def test_a_converged_role_needs_no_patch(self):
        spec = dr.SPECS_BY_KEY["patron"]
        role = _role(1, "Patron", 5, hoist=True, mentionable=False, icon="abc",
                     colors={"primary_color": spec.primary_color,
                             "secondary_color": spec.secondary_color, "tertiary_color": None})
        assert dr.role_patch(spec, role) == {}

    def test_an_existing_image_icon_is_kept(self):
        spec = dr.SPECS_BY_KEY["sponsor"]
        role = _role(1, "Supporter [T2]", 5, hoist=True, icon="abc", colors={"primary_color": 0x3498DB})
        patch = dr.role_patch(spec, role)
        assert patch["name"] == "Sponsor"
        assert patch["colors"] == {"primary_color": 0xFFD966, "secondary_color": 0xE6A817, "tertiary_color": None}
        assert "icon" not in patch and "unicode_emoji" not in patch

    def test_without_the_gradient_feature_the_flat_colour_is_converged(self):
        # The main server lacks ENHANCED_ROLE_COLORS (Discord: 403 "Missing guild
        # feature"), so a flat role must not keep being "fixed" towards a gradient.
        spec = dr.SPECS_BY_KEY["patron"]
        flat = _role(1, "Patron", 5, hoist=True,
                     colors={"primary_color": spec.primary_color, "secondary_color": None, "tertiary_color": None})
        assert dr.role_patch(spec, flat, allow_gradient=False) == {}
        assert dr.role_patch(spec, flat)["colors"]["secondary_color"] == spec.secondary_color

    def test_bug_tester_gets_its_emoji(self):
        spec = dr.SPECS_BY_KEY["bug_tester"]
        patch = dr.role_patch(spec, _role(1, "Bug Tester", 6, hoist=True, colors={"primary_color": 0x2ECC71}))
        assert patch == {"unicode_emoji": "\U0001F41B", "icon": None}

    def test_a_member_role_copies_its_tiers_icon_only_when_it_has_none(self):
        spec = dr.SPECS_BY_KEY["patron_member"]
        emoji_source = _role(1, "Patron", 9, unicode_emoji="\U0001F379")
        image_source = _role(1, "Patron", 9, icon="hash")
        bare = _role(2, "Patron (Member)", 3)
        assert dr.icon_to_copy(spec, bare, emoji_source) == "emoji"
        assert dr.icon_to_copy(spec, None, emoji_source) == "emoji"      # not created yet
        assert dr.icon_to_copy(spec, bare, image_source) == "image"
        assert dr.icon_to_copy(spec, _role(2, "Patron (Member)", 3, unicode_emoji="x"), emoji_source) is None
        assert dr.icon_to_copy(spec, bare, _role(1, "Patron", 9)) is None
        assert dr.icon_to_copy(dr.SPECS_BY_KEY["patron"], bare, emoji_source) is None

    def _layout(self):
        # position 0 is @everyone (id = guild id); the bot's role sits at 10.
        return [
            _role(900, "Other bot", 11), _role(800, "Our bot", 10), _role(700, "Staff", 8),
            _role(600, "divider", 7), _role(500, "Old", 6), _role(400, "Registered", 5),
            _role(300, "T3", 4), _role(200, "Bug Tester", 3), _role(100, "New", 1),
            _role(1, "@everyone", 0),
        ]

    @staticmethod
    def _apply(roles, changes):
        by_id = {r["id"]: dict(r) for r in roles}
        for change in changes:
            by_id[change["id"]]["position"] = change["position"]
        return [r["id"] for r in sorted(by_id.values(), key=lambda r: (-r["position"], int(r["id"])))]

    def test_the_block_lands_beneath_the_divider_in_order(self):
        roles = self._layout()
        changes = dr.plan_role_order(roles, ["300", "200", "100"], "600", bot_top_position=10, guild_id="1")
        assert self._apply(roles, changes) == ["900", "800", "700", "600", "300", "200", "100", "500", "400", "1"]
        # the whole list, numbered 1..n, and never @everyone
        assert sorted(c["position"] for c in changes) == list(range(1, len(roles)))
        assert "1" not in {c["id"] for c in changes}

    def test_more_roles_below_the_bot_than_numbers_under_it(self):
        # The live server: a gap and three roles created tied at position 1 leave
        # more roles below the bot than there are numbers beneath its role, so
        # the plan has to renumber the roles above the bot too, in their order.
        roles = [_role(900, "Patreon", 6), _role(800, "Our bot", 5), _role(700, "Webhook bot", 4),
                 _role(600, "divider", 2), _role(500, "Old", 1), _role(170, "New A", 1),
                 _role(160, "New B", 1), _role(1, "@everyone", 0)]
        changes = dr.plan_role_order(roles, ["170", "160"], "600", bot_top_position=5, guild_id="1")
        order = self._apply(roles, changes)
        assert order == ["900", "800", "700", "600", "170", "160", "500", "1"]
        top_three = {c["id"]: c["position"] for c in changes if c["id"] in ("900", "800", "700")}
        assert top_three["900"] > top_three["800"] > top_three["700"]

    def test_an_order_already_in_place_is_a_no_op(self):
        roles = [_role(800, "Our bot", 10), _role(600, "divider", 7), _role(300, "T3", 6),
                 _role(200, "Bug Tester", 3), _role(400, "Registered", 2), _role(1, "@everyone", 0)]
        assert dr.plan_role_order(roles, ["300", "200"], "600", bot_top_position=10, guild_id="1") == []

    def test_it_refuses_to_plan_what_it_cannot_do(self):
        roles = self._layout()
        # the anchor is above the bot
        assert dr.plan_role_order(roles, ["300"], "900", bot_top_position=10, guild_id="1") is None
        # a block role the bot cannot move
        assert dr.plan_role_order(roles, ["800"], "600", bot_top_position=10, guild_id="1") is None

    def test_roles_discord_just_created_at_position_one_are_placed_too(self):
        # Discord creates every new role at position 1, tied with what is there.
        roles = [_role(800, "Our bot", 10), _role(600, "divider", 7), _role(400, "Registered", 2),
                 _role(150, "Moderator", 1), _role(170, "New A", 1), _role(160, "New B", 1),
                 _role(1, "@everyone", 0)]
        # ties read as lower id higher: Moderator (150) ranks above both new roles
        assert dr.current_role_order(roles, guild_id="1") == ["800", "600", "400", "150", "160", "170"]
        changes = dr.plan_role_order(roles, ["170", "160"], "600", bot_top_position=10, guild_id="1")
        assert self._apply(roles, changes) == ["800", "600", "170", "160", "400", "150", "1"]
        # once applied, the same plan is a no-op
        settled = [dict(r, position={c["id"]: c["position"] for c in changes}.get(r["id"], r["position"]))
                   for r in roles]
        assert dr.plan_role_order(settled, ["170", "160"], "600", bot_top_position=10, guild_id="1") == []

    def test_ties_outside_the_block_are_left_alone(self):
        roles = [_role(800, "Our bot", 10), _role(600, "divider", 7), _role(300, "T3", 6),
                 _role(400, "Registered", 1), _role(410, "Other", 1), _role(1, "@everyone", 0)]
        assert dr.plan_role_order(roles, ["300"], "600", bot_top_position=10, guild_id="1") == []


class TestBugTesterAccount:
    def test_the_badge_goes_on_the_highest_level_account(self):
        players = [SimpleNamespace(player_id=5, total_level=1200), SimpleNamespace(player_id=9, total_level=2277),
                   SimpleNamespace(player_id=7, total_level=2277)]
        assert dr.pick_badge_player(players).player_id == 7

    def test_an_unknown_total_level_counts_as_zero(self):
        players = [SimpleNamespace(player_id=5, total_level=None), SimpleNamespace(player_id=3, total_level=None)]
        assert dr.pick_badge_player(players).player_id == 3
        assert dr.pick_badge_player([]) is None

    def test_granting_without_an_actor_is_refused(self):
        with pytest.raises(ValueError):
            dr.grant_bug_tester(object(), "123", None)


class TestSyncMode:
    @pytest.mark.parametrize("raw,mode", [("", "on"), ("on", "on"), ("off", "off"), ("false", "off"),
                                          ("dry-run", "dry-run"), ("DRY_RUN", "dry-run")])
    def test_values(self, monkeypatch, raw, mode):
        monkeypatch.setenv("DISCORD_ROLE_SYNC", raw)
        assert dr.sync_mode() == mode


class TestTheCommandIsLockedDown:
    """Read with ``ast``: the conftest stubs ``interactions``, so the real
    decorators never run under pytest and can't be inspected at runtime."""

    source = (_ROOT / "commands" / "bug_tester.py").read_text()
    tree = ast.parse(source)

    def _commands(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for deco in node.decorator_list:
                    if isinstance(deco, ast.Call) and getattr(deco.func, "id", None) == "slash_command":
                        yield node, deco

    def test_every_subcommand_is_scoped_to_the_main_server_and_admin_hidden(self):
        commands = list(self._commands())
        assert {n.name for n, _ in commands} == {"bug_tester_add", "bug_tester_remove", "bug_tester_list"}
        for node, deco in commands:
            keywords = {k.arg: k.value for k in deco.keywords}
            assert ast.unparse(keywords["scopes"]) == "[MAIN_GUILD_ID]", node.name
            assert ast.unparse(keywords["default_member_permissions"]) == "Permissions.ADMINISTRATOR", node.name

    def test_the_owner_check_comes_before_anything_else(self):
        for node, _ in self._commands():
            first = node.body[0]
            assert isinstance(first, ast.If), node.name
            assert ast.unparse(first.test) == "not await self._allowed(ctx)", node.name
            assert isinstance(first.body[0], ast.Return), node.name

    def test_allowed_means_main_server_and_bot_owner(self):
        allowed = next(n for n in ast.walk(self.tree)
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == "_allowed")
        text = ast.unparse(allowed)
        assert "discord_roles.MAIN_GUILD_ID" in text
        assert "is_owner()(ctx)" in text

    def test_the_extension_is_registered(self):
        init = (_ROOT / "commands" / "__init__.py").read_text()
        assert "from .bug_tester import BugTesterCommands" in init
