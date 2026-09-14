"""Death message lines in the death sender (services/notification_service.py).

Covers the group variants' tolerant parsing, the config reader's long_value
precedence (a saved list past 255 chars lives in long_value with config_value
blanked — reading config_value alone would silently truncate), and the send
path: content line vs embed description, the content-mode replacement map
swapping the markdown link tokens for plain values (message content renders no
markdown links), members' own messages winning over the group's, the blank
placeholder skip, {death_message} for layouts and custom embeds, and the
member's own line in their personal DM.

Loaded directly from the file path (like test_notification_channel_guard.py)
because conftest stubs the ``services`` package.
"""

import importlib.util
import json
import os
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


# Picking and rendering a message (random choice, ping stripping, the blank
# placeholder skip) moved to db/member_messages.py with members' own death
# messages; tests/unit/test_member_messages.py covers them.


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


def _send_fixture(monkeypatch, death_rows, member_templates=None):
    """A NotificationService wired so send_death_notification_with_session
    reaches the embed+content send with everything else mocked out.

    ``member_templates`` is what db.member_messages says this group will post
    for this member — the allow/block decision itself is tested there."""
    # From sys.modules, not ``import db.member_messages``: conftest stubs the
    # ``db`` package with a MagicMock, whose attribute access would hand back
    # a fresh mock instead of the real module the sender imports from.
    member_messages = sys.modules["db.member_messages"]

    seen_lookups = []

    def _member_templates(session, group_id, player_id):
        seen_lookups.append((group_id, player_id))
        return list(member_templates or [])

    monkeypatch.setattr(member_messages, "member_death_templates_for_group", _member_templates)
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
    service.member_lookups = seen_lookups
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
    assert args[1] == "**Alice** was bonked by Bandos in GWD"
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


# ── Members' own death messages (db/member_messages.py) ─────────────────────


@pytest.mark.asyncio
async def test_a_members_own_message_wins_over_the_groups(monkeypatch):
    raw = json.dumps(["{player_name} was bonked by {source}"])
    service, db, notification, data = _send_fixture(
        monkeypatch,
        [_row("death_message_variants", raw)],
        member_templates=["{player_name} forgot to pray against {killer}"],
    )
    await service.send_death_notification_with_session(notification, data, db)
    args, _ = service._send.await_args
    assert args[1] == "**Alice** forgot to pray against Bandos"
    # Asked about this group and this member, not anyone else.
    assert service.member_lookups == [(5, 9)]


@pytest.mark.asyncio
async def test_no_member_message_for_this_group_falls_back_to_the_groups(monkeypatch):
    # member_death_templates_for_group returns [] when the group has not
    # opted in or has blocked the member; the group's own message goes out.
    raw = json.dumps(["{player_name} was bonked by {source}"])
    service, db, notification, data = _send_fixture(
        monkeypatch, [_row("death_message_variants", raw)], member_templates=[])
    await service.send_death_notification_with_session(notification, data, db)
    args, _ = service._send.await_args
    assert args[1] == "**Alice** was bonked by Bandos"


@pytest.mark.asyncio
async def test_a_member_message_naming_an_unknown_killer_is_skipped(monkeypatch):
    raw = json.dumps(["{player_name} died in {location}"])
    service, db, notification, data = _send_fixture(
        monkeypatch,
        [_row("death_message_variants", raw)],
        member_templates=["{player_name} was slain by {killer}"],
    )
    data["source"] = ""
    await service.send_death_notification_with_session(notification, data, db)
    args, _ = service._send.await_args
    # Not "was slain by ." — the group's message still reads whole.
    assert args[1] == "**Alice** died in GWD"


@pytest.mark.asyncio
async def test_every_message_needing_a_missing_value_leaves_the_default(monkeypatch):
    raw = json.dumps(["{player_name} lost {value_lost}"])
    service, db, notification, data = _send_fixture(
        monkeypatch,
        [_row("death_message_variants", raw)],
        member_templates=["{player_name} lost {value_lost} to {killer}"],
    )
    await service.send_death_notification_with_session(notification, data, db)
    args, _ = service._send.await_args
    # No value_lost in the payload (a pre-6.0.4 plugin).
    assert args[1] == "**Alice** has died!"


@pytest.mark.asyncio
async def test_member_line_goes_inside_the_embed_when_the_group_asks(monkeypatch):
    service, db, notification, data = _send_fixture(
        monkeypatch,
        [_row("death_message_as_embed_description", "1")],
        member_templates=["{player_name} planked to {killer}"],
    )
    await service.send_death_notification_with_session(notification, data, db)
    args, kwargs = service._send.await_args
    assert args[1] == "**Alice** has died!"
    assert kwargs["embed"].description == "[Alice](https://x/9) planked to Bandos"


@pytest.mark.asyncio
async def test_client_sent_values_cannot_ping(monkeypatch):
    service, db, notification, data = _send_fixture(
        monkeypatch, [], member_templates=["{player_name} was ended by {killer}"])
    data["source"] = "@everyone <@&123> Bandos https://evil.example.com"
    await service.send_death_notification_with_session(notification, data, db)
    args, _ = service._send.await_args
    assert args[1] == "**Alice** was ended by Bandos"


@pytest.mark.asyncio
async def test_death_message_placeholder_reaches_a_components_layout(monkeypatch):
    service, db, notification, data = _send_fixture(
        monkeypatch, [], member_templates=["{player_name} walked into {killer}"])
    service._try_send_component_layout = AsyncMock(return_value=object())
    service._finish_component_send = AsyncMock()
    await service.send_death_notification_with_session(notification, data, db)
    replacements = service._try_send_component_layout.await_args.args[5]
    assert replacements["{death_message}"] == "[Alice](https://x/9) walked into Bandos"
    service._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_death_message_placeholder_defaults_to_the_bold_headline(monkeypatch):
    service, db, notification, data = _send_fixture(monkeypatch, [])
    service._try_send_component_layout = AsyncMock(return_value=object())
    service._finish_component_send = AsyncMock()
    await service.send_death_notification_with_session(notification, data, db)
    replacements = service._try_send_component_layout.await_args.args[5]
    assert replacements["{death_message}"] == "**[Alice](https://x/9)** has died!"


def _template_embed(description):
    return SimpleNamespace(
        title="Player Death", description=description, url=None, footer=None,
        fields=[], thumbnail=None, image=None,
    )


@pytest.mark.asyncio
async def test_a_custom_embed_that_places_the_line_does_not_repeat_it(monkeypatch):
    service, db, notification, data = _send_fixture(
        monkeypatch, [], member_templates=["{player_name} tripped over {killer}"])
    monkeypatch.setattr(ns, "has_custom_embeds", lambda gid: True)
    service.db_ops.get_group_embed = AsyncMock(return_value=_template_embed("{death_message}"))
    await service.send_death_notification_with_session(notification, data, db)
    args, kwargs = service._send.await_args
    assert kwargs["embed"].description == "[Alice](https://x/9) tripped over Bandos"
    # Already in the embed, so the content line stays the plain default.
    assert args[1] == "**Alice** has died!"


@pytest.mark.asyncio
async def test_a_custom_embed_without_the_placeholder_still_gets_the_content_line(monkeypatch):
    service, db, notification, data = _send_fixture(
        monkeypatch, [], member_templates=["{player_name} tripped over {killer}"])
    monkeypatch.setattr(ns, "has_custom_embeds", lambda gid: True)
    service.db_ops.get_group_embed = AsyncMock(return_value=_template_embed("{player_name} died"))
    await service.send_death_notification_with_session(notification, data, db)
    args, kwargs = service._send.await_args
    assert kwargs["embed"].description == "[Alice](https://x/9) died"
    assert args[1] == "**Alice** tripped over Bandos"


class TestMemberDeathDm:
    def _service(self):
        return ns.NotificationService(MagicMock(), MagicMock())

    def test_uses_the_members_own_message(self, monkeypatch):
        member_messages = sys.modules["db.member_messages"]

        monkeypatch.setattr(
            member_messages, "load_member_messages",
            lambda session, player_id: ["{player_name} lost {value_lost} to {killer}"],
        )
        line = self._service()._member_death_dm_line(MagicMock(), {
            "player_id": 9, "player_name": "Alice", "source": "Zulrah", "value_lost": 4200000,
        })
        assert line.startswith("**Alice** lost ")
        assert line.endswith(" to Zulrah")

    def test_nothing_usable_means_the_standard_dm(self, monkeypatch):
        member_messages = sys.modules["db.member_messages"]

        monkeypatch.setattr(
            member_messages, "load_member_messages",
            lambda session, player_id: ["{player_name} lost {value_lost}"],
        )
        # No value_lost in the payload, so the only message is skipped.
        assert self._service()._member_death_dm_line(
            MagicMock(), {"player_id": 9, "player_name": "Alice"}) == ""

    def test_player_id_zero_is_still_a_player(self, monkeypatch):
        member_messages = sys.modules["db.member_messages"]

        asked = []
        monkeypatch.setattr(
            member_messages, "load_member_messages",
            lambda session, player_id: asked.append(player_id) or [],
        )
        self._service()._member_death_dm_line(MagicMock(), {"player_id": 0, "player_name": "A"})
        assert asked == [0]
