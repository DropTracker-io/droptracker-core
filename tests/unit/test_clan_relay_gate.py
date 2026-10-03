"""utils/clan_relay_gate.py: clan chat is only taken in for opted-in clans."""

from utils import clan_relay_gate as gate


def _payload(*embeds):
    return {"embeds": [
        {"fields": [{"name": k, "value": v} for k, v in fields.items()]} for fields in embeds
    ]}


OPTED = frozenset({"the best clan"})


def test_group_fields_need_a_clan_name_and_a_live_feature():
    assert gate.group_relay_fields({}) == {
        "clan_chat_bridge": False, "clan_broadcast_tracking": False, "clan_chat_slug": "",
    }
    bridged = gate.group_relay_fields({
        "clan_chat_bridge_enabled": "1", "channel_id_clan_chat_bridge": "123",
        "clan_chat_name": "The_Best Clan",
    })
    assert bridged == {
        "clan_chat_bridge": True, "clan_broadcast_tracking": False,
        "clan_chat_slug": "the best clan",
    }
    # Bridge switched on without a channel is not a bridge; and a clan name
    # left behind after both features were switched off is never advertised.
    no_channel = gate.group_relay_fields({
        "clan_chat_bridge_enabled": "1", "channel_id_clan_chat_bridge": "0",
        "clan_chat_name": "The Best Clan",
    })
    assert no_channel["clan_chat_bridge"] is False and no_channel["clan_chat_slug"] == ""
    tracking = gate.group_relay_fields({"clan_broadcast_tracking": "true", "clan_chat_name": "X"})
    assert tracking == {"clan_chat_bridge": False, "clan_broadcast_tracking": True,
                        "clan_chat_slug": "x"}
    assert gate.group_relay_fields({"clan_broadcast_tracking": "1"})["clan_broadcast_tracking"] is False


def test_relay_for_a_clan_nobody_opted_in_is_dropped():
    p = _payload({"type": "clan_chat", "clan_name": "Strangers", "message": "hi"})
    assert gate.relay_payload_unwanted(p, OPTED) is True


def test_relay_for_an_opted_in_clan_passes_whatever_the_spelling():
    p = _payload({"type": "clan_broadcast", "clan_name": "the-best_clan", "message": "x"})
    assert gate.relay_payload_unwanted(p, OPTED) is False


def test_non_relay_and_mixed_payloads_are_never_touched():
    assert gate.relay_payload_unwanted(_payload({"type": "drop", "item": "x"}), OPTED) is False
    mixed = _payload({"type": "clan_chat", "clan_name": "Strangers"}, {"type": "drop"})
    assert gate.relay_payload_unwanted(mixed, OPTED) is False
    assert gate.relay_payload_unwanted({}, OPTED) is False
    assert gate.relay_payload_unwanted(None, OPTED) is False


def test_gate_fails_open_when_the_opted_in_set_is_unreadable(monkeypatch):
    monkeypatch.setattr(gate, "opted_in_clan_slugs", lambda session=None: None)
    p = _payload({"type": "clan_chat", "clan_name": "Strangers"})
    assert gate.relay_payload_unwanted(p) is False


def test_acceptor_precheck_spots_relay_payloads_only():
    from api.routes import webhook

    assert webhook._looks_like_clan_relay(_payload({"type": "clan_chat"})) is True
    assert webhook._looks_like_clan_relay(_payload({"type": "CLAN_BROADCAST"})) is True
    assert webhook._looks_like_clan_relay(_payload({"type": "drop"})) is False
    assert webhook._looks_like_clan_relay({}) is False
