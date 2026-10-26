"""A group's notification style: plain text (no embed) and member pings.

The rules live in services/notification_text.py (pure, plus two config
reads); NotificationService._send applies them to every queue row, so the
senders never need to know. Loaded from the file path like
test_notification_send_retry.py.
"""

import asyncio
import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from web_api.config_registry import get_config_field

for _name in ("services.contribution_notifications", "services.event_notifications"):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

nt = sys.modules["services.notification_text"]

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_notification_style_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_notification_style_under_test"] = ns
_spec.loader.exec_module(ns)


# --- registry ---------------------------------------------------------------

def test_registry_options_match_the_send_side():
    field = get_config_field("plain_text_notification_types")
    assert [(o["value"], o["label"]) for o in field["options"]] == list(nt.PLAIN_TEXT_OPTIONS)


def test_every_option_covers_at_least_one_queue_type():
    covered = set(nt.PLAIN_TEXT_OPTION_FOR_TYPE.values())
    assert {value for value, _ in nt.PLAIN_TEXT_OPTIONS} == covered


def test_style_settings_are_free_and_default_to_todays_behaviour():
    assert get_config_field("notification_pings")["default"] is True
    assert get_config_field("notifications_plain_text")["default"] is False
    for key in ("notification_pings", "notifications_plain_text", "plain_text_notification_types"):
        assert not get_config_field(key).get("entitlement")


# --- which types go out as plain text ----------------------------------------

def test_the_all_switch_covers_every_member_type_and_nothing_else():
    for queue_type in nt.PLAIN_TEXT_OPTION_FOR_TYPE:
        assert nt.plain_text_applies(True, frozenset(), queue_type)
    for queue_type in ("event_task_progress", "dm_drop", "group_upgrade", "points_earned", ""):
        assert not nt.plain_text_applies(True, frozenset(), queue_type)


def test_chosen_types_only():
    chosen = nt.parse_plain_text_types("clog,pb")
    assert nt.plain_text_applies(False, chosen, "clog")
    assert nt.plain_text_applies(False, chosen, "pb")
    assert not nt.plain_text_applies(False, chosen, "drop")


def test_one_option_covers_its_related_queue_types():
    chosen = nt.parse_plain_text_types("level_up,milestone,pvp")
    for queue_type in ("level_up", "xp_milestone", "total_level_milestone",
                       "kc_milestone", "rank_milestone", "pvp_kill", "pvp_death"):
        assert nt.plain_text_applies(False, chosen, queue_type)


def test_unknown_stored_options_are_ignored():
    assert nt.parse_plain_text_types("drop, bogus,,pb ") == frozenset({"drop", "pb"})
    assert nt.parse_plain_text_types(None) == frozenset()


def test_no_group_never_reads_config():
    assert nt.is_plain_text(None, "drop") is False
    assert nt.member_pings_enabled(None) is True


# --- the built-in plain line ---------------------------------------------------

def test_plain_drop_line_says_what_happened():
    values = {"{item_name}": "Twisted bow", "{item_emoji}": "", "{item_value}": "`1.2B`",
              "{npc_name}": "Great Olm"}
    text = nt.plain_content("drop", values, "**Zezima**")
    assert text == "**Zezima** received **Twisted bow** (`1.2B`) from **Great Olm**"


def test_plain_line_falls_back_when_a_value_is_missing():
    values = {"{item_name}": "Twisted bow", "{item_value}": "`1.2B`", "{npc_name}": None}
    text = nt.plain_content("drop", values, "Zezima")
    assert text.startswith("Zezima received") and "from" not in text and "{" not in text


def test_plain_line_ends_in_the_embed_mode_line():
    assert nt.plain_content("death", {}, "Zezima") == "Zezima has died!"


def test_every_content_type_has_a_plain_line():
    assert set(nt.PLAIN_CONTENT) == set(nt.DEFAULT_CONTENT)


# --- the send path ---------------------------------------------------------------

class _RecordingChannel:
    def __init__(self, channel_id=10):
        self.id = channel_id
        self.calls = []

    async def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(id=None)


class _Scope(ns._SendScope):
    def __init__(self, plain_text, member_pings):
        super().__init__(1, group_id=5, notification_type="drop")
        self._style = (plain_text, member_pings)


@pytest.fixture
def service():
    return ns.NotificationService.__new__(ns.NotificationService)


def _send(service, scope, *args, **kwargs):
    channel = _RecordingChannel()
    token = ns._send_scope.set(scope)
    try:
        asyncio.run(service._send(channel, *args, **kwargs))
    finally:
        ns._send_scope.reset(token)
    return channel.calls[0]


def test_plain_text_drops_the_embed_and_keeps_the_screenshot(service):
    args, kwargs = _send(service, _Scope(True, True), "line", embed="E", files="shot.png")
    assert args == ("line",)
    assert "embed" not in kwargs and kwargs["files"] == "shot.png"


def test_plain_text_keeps_the_embed_when_nothing_else_would_be_posted(service):
    args, kwargs = _send(service, _Scope(True, True), None, embed="E")
    assert kwargs["embed"] == "E"


def test_embed_mode_is_untouched(service):
    _, kwargs = _send(service, _Scope(False, True), "line", embed="E")
    assert kwargs["embed"] == "E"


def test_pings_off_still_lets_roles_ping(service, monkeypatch):
    seen = []
    monkeypatch.setattr(ns.interactions, "AllowedMentions",
                        lambda **kw: seen.append(kw) or kw, raising=False)
    _, kwargs = _send(service, _Scope(False, False), "<@1> got loot <@&2>", embed="E")
    assert kwargs["allowed_mentions"] == {"parse": ["roles"]}


def test_pings_on_lets_users_and_roles_ping(service, monkeypatch):
    monkeypatch.setattr(ns.interactions, "AllowedMentions", lambda **kw: kw, raising=False)
    _, kwargs = _send(service, _Scope(False, True), "<@1> got loot", embed="E")
    assert kwargs["allowed_mentions"] == {"parse": ["users", "roles"]}


def test_pings_off_trims_a_senders_own_mentions(service, monkeypatch):
    monkeypatch.setattr(ns.interactions, "AllowedMentions", lambda **kw: SimpleNamespace(**kw),
                        raising=False)
    own = SimpleNamespace(parse=["users"], roles=["2"])
    _, kwargs = _send(service, _Scope(False, False), "x", allowed_mentions=own)
    assert kwargs["allowed_mentions"].parse == [] and kwargs["allowed_mentions"].roles == ["2"]


def test_components_get_the_same_mention_rule(service, monkeypatch):
    monkeypatch.setattr(ns.interactions, "AllowedMentions", lambda **kw: kw, raising=False)
    _, kwargs = _send(service, _Scope(False, False), components=["c"])
    assert kwargs["allowed_mentions"] == {"parse": ["roles"]}


# --- the message line in plain-text mode ------------------------------------------

def test_message_text_prefers_the_picked_custom_line(service, monkeypatch):
    monkeypatch.setattr(nt, "own_content_template", lambda *a, **k: None)
    token = ns._send_scope.set(_Scope(True, True))
    try:
        text = service._message_text(5, "death", {}, "Zezima", custom_embeds=False,
                                     line="Zezima forgot to pray")
        fallback = service._message_text(5, "death", {"{killer}": "Vorkath"}, "Zezima",
                                         custom_embeds=False)
    finally:
        ns._send_scope.reset(token)
    assert text == "Zezima forgot to pray"
    assert fallback == "Zezima was killed by **Vorkath**"


def test_message_text_in_plain_mode_still_uses_the_groups_own_text(service, monkeypatch):
    monkeypatch.setattr(nt, "own_content_template", lambda *a, **k: "{player_mention} gz")
    token = ns._send_scope.set(_Scope(True, True))
    try:
        text = service._message_text(5, "drop", {}, "Zezima", custom_embeds=True, line="x")
    finally:
        ns._send_scope.reset(token)
    assert text == "Zezima gz"
