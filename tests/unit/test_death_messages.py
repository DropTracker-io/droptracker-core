"""Group-configured death message variants (services/notification_service.py).

Covers the pure helpers (tolerant parsing, deterministic pick, ping stripping),
the config reader's long_value precedence (a saved list past 255 chars lives in
long_value with config_value blanked — reading config_value alone would
silently truncate), and the send-path placement modes: content line vs embed
description, including the content-mode replacement map swapping the markdown
link tokens for plain values (message content renders no markdown links).

Loaded directly from the file path (like test_notification_channel_guard.py)
because conftest stubs the ``services`` package.
"""

import importlib.util
import json
import os
import random
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

for _name in (
    "services.contribution_notifications",
    "services.event_notifications",
):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_death_messages_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_death_messages_under_test"] = ns
_spec.loader.exec_module(ns)


class TestParseDeathVariants:
    def test_none_and_empty_yield_no_variants(self):
        assert ns.parse_death_variants(None) == []
        assert ns.parse_death_variants("") == []

    def test_garbage_json_is_dropped(self):
        assert ns.parse_death_variants("not json {") == []

    def test_non_list_json_is_dropped(self):
        assert ns.parse_death_variants('{"a": 1}') == []
        assert ns.parse_death_variants('"just a string"') == []

    def test_non_string_blank_and_oversized_entries_are_dropped(self):
        raw = json.dumps(["ok", 7, "", "   ", None, "x" * 201, "also ok"])
        assert ns.parse_death_variants(raw) == ["ok", "also ok"]


class TestPickDeathVariant:
    def test_empty_list_yields_none(self):
        assert ns.pick_death_variant([]) is None

    def test_seeded_rng_is_deterministic(self):
        variants = ["a", "b", "c", "d"]
        picks = [ns.pick_death_variant(variants, rng=random.Random(7)) for _ in range(3)]
        assert len(set(picks)) == 1
        assert picks[0] in variants


class TestStripPings:
    def test_strips_everyone_here_and_mentions(self):
        text = "@everyone @here <@123> <@!123> <@&456> {player_name} died"
        assert "@everyone" not in ns.strip_death_message_pings(text)
        assert "<@" not in ns.strip_death_message_pings(text)
        assert "{player_name} died" in ns.strip_death_message_pings(text)


def _config_db(rows):
    """rows: SimpleNamespace(config_key, config_value, long_value) list."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


def _row(key, value, long_value=None):
    return SimpleNamespace(config_key=key, config_value=value, long_value=long_value)


class TestDeathMessageConfigReader:
    def _service(self):
        return ns.NotificationService(MagicMock(), MagicMock())

    def test_short_list_read_from_config_value(self):
        raw = json.dumps(["{player_name} has died!"])
        variants, as_embed = self._service()._death_message_config(
            _config_db([_row("death_message_variants", raw)]), 5)
        assert variants == ["{player_name} has died!"]
        assert as_embed is False

    def test_long_list_read_from_long_value(self):
        # The overflow shape web_api/routes/config.py writes for LONG_VALUE_KEYS:
        # config_value blanked, full JSON in long_value.
        raw = json.dumps([f"message number {i} with plenty of padding" for i in range(20)])
        assert len(raw) > 255
        variants, _ = self._service()._death_message_config(
            _config_db([_row("death_message_variants", "", long_value=raw)]), 5)
        assert len(variants) == 20

    def test_embed_description_toggle_parses_truthy(self):
        for stored, expected in (("1", True), ("true", True), ("0", False), ("", False)):
            _, as_embed = self._service()._death_message_config(
                _config_db([_row("death_message_as_embed_description", stored)]), 5)
            assert as_embed is expected, stored

    def test_no_rows_mean_no_variants(self):
        variants, as_embed = self._service()._death_message_config(_config_db([]), 5)
        assert variants == []
        assert as_embed is False


def _send_fixture(monkeypatch, death_rows):
    """A NotificationService wired so send_death_notification_with_session
    reaches the embed+content send with everything else mocked out."""
    monkeypatch.setattr(ns, "has_custom_embeds", lambda gid: False)
    monkeypatch.setattr(ns, "get_formatted_name", lambda name, gid, s, player_id=None: "**Alice**")
    monkeypatch.setattr(ns, "global_footer", "footer")
    monkeypatch.setattr(ns, "player_link", lambda name, pid: f"[{name}](https://x/{pid})")

    service = ns.NotificationService(MagicMock(), MagicMock())
    service.db_ops.get_group_embed = AsyncMock(return_value=None)
    channel = SimpleNamespace(send=AsyncMock())
    service._fetch_sendable_channel = AsyncMock(return_value=(channel, None))
    service._maybe_get_video_url = MagicMock(return_value="")
    service._try_send_component_layout = AsyncMock(return_value=None)
    service._cleanup_processed_local_video_after_send = AsyncMock()
    service._send = AsyncMock()

    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.first.return_value = _row("channel_id_to_post_deaths", "123456789")
    chain.all.return_value = death_rows

    notification = SimpleNamespace(
        id=1, group_id=5, player_id=9,
        status="pending", processed_at=None, error_message=None,
    )
    data = {"player_name": "Alice", "source": "Bandos", "location": "GWD", "region_id": 11346}
    return service, db, notification, data


@pytest.mark.asyncio
async def test_no_variants_keeps_the_default_content_line(monkeypatch):
    service, db, notification, data = _send_fixture(monkeypatch, [])
    await service.send_death_notification_with_session(notification, data, db)
    args, kwargs = service._send.await_args
    assert args[1] == "**Alice** has died!"
    # interactions is conftest-stubbed, so the default embed is a MagicMock;
    # a str description would mean the variant path overwrote it.
    assert not isinstance(kwargs["embed"].description, str)
    assert notification.status == "sent"


@pytest.mark.asyncio
async def test_content_mode_uses_plain_tokens_and_strips_pings(monkeypatch):
    raw = json.dumps(["@everyone {player_name} was bonked by {source} in {location}"])
    service, db, notification, data = _send_fixture(
        monkeypatch, [_row("death_message_variants", raw)])
    await service.send_death_notification_with_session(notification, data, db)
    args, kwargs = service._send.await_args
    # {player_name} resolves to the plain formatted name, not the markdown
    # site link, and the (legacy/hand-edited) @everyone is stripped at send.
    assert args[1] == " **Alice** was bonked by Bandos in GWD"
    assert "[" not in args[1]
    # The embed itself is untouched in content mode (stubbed interactions:
    # a str description would mean the variant path overwrote it).
    assert not isinstance(kwargs["embed"].description, str)


@pytest.mark.asyncio
async def test_embed_mode_replaces_description_and_keeps_content(monkeypatch):
    raw = json.dumps(["{player_name} fell to {source}"])
    service, db, notification, data = _send_fixture(monkeypatch, [
        _row("death_message_variants", raw),
        _row("death_message_as_embed_description", "1"),
    ])
    await service.send_death_notification_with_session(notification, data, db)
    args, kwargs = service._send.await_args
    assert args[1] == "**Alice** has died!"
    # Embed text may use the markdown site-link form of {player_name}.
    assert kwargs["embed"].description == "[Alice](https://x/9) fell to Bandos"
