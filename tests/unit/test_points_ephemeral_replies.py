"""Private or public replies for the Discord surfaces that change points.

``points_ephemeral_messages`` is a points behavior toggle. Off -- the default,
and what every group has until an admin ticks it -- the confirmation of
``/add-group-points`` / ``/remove-group-points`` is posted for the channel, and
each change made through "Modify Entry" is announced there with the admin who
made it. On, all of it stays with the admin who acted, as it always used to.

What is pinned here:

* the settings route and the bot agree on the key, the default and the parse
  (the ``points_combine_accounts`` lesson: a looser read on one side means the
  toggle shows one thing while the bot does the other);
* the notes "Modify Entry" posts -- what they say, that a hidden drop's note
  repeats none of what hiding blanked out, and how posting degrades;
* the add/remove commands: only the success reply follows the setting;
  refusals and errors stay private.

``services/entry_modifier.py`` is loaded from its file path, because the
conftest stubs the ``services`` package (same as test_points_commands).
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import web_api.routes.points as pts
from utils import group_config as gc

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _name in ("interactions.api", "interactions.api.events"):
    sys.modules.setdefault(_name, MagicMock())

_spec = importlib.util.spec_from_file_location(
    "_entry_modifier_ut", os.path.join(_ROOT, "services", "entry_modifier.py"),
)
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)

GROUP = 42
KEY = "points_ephemeral_messages"


def _stored(monkeypatch, value):
    """Make ``group_configurations`` hold ``value`` for the toggle (None = no row)."""

    def _get(session, group_id, key, default=None):
        assert key == KEY
        return value if value is not None else default

    monkeypatch.setattr(gc, "get", _get)


# ── the setting ──────────────────────────────────────────────────────────────

class TestSetting:
    def test_route_and_bot_name_the_same_key_and_default_to_public(self):
        assert gc.POINTS_EPHEMERAL_MESSAGES == KEY
        assert KEY in pts.BEHAVIOR_BOOL_KEYS
        assert pts.BEHAVIOR_DEFAULTS[KEY] is False

    def test_a_group_that_never_set_it_replies_publicly(self, monkeypatch):
        _stored(monkeypatch, None)
        assert gc.points_replies_ephemeral(None, GROUP) is False

    @pytest.mark.parametrize("raw, private", [
        ("1", True), (" 1 ", True), ("0", False), ("", False), ("true", False),
    ])
    def test_only_a_stored_1_is_private(self, monkeypatch, raw, private):
        _stored(monkeypatch, raw)
        assert gc.points_replies_ephemeral(None, GROUP) is private

    @pytest.mark.parametrize("raw", ["1", " 1 ", "0", "", "true", None])
    def test_the_bot_reads_what_the_settings_page_shows(self, monkeypatch, raw):
        # Same row value through both parsers: the toggle an admin sees and
        # the reply the bot sends can never disagree.
        _stored(monkeypatch, raw)
        row = SimpleNamespace(config_value=raw) if raw is not None else None
        monkeypatch.setattr(pts, "_config_row", lambda s, gid, key: row if key == KEY else None)
        assert pts._read_behavior(None, GROUP)[KEY] is gc.points_replies_ephemeral(None, GROUP)

    def test_an_unreadable_setting_keeps_the_reply_private(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(gc, "get", _boom)
        assert gc.points_replies_ephemeral(None, GROUP) is True


# ── the settings route ───────────────────────────────────────────────────────

@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire_settings(monkeypatch, store: dict):
    @contextmanager
    def _cm():
        yield SimpleNamespace(commit=lambda: None)

    def _row(s, gid, key):
        return SimpleNamespace(config_value=store[key]) if key in store else None

    def _write(s, gid, key, value):
        store[key] = value

    monkeypatch.setattr(pts, "current_user_id", lambda: 7)
    monkeypatch.setattr(pts, "db_session", _cm)
    monkeypatch.setattr(pts, "_admin_ctx", lambda s, uid, gid, entitlement=False: SimpleNamespace(user_id=uid))
    monkeypatch.setattr(
        pts, "_assert_group_exists",
        lambda s, gid: SimpleNamespace(group_id=gid, group_name="Test Clan"),
    )
    monkeypatch.setattr(pts, "_config_row", _row)
    monkeypatch.setattr(pts, "_write_config", _write)
    monkeypatch.setattr(pts, "_audit", lambda s, **kw: None)
    monkeypatch.setattr(pts, "_rules_payload", lambda s, gid: [])


class TestSettingsRoute:
    async def test_the_toggle_saves_as_1_and_reads_back_on(self, client, monkeypatch):
        store: dict = {}
        _wire_settings(monkeypatch, store)
        resp = await client.put(
            f"/api/v1/groups/{GROUP}/points/settings", json={"behavior": {KEY: True}},
        )
        assert resp.status_code == 200
        assert store[KEY] == "1"
        assert (await resp.get_json())["behavior"][KEY] is True

    async def test_turning_it_off_saves_0(self, client, monkeypatch):
        store = {KEY: "1"}
        _wire_settings(monkeypatch, store)
        resp = await client.put(
            f"/api/v1/groups/{GROUP}/points/settings", json={"behavior": {KEY: False}},
        )
        assert store[KEY] == "0"
        assert (await resp.get_json())["behavior"][KEY] is False

    async def test_saving_other_behavior_leaves_it_alone(self, client, monkeypatch):
        store: dict = {}
        _wire_settings(monkeypatch, store)
        resp = await client.put(
            f"/api/v1/groups/{GROUP}/points/settings",
            json={"behavior": {"point_sharing": True}},
        )
        assert KEY not in store
        assert (await resp.get_json())["behavior"][KEY] is False


# ── "Modify Entry" change notes ──────────────────────────────────────────────

class TestNotes:
    def test_a_deleted_drop_is_named_in_full(self):
        title, text, _ = em._deleted_note("Zezima", "Twisted bow", 1, 1_234_567_890, 12)
        assert title == "Drop deleted"
        assert text.splitlines() == [
            "**Player:** Zezima",
            "**Item:** Twisted bow (x1)",
            "**Value:** 1,234,567,890 GP",
            "**Points:** 12 → 0",
        ]

    def test_a_drop_that_paid_no_points_has_no_points_line(self):
        _, text, _ = em._deleted_note("Zezima", "Bones", 3, 300, 0)
        assert "Points" not in text

    def test_a_hidden_drop_repeats_nothing_the_hide_blanked_out(self):
        title, text, _ = em._hidden_note(12)
        assert title == "Drop hidden"
        for detail in ("Player", "Item", "Value", "GP"):
            assert detail not in text
        assert "**Points:** 12 → 0" in text

    def test_a_restored_drop_shows_the_points_it_earned_again(self):
        title, text, _ = em._restored_note("Zezima", "Twisted bow", 1, 0, 12)
        assert title == "Drop restored"
        assert "**Points:** 0 → 12" in text

    def test_a_value_change_shows_before_and_after(self):
        title, text, _ = em._value_note("Zezima", "Coins", 2, 10_000, 4_000, 5, 2)
        assert title == "Drop value changed"
        assert "**Value:** 10,000 GP → 4,000 GP" in text
        assert "**Points:** 5 → 2" in text

    def test_a_split_change_lists_who_it_is_split_with_now(self):
        _, text, _ = em._split_note("Zezima", "Twisted bow", 1, ["Alt", "Friend"], 10, 10)
        assert "**Split with:** Alt, Friend" in text
        assert "Points" not in text  # an equal split moves no total
        _, text, _ = em._split_note("Zezima", "Twisted bow", 1, [], 10, 10)
        assert "**Split with:** nobody" in text

    def test_names_fall_back_instead_of_raising(self):
        assert em._note_names({"player": None, "item": None}) == ("Unknown", "Unknown")

    def test_no_note_uses_an_em_dash(self):
        notes = [
            em._deleted_note("A", "B", 1, 1, 1),
            em._hidden_note(1),
            em._restored_note("A", "B", 1, 0, 1),
            em._value_note("A", "B", 1, 1, 2, 1, 2),
            em._split_note("A", "B", 1, ["C"], 1, 2),
        ]
        for title, text, _ in notes:
            assert "—" not in title and "—" not in text


class FakeEmbed:
    def __init__(self, title=None, description=None, color=None):
        self.title, self.description, self.color = title, description, color
        self.fields, self.footer = [], None

    def add_field(self, name, value, inline=False):
        self.fields.append((name, value))

    def set_footer(self, text):
        self.footer = text


def _channel(*send_effects):
    channel = SimpleNamespace(send=AsyncMock(side_effect=list(send_effects) or None))
    bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=channel))
    return bot, channel


class TestPosting:
    async def test_replies_to_the_notification(self):
        bot, channel = _channel()
        assert await em._post_change_note(bot, "100", "200", "embed") is True
        channel.send.assert_awaited_once_with(embeds=["embed"], reply_to=200)

    async def test_a_refused_reply_falls_back_to_a_plain_message(self):
        bot, channel = _channel(RuntimeError("Unknown message"), None)
        assert await em._post_change_note(bot, "100", "200", "embed") is True
        assert channel.send.await_args_list[-1].kwargs == {"embeds": ["embed"]}

    async def test_a_deleted_drop_posts_without_replying(self):
        bot, channel = _channel()
        assert await em._post_change_note(bot, "100", None, "embed") is True
        channel.send.assert_awaited_once_with(embeds=["embed"])

    async def test_reports_failure_when_nothing_could_be_posted(self):
        bot, _ = _channel(RuntimeError("Missing Permissions"), RuntimeError("Missing Permissions"))
        assert await em._post_change_note(bot, "100", "200", "embed") is False
        bot = SimpleNamespace(fetch_channel=AsyncMock(side_effect=RuntimeError("Unknown Channel")))
        assert await em._post_change_note(bot, "100", "200", "embed") is False


class TestAnnounce:
    @pytest.fixture(autouse=True)
    def _fake_embed(self, monkeypatch):
        monkeypatch.setattr(em, "Embed", FakeEmbed)

    async def _announce(self, bot, build_note=lambda: em._hidden_note(3)):
        ctx = SimpleNamespace(author_id=555, send=AsyncMock())
        await em._announce_change(
            bot, ctx, None, group_id=GROUP, channel_id="100",
            reply_to_message_id="200", drop_id=9, build_note=build_note,
        )
        return ctx

    async def test_public_by_default_and_says_who_did_it(self, monkeypatch):
        _stored(monkeypatch, None)
        bot, channel = _channel()
        ctx = await self._announce(bot)
        embed = channel.send.await_args.kwargs["embeds"][0]
        assert embed.title == "Drop hidden"
        assert embed.fields == [("Performed by", "<@555>")]
        assert embed.footer == "Drop ID: 9"
        ctx.send.assert_not_awaited()

    async def test_a_private_group_gets_no_channel_post(self, monkeypatch):
        _stored(monkeypatch, "1")
        bot, channel = _channel()
        build = MagicMock(return_value=em._hidden_note(3))
        await self._announce(bot, build)
        channel.send.assert_not_awaited()
        build.assert_not_called()

    async def test_the_admin_hears_when_the_channel_post_failed(self, monkeypatch):
        _stored(monkeypatch, None)
        bot, _ = _channel(RuntimeError("Missing Permissions"), RuntimeError("Missing Permissions"))
        ctx = await self._announce(bot)
        ctx.send.assert_awaited_once()
        assert ctx.send.await_args.kwargs["ephemeral"] is True

    async def test_nothing_to_announce_posts_nothing(self, monkeypatch):
        _stored(monkeypatch, None)
        bot, channel = _channel()
        await self._announce(bot, lambda: None)
        channel.send.assert_not_awaited()

    async def test_a_note_that_fails_to_build_never_raises(self, monkeypatch):
        _stored(monkeypatch, None)
        bot, channel = _channel()

        def _boom():
            raise RuntimeError("db down")

        ctx = await self._announce(bot, _boom)
        channel.send.assert_not_awaited()
        ctx.send.assert_not_awaited()


# ── /add-group-points and /remove-group-points ───────────────────────────────

def _sends(func: ast.AsyncFunctionDef):
    return [
        node for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "send"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx"
    ]


@pytest.mark.parametrize("command", ["add_group_points_cmd", "remove_group_points_cmd"])
def test_only_the_confirmation_follows_the_setting(command):
    tree = ast.parse(open(os.path.join(_ROOT, "commands", "group_admin.py")).read())
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == command
    )
    follows, private = [], []
    for call in _sends(func):
        ephemeral = next((k.value for k in call.keywords if k.arg == "ephemeral"), None)
        assert ephemeral is not None, f"line {call.lineno}: say whether the reply is private"
        if isinstance(ephemeral, ast.Constant):
            assert ephemeral.value is True
            private.append(call)
        else:
            assert isinstance(ephemeral, ast.Call)
            assert ephemeral.func.attr == "points_replies_ephemeral"
            follows.append(call)
    # One confirmation (the embed); every refusal and error stays private.
    assert len(follows) == 1
    assert any(k.arg == "embed" for k in follows[0].keywords)
    assert len(private) >= 5
