"""The member-facing points commands: /group-points, /my-points, /lookup.

Two halves, both exercised for real:

* ``services/points_cards.py`` is pure -- the board page, the player card and
  the ids the paging buttons carry -- so it is asserted on directly.
* ``commands/points.py``'s loaders are driven against the same SQLite fixture
  as ``test_point_standings``: what a command says has to follow from the real
  standings SQL (leavers gone, RSNs combined when the clan asks), and the
  privacy rules are decided in those loaders, not in Discord.

Both modules are loaded from their file paths so the conftest stubs for
``services`` / ``interactions`` don't shadow them (same as test_recap_buttons).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.orm import declarative_base

from db.point_standings import MemberTotal, build_standings
from utils.rsn import normalize_player_display_equivalence

from tests.unit.test_point_standings import (  # noqa: F401  (fixtures)
    GROUP,
    OTHER_GROUP,
    _award,
    _join,
    _leave,
    _player,
    _set_combine,
    _user,
    clan,
    session,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _name in ("interactions.api", "interactions.api.events"):
    sys.modules.setdefault(_name, MagicMock())


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cards = _load("_points_cards_ut", ("services", "points_cards.py"))
# The command module does `from services import points_cards`; under the stubbed
# package that attribute lookup would hand back a mock, so pin the real one.
sys.modules["services"].points_cards = cards
sys.modules["services.points_cards"] = cards
commands_points = _load("_commands_points_ut", ("commands", "points.py"))

GUILD = "900000000000000001"
VIEWER = "111"
STRANGER = "222"


def _row(pid, name, points, user_id=None, hidden=False):
    return MemberTotal(player_id=pid, name=name, user_id=user_id, hidden=hidden, points=points)


def _board(n=25, **kw):
    return build_standings([_row(i, f"P{i}", 1000 - i) for i in range(1, n + 1)], combine=False, **kw)


# ── custom ids ───────────────────────────────────────────────────────────────

class TestCustomIds:
    def test_nav_round_trip(self):
        cid = cards.nav_custom_id("next", 190, 3, "202609")
        assert cards.parse_nav_custom_id(cid) == (190, 3, "202609")

    def test_season_period_survives_its_own_colon(self):
        cid = cards.nav_custom_id("prev", 190, 2, "season:12")
        assert cards.parse_nav_custom_id(cid) == (190, 2, "season:12")
        assert cards.parse_me_custom_id(cards.me_custom_id(190, "season:12")) == (190, "season:12")

    def test_prev_and_next_never_collide_on_a_one_page_board(self):
        # Discord rejects a message whose components share a custom id.
        view = cards.board_view(group_id=1, group_name="C", standings=_board(3), period="all")
        assert view["prev_id"] != view["next_id"]
        assert not view["can_prev"] and not view["can_next"]

    def test_ids_fit_discords_100_character_limit(self):
        assert len(cards.nav_custom_id("next", 2**31, 99999, "season:2147483647")) <= 100

    @pytest.mark.parametrize("bad", ["", "gpts:", "gpts:next:x:1:all", "gpts:next:1:all", "other:1"])
    def test_malformed_ids_parse_to_none(self, bad):
        assert cards.parse_nav_custom_id(bad) is None

    def test_period_select_round_trip(self):
        assert cards.parse_period_select_id(cards.period_select_id(190)) == 190
        assert cards.parse_period_select_id("gpts_period:nope") is None


# ── period wording ───────────────────────────────────────────────────────────

class TestPeriodLabel:
    SEASONS = [{"id": 12, "name": "Autumn Cup"}]

    @pytest.mark.parametrize("token,expected", [
        ("all", "All-time"),
        ("202609", "September 2026"),
        ("20260918", "18 September 2026"),
        ("2026W38", "Week 38, 2026"),
        ("season:12", "Autumn Cup"),
        ("season:99", "Season 99"),
    ])
    def test_labels(self, token, expected):
        assert cards.period_label(token, self.SEASONS) == expected

    def test_picker_marks_the_preset_that_resolves_to_the_shown_token(self):
        now = datetime(2026, 9, 18, 12, 0)
        options = cards.period_options(self.SEASONS, "202609", now=now)
        assert [o["value"] for o in options] == ["all", "month", "week", "day", "season:12"]
        assert [o["value"] for o in options if o["default"]] == ["month"]

    def test_picker_marks_a_season(self):
        options = cards.period_options(self.SEASONS, "season:12")
        assert [o["value"] for o in options if o["default"]] == ["season:12"]

    def test_picker_never_exceeds_discords_25_options(self):
        seasons = [{"id": i, "name": f"S{i}"} for i in range(60)]
        assert len(cards.period_options(seasons, "all")) == 25


# ── the board page ───────────────────────────────────────────────────────────

class TestBoardView:
    def test_first_page(self):
        view = cards.board_view(group_id=190, group_name="Clan", standings=_board(), period="all")
        assert view["title"] == "Clan — Points"
        assert "**All-time** · 25 ranked" in view["description"]
        assert "**1.** `P1` — **999**" in view["description"]
        assert "**10.** `P10`" in view["description"] and "**11.**" not in view["description"]
        assert (view["page"], view["pages"]) == (1, 3)
        assert view["can_next"] and not view["can_prev"]
        assert view["url"].endswith("/groups/190/points/leaderboard?period=all")

    def test_next_button_names_the_page_it_leads_to(self):
        view = cards.board_view(group_id=190, group_name="C", standings=_board(), period="202609", page=2)
        assert cards.parse_nav_custom_id(view["next_id"]) == (190, 3, "202609")
        assert cards.parse_nav_custom_id(view["prev_id"]) == (190, 1, "202609")

    def test_stale_page_lands_on_the_last_one(self):
        view = cards.board_view(group_id=1, group_name="C", standings=_board(), period="all", page=40)
        assert view["page"] == 3 and "**25.** `P25`" in view["description"]

    def test_viewer_is_marked_and_quoted_in_the_footer(self):
        view = cards.board_view(group_id=1, group_name="C", standings=_board(), period="all",
                                viewer_player_ids=[14])
        assert "You: #14 · 986 pts" in view["footer"]
        assert view["viewer_page"] == 2 and view["can_me"]
        page2 = cards.board_view(group_id=1, group_name="C", standings=_board(), period="all",
                                 page=2, viewer_player_ids=[14])
        assert "`P14` — **986** ◀ you" in page2["description"]
        assert not page2["can_me"]  # already looking at it

    def test_unranked_viewer(self):
        view = cards.board_view(group_id=1, group_name="C", standings=_board(), period="all",
                                viewer_player_ids=[999])
        assert "not ranked" in view["footer"] and not view["can_me"]

    def test_viewer_with_no_claimed_account_is_pointed_at_claim_rsn(self):
        view = cards.board_view(group_id=1, group_name="C", standings=_board(), period="all")
        assert "/claim-rsn" in view["footer"]

    def test_combined_rows_name_their_accounts(self):
        board = build_standings(
            [_row(1, "Main", 50, user_id=9), _row(2, "Alt", 80, user_id=9)], combine=True)
        view = cards.board_view(group_id=1, group_name="C", standings=board, period="all",
                                combined=True)
        assert "**1.** `Alt` + `Main` — **130**" in view["description"]
        assert "counted together" in view["description"]

    def test_hidden_player_is_not_listed_but_can_see_their_own_rank(self):
        board = build_standings([_row(1, "Ghost", 90, hidden=True), _row(2, "Seen", 10)], combine=False)
        view = cards.board_view(group_id=1, group_name="C", standings=board, period="all",
                                viewer_player_ids=[1])
        assert "Ghost" not in view["description"]
        assert "**2.** `Seen`" in view["description"]
        assert "You: #1 · 90 pts" in view["footer"]
        assert not view["can_me"]  # no page holds a hidden row

    def test_empty_board(self):
        view = cards.board_view(group_id=1, group_name="C", standings=[], period="202609")
        assert "No points have been earned" in view["description"]
        assert (view["page"], view["pages"]) == (1, 1)

    def test_description_stays_inside_discords_embed_limit(self):
        board = build_standings(
            [_row(i, "W" * 12, 10**9 - i, user_id=i // 6) for i in range(1, 61)], combine=True)
        view = cards.board_view(group_id=1, group_name="C", standings=board, period="all", combined=True)
        assert len(view["description"]) < 4096


# ── the player card ──────────────────────────────────────────────────────────

class TestPlayerCard:
    def _card(self, *, combine, member_ids=(1, 2), recent=()):
        rows = [_row(1, "Main", 50, user_id=9), _row(2, "Alt", 80, user_id=9), _row(3, "Solo", 100)]
        rows = [r for r in rows if r.player_id in set(member_ids) | {3}]
        board = build_standings(rows, combine=combine)
        return {"combined": combine, "member_ids": set(member_ids), "all_time": board,
                "month": board, "month_token": "202609", "recent": list(recent)}

    ACCOUNTS = [{"player_id": 1, "name": "Main"}, {"player_id": 2, "name": "Alt"}]

    def test_per_rsn_card_ranks_each_account(self):
        spec = cards.player_card(accounts=self.ACCOUNTS, is_self=True, group_id=190,
                                 group_name="Clan", card=self._card(combine=False))
        assert spec["title"] == "Your points"
        assert "`Alt` — All-time: **#2** of 3 — **80** pts" in spec["description"]
        assert "`Main` — All-time: **#3** of 3 — **50** pts" in spec["description"]

    def test_combined_card_is_one_standing_with_a_breakdown(self):
        spec = cards.player_card(accounts=self.ACCOUNTS, is_self=True, group_id=190,
                                 group_name="Clan", card=self._card(combine=True))
        assert "All-time: **#1** of 2 — **130** pts" in spec["description"]
        assert "Combined across 2 accounts" in spec["description"]
        breakdown = dict((name, value) for name, value, _ in spec["fields"])["Accounts"]
        assert breakdown.splitlines() == ["`Alt` — 80 pts", "`Main` — 50 pts"]

    def test_account_outside_the_clan_is_called_out(self):
        spec = cards.player_card(accounts=self.ACCOUNTS, is_self=True, group_id=190,
                                 group_name="Clan", card=self._card(combine=True, member_ids=(1,)))
        fields = dict((name, value) for name, value, _ in spec["fields"])
        assert "`Alt`" in fields["Not in this clan"]
        assert "All-time: **#2** of 2 — **50** pts" in spec["description"]

    def test_nobody_in_the_clan(self):
        spec = cards.player_card(accounts=self.ACCOUNTS, is_self=False, group_id=190,
                                 group_name="Clan", card=self._card(combine=False, member_ids=()))
        assert "hold no place" in spec["description"]

    def test_lookup_title_names_the_player(self):
        spec = cards.player_card(accounts=[self.ACCOUNTS[0]], is_self=False, group_id=190,
                                 group_name="Clan", card=self._card(combine=False))
        assert spec["title"] == "Main's points"
        assert spec["url"].endswith("/players/1")

    def test_recent_awards_and_other_clans(self):
        recent = [{"player_id": 1, "amount": 5, "reason": "drop", "date_added": datetime(2026, 9, 1)},
                  {"player_id": 2, "amount": -3, "reason": "[Admin]  fix", "date_added": None}]
        spec = cards.player_card(accounts=self.ACCOUNTS, is_self=True, group_id=190,
                                 group_name="Clan", card=self._card(combine=True, recent=recent),
                                 other_groups=[("Other Clan", 1200)])
        fields = dict((name, value) for name, value, _ in spec["fields"])
        first, second = fields["Recent awards"].splitlines()
        assert first.startswith("**+5** · drop · `Main` · <t:")
        assert second == "**-3** · [Admin] fix · `Alt`"
        assert fields["Your other clans"] == "`Other Clan` — **1,200** pts"

    def test_outside_any_clan_server(self):
        spec = cards.player_card(accounts=self.ACCOUNTS, is_self=True, group_id=None,
                                 other_groups=[("Clan", 130)])
        assert "inside your clan's Discord server" in spec["description"]

    def test_clan_without_the_points_system(self):
        card = {"combined": False, "member_ids": {1, 2}, "all_time": [], "month": [], "recent": []}
        spec = cards.player_card(accounts=self.ACCOUNTS, is_self=True, group_id=190,
                                 group_name="Clan", card=card, points_active=False)
        assert "doesn't use DropTracker clan points" in spec["description"]

    def test_stats_line(self):
        accounts = [{"player_id": 1, "name": "Main", "month_loot": 12_300_000,
                     "total_level": 2277, "log_slots": 1200}]
        spec = cards.player_card(accounts=accounts, is_self=True, group_id=None)
        fields = dict((name, value) for name, value, _ in spec["fields"])
        assert "12.3M loot this month · total level 2,277 · 1,200 log slots" in fields["On DropTracker"]


# ── the command loaders, against the real standings SQL ──────────────────────

@pytest.fixture
def discord_env(clan, monkeypatch):
    """The clan fixture, plus what the command loaders read around it: the
    guild link, Discord ids on users, and the columns a player card prints."""
    s = clan
    for ddl in (
        "CREATE TABLE guilds (guild_id VARCHAR(255) PRIMARY KEY, group_id INTEGER)",
        "CREATE TABLE `groups` (group_id INTEGER PRIMARY KEY, group_name VARCHAR(30))",
        "ALTER TABLE users ADD COLUMN discord_id VARCHAR(35)",
        "ALTER TABLE players ADD COLUMN total_level INTEGER",
        "ALTER TABLE players ADD COLUMN log_slots INTEGER",
        "ALTER TABLE players ADD COLUMN player_name_norm VARCHAR(64)",
    ):
        s.execute(text(ddl))
    s.execute(text("INSERT INTO guilds VALUES (:g, :gid)"), {"g": GUILD, "gid": GROUP})
    s.execute(text("INSERT INTO `groups` VALUES (:gid, 'Test Clan'), (:other, 'Other Clan')"),
              {"gid": GROUP, "other": OTHER_GROUP})
    s.execute(text("UPDATE users SET discord_id = :d WHERE user_id = 9"), {"d": VIEWER})
    for pid, name in s.execute(text("SELECT player_id, player_name FROM players")).fetchall():
        s.execute(text("UPDATE players SET player_name_norm = :n WHERE player_id = :p"),
                  {"n": normalize_player_display_equivalence(name), "p": pid})

    Base = declarative_base()

    class Player(Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        player_name = Column(String(64, collation="NOCASE"))
        player_name_norm = Column(String(64))

    @contextmanager
    def _cm():
        yield s

    monkeypatch.setattr(sys.modules["db.models.base"], "db_session", _cm, raising=False)
    monkeypatch.setattr(commands_points, "_player_model", lambda: Player)
    monkeypatch.setattr(commands_points, "_add_month_loot", lambda accounts: accounts)
    monkeypatch.setattr(commands_points, "_points_active", lambda gid: True)
    return s


class TestLoadBoard:
    def test_board_for_the_servers_clan(self, discord_env):
        view = commands_points._load_board(GUILD, VIEWER, "all", 1)
        assert view["title"] == "Test Clan — Points" and view["group_id"] == GROUP
        assert "**1.** `Solo` — **100**" in view["description"]
        # Per-RSN board, viewer owns Alt (#2) and Main (#3): the best is quoted.
        assert "You: #2 · 80 pts" in view["footer"]

    def test_leaver_is_gone_from_the_discord_board_too(self, discord_env):
        _leave(discord_env, 3)
        view = commands_points._load_board(GUILD, VIEWER, "all", 1)
        assert "Solo" not in view["description"]
        assert "**1.** `Alt` — **80**" in view["description"]

    def test_combined_board(self, discord_env):
        _set_combine(discord_env)
        view = commands_points._load_board(GUILD, VIEWER, "all", 1)
        assert "**1.** `Alt` + `Main` — **130** ◀ you" in view["description"]
        assert "You: #1 · 130 pts" in view["footer"]

    def test_unlinked_server(self, discord_env):
        assert "not linked" in commands_points._load_board("12345", VIEWER, "all", 1)["error"]

    def test_deleted_season(self, discord_env):
        assert "no longer exists" in commands_points._load_board(GUILD, VIEWER, "season:77", 1)["error"]

    def test_season_window(self, discord_env):
        discord_env.execute(text(
            "INSERT INTO group_point_seasons (id, group_id, name, start_at, end_at) "
            "VALUES (5, :g, 'Autumn Cup', '2026-09-01 00:00:00', '2026-10-01 00:00:00')"), {"g": GROUP})
        _award(discord_env, 3, 900, when="2026-01-01 00:00:00")
        view = commands_points._load_board(GUILD, VIEWER, "season:5", 1)
        assert "**Autumn Cup**" in view["description"]
        assert "`Solo` — **100**" in view["description"]  # the January award is outside it

    def test_jump_to_viewer(self, discord_env):
        for pid in range(100, 115):
            _player(discord_env, pid, f"Filler{pid}")
            _join(discord_env, pid)
            _award(discord_env, pid, 500)
        view = commands_points._load_board(GUILD, VIEWER, "all", 1, jump_to_viewer=True)
        assert view["page"] == 2 and "◀ you" in view["description"]


class TestLoadCard:
    def test_self_card(self, discord_env):
        _join(discord_env, 1, group_id=OTHER_GROUP)
        _award(discord_env, 1, 40, group_id=OTHER_GROUP)
        spec = commands_points._load_card(GUILD, VIEWER)
        assert spec["title"] == "Your points"
        fields = dict((name, value) for name, value, _ in spec["fields"])
        assert fields["Your other clans"] == "`Other Clan` — **40** pts"

    def test_self_with_no_claimed_account(self, discord_env):
        assert "/claim-rsn" in commands_points._load_card(GUILD, STRANGER)["error"]

    def test_rsn_lookup_folds_separators_and_never_names_the_discord_user(self, discord_env):
        _player(discord_env, 50, "Tzuk Kal Lag")
        discord_env.execute(text(
            "UPDATE players SET player_name_norm = 'tzuk kal lag' WHERE player_id = 50"))
        _join(discord_env, 50)
        _award(discord_env, 50, 7)
        spec = commands_points._load_card(GUILD, STRANGER, rsn="Tzuk-Kal-Lag")
        assert spec["title"] == "Tzuk Kal Lag's points"
        assert VIEWER not in str(spec)

    def test_rsn_lookup_of_your_own_account_is_a_self_view(self, discord_env):
        assert commands_points._load_card(GUILD, VIEWER, rsn="main")["title"] == "Your points"

    def test_unknown_rsn(self, discord_env):
        assert "No player named" in commands_points._load_card(GUILD, VIEWER, rsn="nobody")["error"]

    def test_member_lookup_lists_only_their_accounts_in_this_clan(self, discord_env):
        _leave(discord_env, 2)  # Alt plays elsewhere now
        spec = commands_points._load_card(GUILD, STRANGER, target_discord_id=VIEWER)
        assert spec["title"] == "Main's points"
        assert "Alt" not in str(spec)

    def test_member_lookup_with_nothing_in_this_clan(self, discord_env):
        _leave(discord_env, 1)
        _leave(discord_env, 2)
        assert "no accounts in" in commands_points._load_card(
            GUILD, STRANGER, target_discord_id=VIEWER)["error"]

    def test_member_lookup_needs_a_clan_server(self, discord_env):
        assert "only works inside" in commands_points._load_card(
            None, STRANGER, target_discord_id=VIEWER)["error"]

    def test_hidden_account_is_shown_to_its_owner_only(self, discord_env):
        discord_env.execute(text("UPDATE players SET hidden = 1 WHERE player_id IN (1, 2)"))
        assert "chosen to hide" in commands_points._load_card(GUILD, STRANGER, rsn="Main")["error"]
        assert "chosen to hide" in commands_points._load_card(
            GUILD, STRANGER, target_discord_id=VIEWER)["error"]
        assert commands_points._load_card(GUILD, VIEWER)["title"] == "Your points"

    def test_looking_yourself_up_as_a_member_is_the_self_view(self, discord_env):
        assert commands_points._load_card(
            GUILD, VIEWER, target_discord_id=VIEWER)["title"] == "Your points"


class TestAutocomplete:
    def test_suggests_this_clans_members(self, discord_env):
        _player(discord_env, 60, "Mainframe")  # not in the clan
        assert commands_points._suggest_names(GUILD, "mai") == ["Main"]

    def test_no_text_lists_the_roster(self, discord_env):
        assert commands_points._suggest_names(GUILD, "") == ["Alt", "Main", "Solo"]

    def test_hidden_players_are_not_suggested(self, discord_env):
        discord_env.execute(text("UPDATE players SET hidden = 1 WHERE player_id = 3"))
        assert commands_points._suggest_names(GUILD, "") == ["Alt", "Main"]

    def test_percent_is_literal(self, discord_env):
        assert commands_points._suggest_names(GUILD, "%") == []

    def test_outside_a_clan_server_it_is_a_prefix_search(self, discord_env):
        _player(discord_env, 60, "Mainframe")
        assert commands_points._suggest_names(None, "mai") == ["Main", "Mainframe"]
        assert commands_points._suggest_names(None, "") == []
