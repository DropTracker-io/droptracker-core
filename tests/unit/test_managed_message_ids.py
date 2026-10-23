"""Bot-managed message ids: never edited by hand, reset with Re-post board.

Admins used to see ``lootboard_message_id`` (and the Clan Log and PvP board
equivalents) as plain text fields. Some pasted an id there, pointing the bot at
a message it could not edit, and their board silently stopped updating. What
these pin:

* the registry marks exactly those keys managed, and the website's copy agrees;
* no settings save (website or plugin) can change one;
* Re-post board forgets the id (keeping the row), asks the bot to delete the
  old board, audits it, and drops the poster's posted marker;
* the bot deletes the old board only when it is the bot's own message.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.local_artifacts import ts_registry
from web_api import config_registry as reg
from web_api.common import ProblemException
import web_api.routes.config as cfg

MESSAGE_KEYS = {"lootboard_message_id", "clan_log_message_id", "pvp_lootboard_message_id"}


def test_exactly_the_board_message_ids_are_managed():
    assert reg.MANAGED_KEYS == MESSAGE_KEYS
    assert {spec[1] for spec in cfg.REPOST_BOARDS.values()} == MESSAGE_KEYS


@pytest.mark.skipif(ts_registry("group-config.ts") is None, reason="web repo not checked out beside this one")
def test_the_website_registry_marks_the_same_keys_managed():
    src = ts_registry("group-config.ts").read_text(encoding="utf-8")
    managed = {m.group(1) for m in re.finditer(r'\{\s*key:\s*"(\w+)"[^\n]*managed:\s*true', src)}
    assert managed == MESSAGE_KEYS


def test_a_settings_save_cannot_change_a_managed_key():
    with pytest.raises(ProblemException) as e:
        cfg.apply_group_config_patch(7, 42, {"lootboard_message_id": "123456789012345678"})
    assert e.value.status == 422 and "Re-post board" in e.value.detail


def test_the_plugin_cannot_change_one_either():
    pa = pytest.importorskip("api.routes.plugin_admin")
    errors = pa._validate(None, 7, 42, {"clan_log_message_id": "1"})
    assert errors == {"clan_log_message_id": pa.MANAGED}


# --------------------------------------------------------------------------- #
# Re-post board
# --------------------------------------------------------------------------- #
class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, rows):
        self.rows, self.added, self.committed = rows, [], False

    def query(self, *a):
        return _Query(self.rows)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True


class _CM:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


class _Redis:
    def __init__(self):
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)


class _FakeConfigRow(SimpleNamespace):
    """Stands in for the ORM class: column attributes for the query, kwargs
    for a new row."""
    group_id = config_key = id = MagicMock()


def _row(key, value):
    return SimpleNamespace(config_key=key, config_value=value)


@pytest.fixture
def wired(monkeypatch):
    state = {"enqueued": [], "invalidated": [], "redis": _Redis()}

    def make(rows):
        s = _Session(rows)
        monkeypatch.setattr(cfg, "db_session", lambda: _CM(s))
        monkeypatch.setattr(cfg, "load_user", lambda s, uid: SimpleNamespace(user_id=uid))
        monkeypatch.setattr(cfg, "manageable_guild_ids", lambda uid: set())
        monkeypatch.setattr(cfg, "assert_group_admin", lambda *a, **k: None)
        monkeypatch.setattr(cfg, "GroupConfiguration", _FakeConfigRow)
        monkeypatch.setattr(cfg, "AuditLog", lambda **kw: SimpleNamespace(kind="audit", **kw))
        monkeypatch.setattr(cfg.group_config, "invalidate", lambda gid, *a: state["invalidated"].append(gid))
        monkeypatch.setattr(cfg, "_rc", lambda: state["redis"])
        import sys

        # conftest stubs the services package; the route imports the outbox from it.
        monkeypatch.setattr(sys.modules["services"], "discord_outbox",
                            SimpleNamespace(enqueue=lambda s, **kw: state["enqueued"].append(kw)), raising=False)
        state["session"] = s
        return state
    return make


def test_repost_forgets_the_id_and_asks_the_bot_to_remove_its_old_board(wired):
    message = _row("lootboard_message_id", "123456789012345678")
    state = wired([_row("lootboard_channel_id", "<#998877665544332211>"), message])
    assert cfg.repost_board(7, 42, "lootboard") == {"ok": True, "board": "lootboard"}
    assert message.config_value == ""
    assert state["enqueued"] == [{
        "channel_id": "998877665544332211", "kind": "delete_own_message",
        "discord_message_id": "123456789012345678", "ref_type": "board_repost",
        "actor_user_id": 7, "commit": False}]
    audit = [a for a in state["session"].added if getattr(a, "kind", None) == "audit"]
    assert audit[0].action == "board.repost" and audit[0].before == "123456789012345678"
    assert state["session"].committed and state["invalidated"] == [42]
    assert state["redis"].deleted and state["redis"].deleted[0].endswith("42")


def test_repost_creates_the_row_the_lootboard_poster_needs(wired):
    state = wired([_row("lootboard_channel_id", "998877665544332211")])
    cfg.repost_board(7, 42, "lootboard")
    created = [a for a in state["session"].added if getattr(a, "config_key", None) == "lootboard_message_id"]
    assert created and created[0].config_value == ""
    assert state["enqueued"] == []  # nothing to delete


def test_repost_ignores_a_stored_value_that_is_not_a_message_id(wired):
    state = wired([_row("clan_log_channel_id", "998877665544332211"), _row("clan_log_message_id", "0")])
    cfg.repost_board(7, 42, "clan_log")
    assert state["enqueued"] == []
    assert state["redis"].deleted == ["clan_log:42:posted_hash"]


def test_repost_needs_a_channel(wired):
    wired([_row("pvp_lootboard_message_id", "123456789012345678")])
    with pytest.raises(ProblemException) as e:
        cfg.repost_board(7, 42, "pvp_lootboard")
    assert e.value.status == 409


def test_repost_of_an_unknown_board_is_404():
    with pytest.raises(ProblemException) as e:
        cfg.repost_board(7, 42, "hall_of_fame")
    assert e.value.status == 404


# --------------------------------------------------------------------------- #
# The bot only ever deletes its own board
# --------------------------------------------------------------------------- #
def _outbox():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "services" / "discord_outbox.py"
    spec = importlib.util.spec_from_file_location("_real_outbox", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bot(author_id):
    deleted = []

    class Message:
        author = SimpleNamespace(id=author_id)

        async def delete(self):
            deleted.append(True)

    class Channel:
        async def fetch_message(self, mid):
            return Message()

    class Bot:
        user = SimpleNamespace(id=111)

        async def fetch_channel(self, cid):
            return Channel()

    return Bot(), deleted


@pytest.mark.parametrize("kind_own_only,author,expect_deleted", [
    (True, 111, True),    # the bot's own board: removed
    (True, 222, False),   # someone else's message: left alone
    (False, 222, True),   # plain delete_message keeps its old behaviour
])
def test_delete_own_message_never_touches_another_authors_message(kind_own_only, author, expect_deleted):
    outbox = _outbox()
    bot, deleted = _bot(author)
    row = SimpleNamespace(channel_id="1", discord_message_id="2")
    asyncio.run(outbox._delete_message(bot, None, row, own_only=kind_own_only))
    assert bool(deleted) is expect_deleted


# --------------------------------------------------------------------------- #
# Posters recover from a board message that isn't the bot's
# --------------------------------------------------------------------------- #
def _poster(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "services" / "pvp_lootboard_poster.py"
    spec = importlib.util.spec_from_file_location("_real_pvp_poster", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    saved = []
    monkeypatch.setattr(module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(module, "build_embed", lambda *a: "embed")
    monkeypatch.setattr(module.interactions, "File", lambda *a, **k: "file")
    monkeypatch.setattr(module, "_save_message_id", lambda gid, mid: saved.append(mid))

    async def thread_ok(*a, **k):
        return "ok"

    threads = SimpleNamespace(PROBLEM_STATES=set(), ensure_thread_open=thread_ok)
    monkeypatch.setitem(sys.modules, "utils.discord_threads", threads)
    return module, saved


def _channel(author_id):
    log = {"edited": 0, "deleted": 0, "sent": 0}

    class Message:
        id = 555
        author = SimpleNamespace(id=author_id)
        attachments = []

        async def edit(self, **k):
            log["edited"] += 1

        async def delete(self):
            log["deleted"] += 1

    class Channel:
        async def fetch_message(self, message_id):
            return Message()

        async def send(self, **k):
            log["sent"] += 1
            return SimpleNamespace(id=777)

    class Bot:
        user = SimpleNamespace(id=111)
        http = None

        async def fetch_channel(self, channel_id):
            return Channel()

    return Bot(), log


@pytest.mark.parametrize("repost", [False, True])
def test_pvp_board_posts_fresh_when_the_stored_message_is_someone_elses(monkeypatch, repost):
    module, saved = _poster(monkeypatch)
    bot, log = _channel(author_id=222)
    target = {"channel": "1", "message": "555", "repost": repost}
    assert asyncio.run(module.post_one(bot, 42, target, 0.0, None)) is True
    assert log == {"edited": 0, "deleted": 0, "sent": 1} and saved == [777]


def test_pvp_board_still_edits_its_own_message(monkeypatch):
    module, saved = _poster(monkeypatch)
    bot, log = _channel(author_id=111)
    asyncio.run(module.post_one(bot, 42, {"channel": "1", "message": "555", "repost": False}, 0.0, None))
    assert log == {"edited": 1, "deleted": 0, "sent": 0} and saved == []
