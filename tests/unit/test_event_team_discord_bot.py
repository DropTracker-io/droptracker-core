"""Unit tests for the pure helpers of services/event_team_discord_bot.py
(channel intro copy + name/color parsing) and for the team-channel lootboard
delivery pass (web93a). Loaded by file path — module-level imports are
sqlalchemy/stdlib only, so the conftest stubs never interfere."""
import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    name = "_event_team_discord_bot_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "services" / "event_team_discord_bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bot_mod = _load()
_team = SimpleNamespace(name="Reds")


def _event(**kw):
    base = {"kind": "standard", "has_bingo": False}
    base.update(kw)
    return SimpleNamespace(**base)


class TestChannelIntro:
    def test_bingo_never_mentions_rolls(self):
        intro = bot_mod._channel_intro(_event(has_bingo=True, kind="bingo"), _team)
        assert "roll" not in intro.lower()
        assert "tile" in intro.lower()

    def test_board_game_mentions_rolls(self):
        intro = bot_mod._channel_intro(_event(kind="board_game"), _team)
        assert "roll" in intro.lower()

    def test_standard_is_generic(self):
        intro = bot_mod._channel_intro(_event(), _team)
        assert "roll" not in intro.lower()
        assert "Reds" in intro


class TestParseColor:
    def test_hex_parses(self):
        assert bot_mod._parse_color("#ff0000") == 0xFF0000

    def test_garbage_none(self):
        assert bot_mod._parse_color("red") is None
        assert bot_mod._parse_color(None) is None


class TestRoleColorValue:
    def test_reads_an_interactions_color_object(self):
        # interactions.Role.color is a Color, and int(Color) raises — reading
        # it wrong failed every re-sync of a team that had an accent color.
        role = SimpleNamespace(color=SimpleNamespace(value=0x00B900))
        assert bot_mod._role_color_value(role) == 0x00B900

    def test_plain_int_and_missing_color(self):
        assert bot_mod._role_color_value(SimpleNamespace(color=0xFF0000)) == 0xFF0000
        assert bot_mod._role_color_value(SimpleNamespace(color=None)) == 0
        assert bot_mod._role_color_value(SimpleNamespace()) == 0


class TestExpectedMemberErrorFilter:
    """The bots/main.py logging filter — re-implemented check against the
    exact record shapes interactions emits (kept in sync by string contract)."""

    def _filter(self):
        import logging

        class F(logging.Filter):
            def filter(self, record):
                try:
                    msg = record.getMessage()
                except Exception:
                    return True
                if "/members/" not in msg and "/thread-members/" not in msg:
                    return True
                stripped = msg.rstrip()
                return not (stripped.endswith(": 403") or stripped.endswith(": 404"))

        return F()

    def _record(self, msg):
        import logging

        return logging.LogRecord("interactions", logging.ERROR, __file__, 1,
                                 msg, None, None)

    def test_member_404_dropped(self):
        f = self._filter()
        assert not f.filter(self._record(
            "GET::https://discord.com/api/v10/guilds/1/members/2: 404"))
        assert not f.filter(self._record(
            "PUT::https://discord.com/api/v10/guilds/1/members/2/roles/3: 403"))
        assert not f.filter(self._record(
            "PUT::https://discord.com/api/v10/channels/9/thread-members/2: 404"))

    def test_other_errors_kept(self):
        f = self._filter()
        assert f.filter(self._record(
            "GET::https://discord.com/api/v10/guilds/1/members/2: 500"))
        assert f.filter(self._record(
            "POST::https://discord.com/api/v10/channels/9/messages: 403"))
        assert f.filter(self._record("some unrelated error"))


class TestSyncMembersOutcomes:
    """The tri-state member sync: applied/absent are 'handled', transient
    errors are retried (never silently marked handled — the 2026-07-17
    outage regression)."""

    def _run(self, outcomes: dict, desired: set, state=None):
        import asyncio
        import json

        ix_errors = _install_fake_interactions_errors()

        class Http:
            async def add_guild_member_role(self, gid, uid, rid, reason=None):
                kind = outcomes.get(str(uid), "ok")
                if kind == "absent":
                    raise ix_errors.NotFound()
                if kind == "forbidden":
                    raise ix_errors.Forbidden()
                if kind == "error":
                    raise RuntimeError("boom")

            async def remove_guild_member_role(self, gid, uid, rid, reason=None):
                return await self.add_guild_member_role(gid, uid, rid, reason)

        class Bot:
            http = Http()

        row = SimpleNamespace(guild_id="1", role_id="2", channel_id=None,
                              channel_kind=None, last_error=None,
                              member_state=json.dumps(sorted(state)) if state else None)
        converged = asyncio.get_event_loop().run_until_complete(
            bot_mod._sync_members(Bot(), None, row, desired))
        return converged, set(json.loads(row.member_state)), row

    def test_ok_and_absent_are_handled(self):
        converged, state, _row = self._run(
            {"10": "ok", "11": "absent"}, {"10", "11"})
        assert converged
        assert state == {"10", "11"}

    def test_forbidden_not_marked_handled(self):
        # Audit P0-11: a 403 (bot role below the team role / Manage Roles
        # revoked) must NOT be treated as "absent" — the member never got the
        # role while the UI read "synced". It stays unhandled (retried once
        # perms are fixed) and leaves an actionable last_error on the row.
        converged, state, row = self._run(
            {"10": "ok", "12": "forbidden"}, {"10", "12"})
        assert not converged
        assert state == {"10"}
        assert row.last_error and "Manage Roles" in row.last_error

    def test_transient_error_not_marked_handled(self):
        converged, state, _row = self._run(
            {"10": "ok", "11": "error"}, {"10", "11"})
        assert not converged  # stays members_dirty — retried next pass
        assert state == {"10"}

    def test_remove_error_keeps_id_for_retry(self):
        converged, state, _row = self._run(
            {"99": "error"}, set(), state={"99"})
        assert not converged
        assert state == {"99"}


class _Msg:
    """A bot-owned Discord message: records edits, deletes and pins."""

    def __init__(self, message_id):
        self.id = message_id
        self.edits = []
        self.deleted = False
        self.pinned = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True

    async def pin(self):
        self.pinned = True


class _Channel:
    def __init__(self, *messages, next_id=9000):
        self.messages = {str(m.id): m for m in messages}
        self.sends = []
        self._next_id = next_id

    async def fetch_message(self, message_id=None):
        # Mirrors interactions' Channel.fetch_message: a 404 comes back as
        # None on the VALUE path, so every exception out of here means
        # something other than "deleted" — which is the whole distinction the
        # delivery code hangs on.
        return self.messages.get(str(message_id))

    async def send(self, **kwargs):
        self._next_id += 1
        message = _Msg(self._next_id)
        self.messages[str(message.id)] = message
        self.sends.append(kwargs)
        return message


class _NotFound(Exception):
    """Discord's "this object is gone". Carries ``status`` because that is
    what the delivery code reads — it recognises interactions' NotFound and
    anything else advertising a 404, and treats everything else as transient."""

    status = 404


class _UnreachableChannel(_Channel):
    """A channel whose messages exist but cannot be reached this tick —
    Forbidden, a 5xx, a dropped connection. Reposting into it would fork the
    message it failed to reach."""

    def __init__(self, *messages, fail_on="fetch", **kwargs):
        super().__init__(*messages, **kwargs)
        self.fail_on = fail_on

    async def fetch_message(self, message_id=None):
        if self.fail_on == "fetch":
            raise RuntimeError("503 Service Unavailable")
        message = await super().fetch_message(message_id=message_id)
        if message is not None:
            async def _boom(**kwargs):
                raise RuntimeError("503 Service Unavailable")
            message.edit = _boom
        return message


class _Bot:
    def __init__(self, channel):
        self.channel = channel
        self.fetched = []

    async def fetch_channel(self, channel_id):
        self.fetched.append(channel_id)
        return self.channel


class _Session:
    """Chainable stand-in: ``first()`` answers the EventTeam lookup, ``all()``
    the candidate-row query."""

    def __init__(self, team=None, rows=None):
        self.team = team
        self.rows = rows or []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def query(self, *args):
        return self

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def first(self):
        return self.team

    def all(self):
        return self.rows

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _install_team_discord(monkeypatch):
    """Register the REAL services/event_team_discord.py under its canonical
    name for the duration of one test.

    The conftest stubs ``services`` as a bare MagicMock, so the delivery pass's
    lazy ``from services.event_team_discord import LIVE_CHANNEL_STATUSES``
    cannot resolve on its own ('services' is not a package). That module is
    documented stdlib-only at import time, so loading it by path is safe and
    the tests assert against the real status tuple rather than a copy of it."""
    name = "services.event_team_discord"
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "services" / "event_team_discord.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _install_fake_layouts(monkeypatch):
    """``services`` is a bare MagicMock under the conftest stubs, so the
    payload builder's lazy ``from services.event_message_layouts import ...``
    cannot resolve ('services' is not a package). Register a minimal
    stand-in implementing the two functions it uses, so the test can assert on
    the resolved heading and the attachment:// reference."""
    import types

    module = types.ModuleType("services.event_message_layouts")

    def render_message_spec(layout, context, **kwargs):
        blocks = []
        for block in layout.get("blocks") or []:
            content = block["content"]
            for key, value in (context or {}).items():
                content = content.replace("{" + key + "}", str(value))
            blocks.append({"type": "text", "content": content})
        return {"blocks": blocks}

    def build_components(spec, image_ref=None, **kwargs):
        return {"blocks": spec["blocks"], "image_ref": image_ref}

    module.render_message_spec = render_message_spec
    module.build_components = build_components
    monkeypatch.setitem(sys.modules, "services.event_message_layouts", module)
    return module


class TestTeamLootPosts:
    """Delivery of the per-team lootboard: a SECOND, continuously-updated
    message that must live directly beneath the team's primary board post."""

    # A *complete* stand-in image: signature, some payload, and PNG's IEND
    # terminator — the delivery pass refuses anything that does not end with
    # it, because the generator writes this file in place from another process
    # and a truncated read must never be uploaded (let alone cached).
    PNG = b"\x89PNG\r\n\x1a\n" + b"team-loot-payload" + b"\x00\x00\x00\x00IEND\xaeB`\x82"

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _event(self, **kw):
        base = {"id": 42, "group_id": 7, "name": "Summer Bingo",
                "status": "active", "visibility": "public"}
        base.update(kw)
        return SimpleNamespace(**base)

    def _team_row(self, **kw):
        return SimpleNamespace(id=3, name="Red", group_id=None, auto_clan=False,
                               **kw)

    def _row(self, **kw):
        base = {"id": 1, "event_id": 42, "team_id": 3, "group_id": 7,
                "guild_id": "9", "channel_id": "123", "sync_status": "synced",
                "board_message_id": "100", "loot_message_id": None,
                "loot_state_hash": None, "loot_updated_at": None}
        base.update(kw)
        return SimpleNamespace(**base)

    def _setup(self, monkeypatch, tmp_path, *, png=PNG):
        """Point the generator's path helper at a temp PNG (or a missing one
        when ``png`` is None) and make the payload builder resolvable."""
        from lootboard import team_boards as tb

        path = tmp_path / "lootboard.png"
        if png is not None:
            path.write_bytes(png)
        monkeypatch.setenv(tb.FEATURE_FLAG_ENV, "1")
        monkeypatch.setattr(tb, "team_board_path", lambda *a, **k: str(path))
        _install_fake_layouts(monkeypatch)
        _install_team_discord(monkeypatch)
        return path

    def _hash_for(self, event, team, png=PNG):
        from lootboard import team_boards as tb

        return bot_mod._loot_state_hash(png, tb.board_title(event, team))

    # -- ordering ---------------------------------------------------------- #
    def test_nothing_posted_before_the_board_post_exists(self, tmp_path, monkeypatch):
        # Discord orders by message id, so a lootboard created before the board
        # post would sit ABOVE it forever.
        self._setup(monkeypatch, tmp_path)
        event, row = self._event(), self._row(board_message_id=None)
        channel, session = _Channel(), _Session(team=self._team_row())
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (False, False)
        assert not bot.fetched and not channel.sends
        assert row.loot_message_id is None

    def test_first_post_stores_the_message_id_and_is_not_pinned(self, tmp_path,
                                                                monkeypatch):
        self._setup(monkeypatch, tmp_path)
        event, team, row = self._event(), self._team_row(), self._row()
        channel, session = _Channel(), _Session(team=team)
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert len(channel.sends) == 1
        assert channel.sends[0]["files"] is not None
        assert row.loot_message_id == "9001"
        assert int(row.loot_message_id) > int(row.board_message_id)  # beneath it
        assert row.loot_state_hash == self._hash_for(event, team)
        assert row.loot_updated_at is not None
        # The board post is the channel's pinned message; a second pin would
        # bury it.
        assert channel.messages["9001"].pinned is False
        # Read transaction released before the disk read + upload.
        assert session.commits >= 1

    # -- cheap no-op ------------------------------------------------------- #
    def test_unchanged_image_makes_no_discord_call(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        row = self._row(loot_message_id="9001",
                        loot_state_hash=self._hash_for(event, team),
                        loot_updated_at=datetime.now())
        channel, session = _Channel(_Msg(9001)), _Session(team=team)
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (False, False)
        assert not bot.fetched  # no channel fetch, no message fetch, no edit
        assert not channel.sends and not channel.messages["9001"].edits

    def test_rewritten_but_identical_image_only_restamps(self, tmp_path,
                                                         monkeypatch):
        # The generator rewrites on a timer, not on change: a fresh mtime with
        # identical bytes must still cost zero Discord calls.
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        stale = datetime.now() - timedelta(hours=2)
        row = self._row(loot_message_id="9001",
                        loot_state_hash=self._hash_for(event, team),
                        loot_updated_at=stale)
        channel, session = _Channel(_Msg(9001)), _Session(team=team)
        bot = _Bot(channel)

        wrote, called = self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row))
        assert (wrote, called) == (True, False)
        assert row.loot_updated_at > stale
        assert not bot.fetched and not channel.sends
        assert not channel.messages["9001"].edits

    # -- in-place update --------------------------------------------------- #
    def test_changed_image_edits_in_place_and_clears_attachments(self, tmp_path,
                                                                 monkeypatch):
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        existing = _Msg(9001)
        row = self._row(loot_message_id="9001", loot_state_hash="stale-hash",
                        loot_updated_at=datetime.now() - timedelta(hours=2))
        channel, session = _Channel(existing), _Session(team=team)
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert not channel.sends  # edited, never reposted
        assert len(existing.edits) == 1
        # attachments=[] drops the previous upload so files don't accumulate.
        assert existing.edits[0]["attachments"] == []
        assert existing.edits[0]["files"] is not None
        assert row.loot_message_id == "9001"
        assert row.loot_state_hash == self._hash_for(event, team)

    def test_vanished_message_is_reposted(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        row = self._row(loot_message_id="9001", loot_state_hash="stale-hash",
                        loot_updated_at=datetime.now() - timedelta(hours=2))
        channel, session = _Channel(), _Session(team=team)  # 9001 is gone
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert len(channel.sends) == 1
        assert row.loot_message_id == "9001"
        assert row.loot_state_hash == self._hash_for(event, team)

    # -- unreachable, but NOT gone ----------------------------------------- #
    # The 2026-08-19 fork: a message that could not be reached was treated as
    # deleted, so the pass posted a replacement and abandoned the original —
    # which stayed in the channel, frozen, holding the position and the pin.
    def test_unreachable_message_is_never_forked(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        ours = _Msg(9001)
        row = self._row(loot_message_id="9001", loot_state_hash="stale-hash",
                        loot_updated_at=datetime.now() - timedelta(hours=2))
        channel = _UnreachableChannel(ours, fail_on="fetch")
        session, bot = _Session(team=team), _Bot(channel)

        # The attempt is spent (it cost a real round trip) but nothing else
        # moves: no repost, no new id, and the delivered hash still describes
        # the image that IS on the message.
        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert not channel.sends
        assert ours.deleted is False
        assert row.loot_message_id == "9001"
        assert row.loot_state_hash == "stale-hash"
        # Stamped, so a channel that is broken every tick rotates to the back
        # of the stalest-first queue instead of leading it forever.
        assert row.loot_updated_at is not None

    def test_edit_that_cannot_be_delivered_is_never_forked(self, tmp_path,
                                                           monkeypatch):
        # A message can be fetched (possibly from cache) and still fail to
        # write — same rule applies.
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        row = self._row(loot_message_id="9001", loot_state_hash="stale-hash",
                        loot_updated_at=datetime.now() - timedelta(hours=2))
        channel = _UnreachableChannel(_Msg(9001), fail_on="edit")
        session, bot = _Session(team=team), _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert not channel.sends
        assert row.loot_message_id == "9001"
        assert row.loot_state_hash == "stale-hash"

    def test_message_deleted_between_fetch_and_edit_is_reposted(
            self, tmp_path, monkeypatch):
        # The fetch may be answered from cache, so "gone" can surface on the
        # edit. A 404 there IS a deletion and must repost.
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        stale = _Msg(9001)

        async def _gone(**kwargs):
            raise _NotFound()

        stale.edit = _gone
        row = self._row(loot_message_id="9001", loot_state_hash="stale-hash",
                        loot_updated_at=datetime.now() - timedelta(hours=2))
        channel, session = _Channel(stale), _Session(team=team)
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert len(channel.sends) == 1
        assert row.loot_state_hash == self._hash_for(event, team)

    def test_repost_deletes_the_message_it_replaces(self, tmp_path, monkeypatch):
        # Belt and braces: even on a path that believes the old message is
        # gone, the replacement is only sent after trying to delete it — a
        # repost must never be able to leave two copies behind.
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        stale = _Msg(9001)

        async def _gone(**kwargs):
            raise _NotFound()

        stale.edit = _gone
        row = self._row(loot_message_id="9001", loot_state_hash="stale-hash",
                        loot_updated_at=datetime.now() - timedelta(hours=2))
        channel, session = _Channel(stale), _Session(team=team)

        self._run(bot_mod._refresh_one_loot_post(_Bot(channel), session,
                                                 event, row))
        assert stale.deleted is True
        assert len(channel.sends) == 1

    # -- re-created board post --------------------------------------------- #
    def test_recreated_board_post_recreates_the_lootboard_beneath_it(
            self, tmp_path, monkeypatch):
        # The board post was deleted and re-sent, so its id is now NEWER than
        # ours: our message sits above it. Delete ours (the only message this
        # code owns) and repost underneath — even though the image is unchanged.
        self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        ours = _Msg(4000)
        row = self._row(board_message_id="5000", loot_message_id="4000",
                        loot_state_hash=self._hash_for(event, team),
                        loot_updated_at=datetime.now())
        channel, session = _Channel(ours), _Session(team=team)
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert ours.deleted is True
        assert len(channel.sends) == 1
        assert row.loot_message_id == "9001"
        assert int(row.loot_message_id) > int(row.board_message_id)

    def test_ordering_check_is_pure_arithmetic(self):
        below = bot_mod._loot_post_is_below_board
        assert below(SimpleNamespace(loot_message_id="200", board_message_id="100"))
        assert not below(SimpleNamespace(loot_message_id="100", board_message_id="200"))
        # Never guesses when an id is missing/unparseable — this decides a delete.
        assert below(SimpleNamespace(loot_message_id=None, board_message_id="100"))
        assert below(SimpleNamespace(loot_message_id="x", board_message_id="100"))

    # -- missing image ----------------------------------------------------- #
    def test_missing_png_is_a_silent_no_op(self, tmp_path, monkeypatch):
        # The generator is a different process on an hourly throttle: "no image
        # yet" is a normal state, every 60s, forever.
        self._setup(monkeypatch, tmp_path, png=None)
        event, row = self._event(), self._row()
        channel, session = _Channel(), _Session(team=self._team_row())
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (False, False)
        assert not bot.fetched and not channel.sends
        assert row.loot_message_id is None and row.loot_state_hash is None

    # -- the pass ---------------------------------------------------------- #
    def test_flag_off_is_a_complete_no_op(self, monkeypatch):
        from lootboard import team_boards as tb

        monkeypatch.delenv(tb.FEATURE_FLAG_ENV, raising=False)

        def factory():
            raise AssertionError("opened a session with the feature flag off")

        self._run(bot_mod._loot_post_pass(object(), factory))

    def test_pass_delivers_and_respects_the_write_budget(self, tmp_path,
                                                         monkeypatch):
        self._setup(monkeypatch, tmp_path)
        event = self._event()
        rows = [self._row(id=n, team_id=n) for n in (1, 2, 3)]
        channel = _Channel()
        session = _Session(team=self._team_row(),
                           rows=[(row, event) for row in rows])
        bot = _Bot(channel)

        self._run(bot_mod._loot_post_pass(bot, lambda: session, write_budget=1))

        assert len(channel.sends) == 1          # bounded burst on a cold start
        assert rows[0].loot_message_id == "9001"
        assert rows[1].loot_message_id is None  # converges on the next tick
        assert session.commits >= 1 and session.closed is True

    def test_pass_skips_private_events(self, tmp_path, monkeypatch):
        # A private event is never rendered to the public /img tree; there is
        # nothing to deliver even if a stale PNG exists on disk.
        self._setup(monkeypatch, tmp_path)
        event = self._event(visibility="private")
        row = self._row()
        channel = _Channel()
        session = _Session(team=self._team_row(), rows=[(row, event)])

        self._run(bot_mod._loot_post_pass(_Bot(channel), lambda: session))

        assert not channel.sends and row.loot_message_id is None

    def test_row_failure_rolls_back_and_does_not_poison_the_pass(self, tmp_path,
                                                                 monkeypatch):
        self._setup(monkeypatch, tmp_path)
        event = self._event()
        bad, good = self._row(id=1, team_id=1), self._row(id=2, team_id=2)
        channel = _Channel()
        session = _Session(team=self._team_row(), rows=[(bad, event), (good, event)])

        class _AngryBot(_Bot):
            async def fetch_channel(self, channel_id):
                self.fetched.append(channel_id)
                if len(self.fetched) == 1:
                    raise RuntimeError("Forbidden")
                return self.channel

        self._run(bot_mod._loot_post_pass(_AngryBot(channel), lambda: session))

        assert session.rollbacks == 1
        assert bad.loot_message_id is None
        assert good.loot_message_id == "9001"

    # -- failures are charged, and back off -------------------------------- #
    def test_raising_rows_spend_the_tick_budget(self, tmp_path, monkeypatch):
        # A 403/404/5xx costs the same round trip as a success (and 403s count
        # toward Discord's invalid-request ban budget), so a failure MUST spend
        # budget. Counting only successes made the real ceiling
        # LOOT_POST_SCAN_LIMIT requests per tick, forever, whenever something
        # systematic broke the write.
        self._setup(monkeypatch, tmp_path)
        event = self._event()
        rows = [self._row(id=n, team_id=n) for n in range(1, 7)]
        session = _Session(team=self._team_row(),
                           rows=[(row, event) for row in rows])

        class _BrokenBot(_Bot):
            async def fetch_channel(self, channel_id):
                self.fetched.append(channel_id)
                raise RuntimeError("403 Forbidden: Missing Access")

        bot = _BrokenBot(_Channel())
        self._run(bot_mod._loot_post_pass(bot, lambda: session, write_budget=2))

        assert len(bot.fetched) == 2  # not one request per candidate row
        assert session.rollbacks == 2

    def test_failed_row_is_stamped_so_it_stops_leading_the_queue(
            self, tmp_path, monkeypatch):
        # Rows are ordered stalest-first with NULL first, so a rolled-back row
        # that keeps its NULL loot_updated_at re-leads the queue 60s later and
        # monopolises the budget. The failure stamp rotates it to the back.
        self._setup(monkeypatch, tmp_path)
        event = self._event()
        rows = [self._row(id=n, team_id=n) for n in (1, 2)]
        session = _Session(team=self._team_row(),
                           rows=[(row, event) for row in rows])

        class _BrokenBot(_Bot):
            async def fetch_channel(self, channel_id):
                self.fetched.append(channel_id)
                raise RuntimeError("Forbidden")

        self._run(bot_mod._loot_post_pass(
            _BrokenBot(_Channel()), lambda: session, write_budget=1))

        assert rows[0].loot_updated_at is not None  # spent + rotated
        assert rows[0].loot_message_id is None      # nothing was delivered
        assert rows[1].loot_updated_at is None      # untouched: budget spent
        assert session.commits >= 1                 # the stamp was persisted

    def test_unusable_channel_spends_budget_and_backs_off(self, tmp_path,
                                                          monkeypatch):
        # smart_cache swallows a 403 on fetch_channel and hands back a bare
        # BaseChannel with no send(); the HTTP GET still happened.
        self._setup(monkeypatch, tmp_path)
        event, row = self._event(), self._row()
        session = _Session(team=self._team_row())

        class _BlindBot(_Bot):
            async def fetch_channel(self, channel_id):
                self.fetched.append(channel_id)
                return SimpleNamespace()  # no .send

        bot = _BlindBot(None)
        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert row.loot_updated_at is not None
        assert row.loot_message_id is None and row.loot_state_hash is None

    def test_deleted_channel_spends_budget_and_backs_off(self, tmp_path,
                                                         monkeypatch):
        # Channel deleted while the row still reads sync_status='synced':
        # interactions turns the 404 into None.
        self._setup(monkeypatch, tmp_path)
        event, row = self._event(), self._row()
        session = _Session(team=self._team_row())

        class _GoneBot(_Bot):
            async def fetch_channel(self, channel_id):
                self.fetched.append(channel_id)
                return None

        assert self._run(bot_mod._refresh_one_loot_post(
            _GoneBot(None), session, event, row)) == (True, True)
        assert row.loot_updated_at is not None
        assert row.loot_message_id is None

    def test_stamped_failure_goes_quiet_until_the_png_changes(self, tmp_path,
                                                              monkeypatch):
        # A row that HAS been delivered and then fails is quiet until the
        # generator rewrites the image: the mtime skip covers it, so a
        # permanent Forbidden costs one attempt per regeneration, not one per
        # tick.
        path = self._setup(monkeypatch, tmp_path)
        event, team = self._event(), self._team_row()
        row = self._row(loot_message_id="9001", loot_state_hash="stale-hash",
                        loot_updated_at=datetime.now() - timedelta(hours=2))
        session = _Session(team=team)

        class _BlindBot(_Bot):
            async def fetch_channel(self, channel_id):
                self.fetched.append(channel_id)
                return SimpleNamespace()

        bot = _BlindBot(None)
        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        # Second tick, same unchanged PNG: not even a channel fetch.
        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (False, False)
        assert len(bot.fetched) == 1
        # ...until the generator rewrites it.
        os.utime(path, (os.path.getmtime(path) + 3600,) * 2)
        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert len(bot.fetched) == 2

    # -- torn reads --------------------------------------------------------- #
    def test_truncated_png_is_never_uploaded_or_cached(self, tmp_path,
                                                       monkeypatch):
        # The generator saves in place from another process, so a read can land
        # mid-encode. A torn frame must not be posted — and above all must not
        # be hashed/stamped as delivered, which would freeze it in the channel
        # until the next hourly regeneration.
        self._setup(monkeypatch, tmp_path, png=self.PNG[:-8])  # no IEND
        event, row = self._event(), self._row()
        channel, session = _Channel(), _Session(team=self._team_row())
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (False, False)
        assert not bot.fetched and not channel.sends
        assert row.loot_message_id is None and row.loot_state_hash is None
        assert row.loot_updated_at is None  # nothing cached: retried next tick

    def test_png_rewritten_during_the_read_is_rejected(self, tmp_path,
                                                       monkeypatch):
        # The completeness check cannot see a tear that happens to leave a
        # valid trailer, so the file must also be unchanged across the read.
        path = tmp_path / "lootboard.png"
        path.write_bytes(self.PNG)
        real_stat = bot_mod.os.stat
        seen = []

        def moving_stat(target, *args, **kwargs):
            result = real_stat(target, *args, **kwargs)
            if str(target) != str(path):
                return result
            seen.append(target)
            return SimpleNamespace(st_size=result.st_size + len(seen) - 1,
                                   st_mtime_ns=result.st_mtime_ns + len(seen) - 1,
                                   st_mtime=result.st_mtime)

        monkeypatch.setattr(bot_mod.os, "stat", moving_stat)
        assert bot_mod._read_png(str(path)) == (None, None)

    def test_complete_png_reads_back_with_its_mtime(self, tmp_path):
        path = tmp_path / "lootboard.png"
        path.write_bytes(self.PNG)

        png, mtime = bot_mod._read_png(str(path))

        assert png == self.PNG
        assert mtime == path.stat().st_mtime

    def test_delivery_stamp_is_the_files_mtime_not_wall_clock(self, tmp_path,
                                                              monkeypatch):
        # Stamping now() marks a regeneration that landed between the read and
        # the stamp as "already delivered", and the mtime skip then discards
        # the newer image until the following hourly render.
        path = self._setup(monkeypatch, tmp_path)
        event, team, row = self._event(), self._team_row(), self._row()
        rendered_at = os.path.getmtime(path) - 30
        os.utime(path, (rendered_at, rendered_at))
        channel, session = _Channel(), _Session(team=team)

        assert self._run(bot_mod._refresh_one_loot_post(
            _Bot(channel), session, event, row)) == (True, True)
        assert row.loot_updated_at == datetime.fromtimestamp(rendered_at)
        assert row.loot_updated_at < datetime.now()

    def test_image_regenerated_during_delivery_is_still_delivered(
            self, tmp_path, monkeypatch):
        # v2 is written while v1 is being uploaded: its mtime is newer than the
        # file we read but still older than wall clock. With a wall-clock stamp
        # the mtime skip swallowed it for an hour.
        path = self._setup(monkeypatch, tmp_path)
        event, team, row = self._event(), self._team_row(), self._row()
        v1_mtime = os.path.getmtime(path) - 30
        os.utime(path, (v1_mtime, v1_mtime))
        channel, session = _Channel(), _Session(team=team)
        bot = _Bot(channel)

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)

        v2 = b"\x89PNG\r\n\x1a\n" + b"v2-payload" + b"\x00\x00\x00\x00IEND\xaeB`\x82"
        path.write_bytes(v2)
        os.utime(path, (v1_mtime + 5, v1_mtime + 5))  # still in the past

        assert self._run(bot_mod._refresh_one_loot_post(
            bot, session, event, row)) == (True, True)
        assert len(channel.messages["9001"].edits) == 1  # v2 actually landed
        assert row.loot_state_hash == self._hash_for(event, team, png=v2)

    # -- payload ----------------------------------------------------------- #
    def test_payload_references_the_attachment_not_a_url(self, monkeypatch):
        # Media galleries fed an external URL spin forever in the client, so the
        # image must be delivered as an upload the message references.
        _install_fake_layouts(monkeypatch)
        event, team, row = self._event(), self._team_row(), self._row()

        components, loot_file = bot_mod._team_loot_payload(
            event, row, team, self.PNG)

        assert components["image_ref"] == "attachment://team-loot-42-3.png"
        assert loot_file is not None
        heading = components["blocks"][0]["content"]
        assert heading.startswith("## ")
        assert "Red" in heading and "Summer Bingo" in heading
        assert "loot" in heading.lower()


def _install_fake_interactions_errors():
    """The conftest stubs `interactions` as a bare MagicMock (not a package),
    so the module's lazy `from interactions.client import errors` can't
    resolve. Register a minimal errors module carrying the two classes the
    sync code catches; the fakes are what the test raises too."""
    import types

    errors = sys.modules.get("interactions.client.errors")
    if errors is None or not isinstance(getattr(errors, "NotFound", None), type):
        errors = types.ModuleType("interactions.client.errors")

        class NotFound(Exception):
            pass

        class Forbidden(Exception):
            pass

        errors.NotFound = NotFound
        errors.Forbidden = Forbidden
        client_pkg = types.ModuleType("interactions.client")
        client_pkg.errors = errors
        sys.modules["interactions.client"] = client_pkg
        sys.modules["interactions.client.errors"] = errors
    return errors


class _FakeRedis:
    """Just enough redis for the retry gate: exists/incr/expire/set/delete
    with no TTL clock (nothing here needs one to expire mid-test — a window
    elapsing is modelled by deleting the gate key)."""

    def __init__(self, *, broken=False):
        self.store = {}
        self.ttls = {}
        self.broken = broken

    def _check(self):
        if self.broken:
            raise RuntimeError("redis is down")

    def exists(self, key):
        self._check()
        return 1 if key in self.store else 0

    def incr(self, key):
        self._check()
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, seconds):
        self._check()
        self.ttls[key] = seconds

    def set(self, key, value, ex=None):
        self._check()
        self.store[key] = value
        self.ttls[key] = ex

    def delete(self, *keys):
        self._check()
        for key in keys:
            self.store.pop(key, None)
            self.ttls.pop(key, None)


class _RedisClient:
    def __init__(self, **kwargs):
        self.client = _FakeRedis(**kwargs)


class TestFailedRowRetry:
    """``failed`` used to be terminal: a row waited for a human to re-save a
    settings page they had no reason to revisit, so an admin who fixed the
    permission in Discord saw nothing happen. Now it is retried on an
    escalating backoff."""

    def _gate(self, rc, row_id=7):
        return rc.client.store.get(bot_mod._retry_gate_key(row_id))

    def _ttl(self, rc, row_id=7):
        return rc.client.ttls.get(bot_mod._retry_gate_key(row_id))

    def test_a_row_with_no_history_is_due(self):
        assert bot_mod._retry_is_due(_RedisClient(), 7) is True

    def test_arming_holds_the_row_back(self):
        rc = _RedisClient()
        bot_mod._arm_retry_backoff(rc, 7)
        assert bot_mod._retry_is_due(rc, 7) is False

    def test_backoff_widens_with_each_consecutive_failure(self):
        rc = _RedisClient()
        seen = []
        for _ in range(len(bot_mod._RETRY_BACKOFF_SECONDS) + 2):
            bot_mod._arm_retry_backoff(rc, 7)
            seen.append(self._ttl(rc))
            rc.client.delete(bot_mod._retry_gate_key(7))  # window elapses
        assert seen[:len(bot_mod._RETRY_BACKOFF_SECONDS)] == \
            list(bot_mod._RETRY_BACKOFF_SECONDS)
        # …and then holds at the ceiling rather than running away.
        assert seen[-1] == bot_mod._RETRY_BACKOFF_SECONDS[-1]

    def test_the_counter_outlives_its_own_window(self):
        # Otherwise every retry would look like a first failure and the
        # backoff would never escalate past its shortest step.
        rc = _RedisClient()
        bot_mod._arm_retry_backoff(rc, 7)
        assert rc.client.ttls[bot_mod._retry_count_key(7)] > self._ttl(rc)

    def test_success_forgets_the_failure_history(self):
        rc = _RedisClient()
        bot_mod._arm_retry_backoff(rc, 7)
        bot_mod._arm_retry_backoff(rc, 7)
        bot_mod._clear_retry_backoff(rc, 7)
        assert bot_mod._retry_is_due(rc, 7) is True
        bot_mod._arm_retry_backoff(rc, 7)
        assert self._ttl(rc) == bot_mod._RETRY_BACKOFF_SECONDS[0]

    def test_redis_down_fails_open(self):
        # A row that is never retried is the failure this mechanism exists to
        # prevent; one extra attempt per pass is the cheaper wrong answer.
        rc = _RedisClient(broken=True)
        assert bot_mod._retry_is_due(rc, 7) is True
        bot_mod._arm_retry_backoff(rc, 7)   # must not raise
        bot_mod._clear_retry_backoff(rc, 7)

    def test_taking_rows_arms_them_and_respects_the_cap(self):
        rc = _RedisClient()
        rows = [SimpleNamespace(id=n) for n in range(1, 20)]
        session = _Session(rows=rows)

        taken = bot_mod._take_failed_rows_for_retry(session, rc)

        assert len(taken) == bot_mod.FAILED_RETRY_LIMIT
        # Armed AS THEY ARE TAKEN, not in the failure handler: a restart
        # mid-retry must not turn into an unthrottled retry loop.
        assert all(not bot_mod._retry_is_due(rc, row.id) for row in taken)
        # Rows past the cap are untouched and lead the next pass.
        assert bot_mod._retry_is_due(rc, rows[-1].id) is True

    def test_rows_still_in_backoff_are_skipped(self):
        rc = _RedisClient()
        rows = [SimpleNamespace(id=n) for n in (1, 2, 3)]
        bot_mod._arm_retry_backoff(rc, 1)
        bot_mod._arm_retry_backoff(rc, 2)

        taken = bot_mod._take_failed_rows_for_retry(_Session(rows=rows), rc)

        assert [row.id for row in taken] == [3]


class TestNotFoundClassifier:
    """The one question that decides between "edit again later" and "post a
    replacement"."""

    def test_a_404_is_gone(self):
        assert bot_mod._is_not_found(_NotFound()) is True

    def test_interactions_not_found_is_gone(self):
        from interactions.client import errors as ix_errors

        assert bot_mod._is_not_found(
            ix_errors.NotFound.__new__(ix_errors.NotFound)) is True

    def test_everything_else_is_transient(self):
        # Forbidden, 5xx, timeouts, connection resets — the message is
        # probably still there, so reposting would fork it.
        assert bot_mod._is_not_found(RuntimeError("503")) is False
        assert bot_mod._is_not_found(SimpleNamespace(status=403)) is False
        assert bot_mod._is_not_found(TimeoutError()) is False
