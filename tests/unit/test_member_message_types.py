"""Member and group messages beyond deaths: collection logs, drops, PBs.

The rules are db/member_messages.py's, shared with deaths (covered in
test_member_messages.py); these pin what differs per type, and how the
senders publish the picked line ({drop_message} and friends).
"""

import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from web_api.config_registry import get_config_field

mm = importlib.import_module("db.member_messages")

for _name in ("services.contribution_notifications", "services.event_notifications"):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_member_message_types_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_member_message_types_under_test"] = ns
_spec.loader.exec_module(ns)


NEW_TYPES = ("clog", "drop", "pb")


# --- the kinds --------------------------------------------------------------

@pytest.mark.parametrize("message_type", mm.MESSAGE_TYPES)
def test_every_kind_has_its_group_settings_in_the_registry(message_type):
    kind = mm.MESSAGE_KINDS[message_type]
    assert get_config_field(kind.variants_key)["type"] == "messagelist"
    assert get_config_field(kind.as_embed_key)["type"] == "boolean"
    member_switch = get_config_field(kind.allow_key)
    assert member_switch["type"] == "boolean" and member_switch["default"] is False


@pytest.mark.parametrize("message_type", mm.MESSAGE_TYPES)
def test_group_message_lists_can_spill_into_long_value(message_type):
    routes_config = sys.modules.get("web_api.routes.config")
    if routes_config is None:
        import web_api.routes.config as routes_config
    assert mm.MESSAGE_KINDS[message_type].variants_key in routes_config.LONG_VALUE_KEYS


@pytest.mark.parametrize("message_type", mm.MESSAGE_TYPES)
def test_aliases_point_at_documented_tokens_and_samples_fill_every_token(message_type):
    kind = mm.MESSAGE_KINDS[message_type]
    documented = {doc["token"] for doc in kind.tokens}
    assert set(kind.aliases.values()) <= documented
    assert set(mm.sample_replacements(message_type)) == kind.allowed_tokens


@pytest.mark.parametrize("message_type", mm.MESSAGE_TYPES)
def test_the_example_is_a_valid_message(message_type):
    kind = mm.MESSAGE_KINDS[message_type]
    assert mm.normalize_messages([kind.example], message_type) == [kind.example]


def test_unknown_type_is_refused():
    with pytest.raises(mm.MemberMessageError):
        mm.message_kind("quest")
    with pytest.raises(mm.MemberMessageError):
        mm.store_member_messages(MagicMock(), 9, ["hi"], via=mm.VIA_WEB, message_type="quest")


# --- placeholders are per type ----------------------------------------------------

def test_a_pb_message_cannot_use_death_placeholders():
    with pytest.raises(mm.MemberMessageError, match="Unknown placeholder"):
        mm.normalize_messages(["{player_name} died to {killer}"], "pb")
    assert mm.normalize_messages(["{player_name} got {boss} to {time}"], "pb")


def test_aliases_work_so_a_group_line_can_be_copied():
    assert mm.normalize_messages(["{player_name} {npc_name} {personal_best}"], "pb")
    assert mm.normalize_messages(["{player_name} {npc_name} {kc_received}"], "clog")


def test_stored_rows_are_judged_by_their_own_type():
    raw = json.dumps(["{player_name} got {item_name}", "{player_name} died to {killer}"])
    assert mm.parse_stored_messages(raw, "drop") == ["{player_name} got {item_name}"]


def test_a_line_naming_a_missing_detail_is_skipped():
    values = {"{player_name}": "Zezima", "{item_name}": "Twisted bow", "{kill_count}": ""}
    assert not mm.is_renderable("{player_name} got it at {kill_count} kc", values, "drop")
    assert mm.is_renderable("{player_name} got {item_name}", values, "drop")
    picked = mm.pick_template(
        ["{player_name} got it at {kill_count} kc", "{player_name} got {item_name}"],
        values, message_type="drop")
    assert picked == "{player_name} got {item_name}"


def test_member_render_drops_tokens_of_other_types_and_cleans_client_values():
    values = {"{player_name}": "**Zezima**", "{item_name}": "<@123> Twisted bow",
              "{video_url}": "https://evil"}
    text = mm.render_template("{player_name} got {item_name} {video_url}", values,
                              member=True, message_type="drop")
    assert text == "**Zezima** got Twisted bow"


def test_member_templates_check_the_types_own_switch(monkeypatch):
    seen = []
    monkeypatch.setattr(mm, "group_allows_member_messages",
                        lambda s, g, t="death": seen.append(t) or t == "clog")
    monkeypatch.setattr(mm, "is_member_blocked", lambda s, g, p: False)
    monkeypatch.setattr(mm, "load_member_messages", lambda s, p, t="death": [f"{t} line"])
    assert mm.member_templates_for_group(MagicMock(), 5, 9, "clog") == ["clog line"]
    assert mm.member_templates_for_group(MagicMock(), 5, 9, "drop") == []
    assert seen == ["clog", "drop"]


def test_limits_payload_describes_the_type_and_lists_all_types():
    payload = mm.limits_payload("pb")
    assert payload["type"] == "pb"
    assert [doc["token"] for doc in payload["tokens"]][:2] == ["{player_name}", "{boss}"]
    assert [t["type"] for t in payload["types"]] == list(mm.MESSAGE_TYPES)
    assert payload["aliases"] == {"{npc_name}": "{boss}", "{personal_best}": "{time}"}


# --- the senders --------------------------------------------------------------------

def _service(monkeypatch, *, variants=None, as_embed=False, member=None):
    service = ns.NotificationService.__new__(ns.NotificationService)
    monkeypatch.setattr(service, "_message_config",
                        lambda db, gid, t: (list(variants or []), as_embed), raising=False)
    monkeypatch.setattr(mm, "member_templates_for_group",
                        lambda s, g, p, t="death": list(member or []))
    return service


def _drop_values():
    return {"{player_name}": "[Zezima](https://x/9)", "{item_name}": "Twisted bow",
            "{item_emoji}": "", "{item_value}": "`1.2B`", "{npc_name}": "Great Olm"}


def test_a_members_drop_message_becomes_the_token_and_the_content_line(monkeypatch):
    service = _service(monkeypatch, variants=["{player_name} got a {item_name}"],
                       member=["{player_name} made {value} off {source}"])
    values = _drop_values()
    embed_line, content_line, as_embed = service._custom_line_values(
        MagicMock(), "drop", 5, 9, values, {"{source}": "Great Olm", "{value}": "1.2B"},
        "**Zezima**", "")
    assert embed_line == "[Zezima](https://x/9) made 1.2B off Great Olm"
    assert content_line == "**Zezima** made 1.2B off Great Olm"
    assert values["{drop_message}"] == embed_line
    assert as_embed is False


def test_with_no_custom_message_the_token_is_the_built_in_sentence(monkeypatch):
    service = _service(monkeypatch)
    values = _drop_values()
    embed_line, content_line, _ = service._custom_line_values(
        MagicMock(), "drop", 5, 9, values, {}, "**Zezima**", "")
    assert embed_line == content_line == ""
    assert values["{drop_message}"] == (
        "**[Zezima](https://x/9)** received **Twisted bow** (`1.2B`) from **Great Olm**")


def test_a_group_line_is_used_when_the_member_has_none(monkeypatch):
    service = _service(monkeypatch, variants=["{player_name} logged {item_name}"])
    values = {"{player_name}": "[Zezima](https://x/9)", "{item_name}": "Tanzanite fang"}
    _, content_line, _ = service._custom_line_values(
        MagicMock(), "clog", 5, 9, values, {}, "**Zezima**", "")
    assert content_line == "**Zezima** logged Tanzanite fang"


def test_a_failure_reads_as_no_custom_message(monkeypatch):
    service = ns.NotificationService.__new__(ns.NotificationService)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "_message_config", _boom, raising=False)
    assert service._custom_lines(MagicMock(), "pb", 5, 9, {}, {}) == ("", "", False)


def test_the_line_replaces_the_description_when_asked():
    embed = SimpleNamespace(description="old")
    assert ns.NotificationService._place_custom_line(embed, None, "{pb_message}", "line", True)
    assert embed.description == "line"


def test_a_custom_embed_placing_the_token_keeps_it_out_of_the_text():
    template = SimpleNamespace(title="{pb_message}", description="", footer=None, fields=[])
    embed = SimpleNamespace(description="d")
    assert ns.NotificationService._place_custom_line(embed, template, "{pb_message}", "line", False)
    assert embed.description == "d"
    assert not ns.NotificationService._place_custom_line(embed, None, "{pb_message}", "", True)


def test_message_text_uses_the_line_unless_the_group_wrote_its_own(monkeypatch):
    service = ns.NotificationService.__new__(ns.NotificationService)
    nt = sys.modules["services.notification_text"]
    monkeypatch.setattr(nt, "own_content_template", lambda *a, **k: None)
    monkeypatch.setattr(nt, "content_template", lambda *a, **k: None)
    assert service._message_text(5, "pb", {}, "Zezima", custom_embeds=False,
                                 line="Zezima shaved Vorkath") == "Zezima shaved Vorkath"
    # Already in the embed: the text stays the built-in line.
    assert service._message_text(5, "pb", {}, "Zezima", custom_embeds=False,
                                 line="x", line_in_embed=True) == (
        "Zezima has achieved a new personal best:")
    monkeypatch.setattr(nt, "own_content_template", lambda *a, **k: "{player_mention} pb!")
    assert service._message_text(5, "pb", {}, "Zezima", custom_embeds=True,
                                 line="x") == "Zezima pb!"
