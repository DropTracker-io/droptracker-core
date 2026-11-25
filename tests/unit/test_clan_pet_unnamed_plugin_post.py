"""A clan-broadcast pet does not post again where the plugin already posted
"Unknown Pet" for it (ticket 544, 2026-11-25).

Since the 2026-11-18 update the plugin's PetHandler cannot read the pet's name
from the clan line (the pronoun its pattern needs arrives stripped), so it posts
"Unknown Pet" and records nothing. Once the server parsed those lines (74c5268),
the deferred broadcast found no pet row, so no plugin copy, and posted the same
pet again with its real name. The broadcast must still RECORD the pet; only its
post is skipped, and only in the groups that already got the unnamed one.
"""

import asyncio
import json
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import data.submissions.clan_broadcast as cb
from utils.clan_broadcasts import parse_broadcast


def _row(gid, **data):
    return (gid, json.dumps(data))


# ── which groups already got the unnamed plugin post ─────────────────────────

def test_unnamed_plugin_pet_post_is_found():
    rows = [_row(339, pet_name="Unknown Pet", guid="1795-hash-1")]
    assert cb._unnamed_pet_group_ids(rows) == {339}


def test_named_plugin_pet_post_does_not_count():
    rows = [_row(339, pet_name="Pet snakeling")]
    assert cb._unnamed_pet_group_ids(rows) == set()


def test_a_clan_broadcast_post_never_counts_even_if_unnamed():
    rows = [_row(339, pet_name="Unknown Pet", via_clan_broadcast=True)]
    assert cb._unnamed_pet_group_ids(rows) == set()


def test_only_the_groups_that_were_told():
    rows = [_row(339, pet_name="Unknown Pet"), _row(2, pet_name="Unknown Pet"),
            _row(14, pet_name="Beaver")]
    assert cb._unnamed_pet_group_ids(rows) == {2, 339}


def test_unreadable_rows_are_ignored():
    assert cb._unnamed_pet_group_ids([(339, "{not json"), (2, None)]) == set()


def test_lookup_failure_suppresses_nothing():
    session = MagicMock()
    session.query.side_effect = RuntimeError("db down")
    assert cb._groups_told_unnamed_pet(session, 5775288, datetime(2026, 11, 21)) == set()


# ── the pet path: records, and skips only the already-told groups ────────────

def _run_chat_pet(monkeypatch, told):
    session = MagicMock()
    # No existing pet row: the broadcast is the first record of it.
    session.query.return_value.filter.return_value.first.return_value = None

    monkeypatch.setattr(cb, "ensure_can_create", AsyncMock(return_value=True))
    monkeypatch.setattr(cb, "ensure_item_by_name",
                        AsyncMock(return_value=types.SimpleNamespace(item_id=12921)))
    monkeypatch.setattr(cb, "_notify_enabled_groups", lambda *_a: {2, 339, 400})
    monkeypatch.setattr(cb, "_images_gate_blocks", AsyncMock(return_value=False))
    monkeypatch.setattr(cb, "_groups_told_unnamed_pet", lambda *_a: set(told))
    monkeypatch.setattr(cb, "_stat", lambda *_a, **_k: None)
    created = AsyncMock()
    monkeypatch.setattr(cb, "create_notification", created)

    line = ("Kipsteel feels something weird sneaking into backpack: "
            "Pet snakeling at 38 killcount from Zulrah.")
    parsed = parse_broadcast(line)
    subject = types.SimpleNamespace(player_id=5775288, player_name="Kipsteel")
    response = asyncio.run(cb._process_chat_pet(
        session, False, parsed, subject, [2, 339, 400], {}, "cc:abc",
        datetime(2026, 11, 21, 21, 3, 56), line,
    ))
    posted = sorted(call.args[3] for call in created.await_args_list)
    added = [c.args[0] for c in session.add.call_args_list]
    return response, posted, added


def test_skips_the_post_where_unknown_pet_was_sent_but_still_records(monkeypatch):
    response, posted, added = _run_chat_pet(monkeypatch, told={2, 339})
    assert posted == [400]
    assert len(added) == 1 and added[0].item_id == 12921
    assert added[0].pet_name == "Pet snakeling"
    assert response.success is True


def test_posts_everywhere_when_no_unnamed_plugin_post(monkeypatch):
    _response, posted, added = _run_chat_pet(monkeypatch, told=set())
    assert posted == [2, 339, 400]
    assert len(added) == 1
