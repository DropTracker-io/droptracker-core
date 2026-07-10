"""Unit tests for web_api/mentions.py — Discord token resolution.

Covers the id-collection and plain-text cleaning used to turn raw Discord
message markup (``<@123>``, ``<@&…>``, ``<#…>``, ``<:emoji:…>``, ``<t:…>``)
into readable web content. ``db.models`` is stubbed by conftest, so ``User``
is a MagicMock and the DB query is mocked out.
"""
from unittest.mock import MagicMock

from web_api.mentions import clean_tokens, collect_user_ids, resolve_user_mentions


def test_collect_user_ids_handles_both_forms_and_dedupes():
    ids = collect_user_ids(["a <@1> b", "<@!2> and <@1> again", None, ""])
    assert ids == {"1", "2"}


def test_collect_user_ids_ignores_role_channel_emoji():
    # <@&…> (role), <#…> (channel) and <:name:…> (emoji) are NOT user mentions.
    assert collect_user_ids(["<@&3> <#4> <:pepe:5>"]) == set()


def test_clean_tokens_resolves_known_and_falls_back_to_unknown():
    m = {"356841943936425984": "joelhalen"}
    assert clean_tokens("hey <@356841943936425984>!", m) == "hey @joelhalen!"
    assert clean_tokens("nick <@!356841943936425984>", m) == "nick @joelhalen"
    assert clean_tokens("who is <@999>?", m) == "who is @unknown?"


def test_clean_tokens_flattens_all_entity_kinds():
    out = clean_tokens("role <@&1> chan <#2> emo <:pepe:3> <a:spin:4> at <t:1700000000:R>", {})
    assert out == "role @role chan #channel emo :pepe: :spin: at "


def test_clean_tokens_none_is_empty_string():
    assert clean_tokens(None, {}) == ""


def test_resolve_user_mentions_batches_and_maps():
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = [
        ("111", "alice"),
        ("222", "bob"),
    ]
    result = resolve_user_mentions(session, ["<@111> and <@!222>"])
    assert result == {"111": "alice", "222": "bob"}


def test_resolve_user_mentions_skips_query_when_no_mentions():
    session = MagicMock()
    assert resolve_user_mentions(session, ["no mentions here", None]) == {}
    session.query.assert_not_called()


def test_resolve_user_mentions_drops_rows_without_a_username():
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = [
        ("111", "alice"),
        ("222", None),
        (None, "ghost"),
    ]
    assert resolve_user_mentions(session, ["<@111> <@222> <@333>"]) == {"111": "alice"}
