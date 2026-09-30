"""Plugin settings snapshots (data/submissions/config_snapshot.py).

The JSON rides in the embed description, which both intake parsers used to
throw away, so the parsers are pinned here along with the processor.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from data.submissions import config_snapshot, dispatch
from data.submissions.config_snapshot import (
    DESCRIPTION_KEY,
    config_snapshot_processor,
    parse_snapshot,
)

SNAPSHOT = {
    "v": 1,
    "settings": {"Tracking": {"lootEmbeds": True, "petEmbeds": False},
                 "Events": {"eventDisplayMode": "POPUP"}},
    "customized": ["petEmbeds"],
    "env": {"runelite_version": "1.13.1", "resizable": False},
    "hash": "0123456789abcdef",
}


class _Row(SimpleNamespace):
    """Stands in for the model (db.models may be stubbed in this suite)."""

    def __init__(self, **kwargs):
        super().__init__(previous_config_json=None, previous_captured_at=None, **kwargs)


@pytest.fixture(autouse=True)
def _plain_model(monkeypatch):
    monkeypatch.setattr(config_snapshot, "PlayerPluginConfig", _Row)


def _session(player, row=None):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = player
    session.get.return_value = row
    return session


def _data(snapshot=SNAPSHOT, **extra):
    data = {"type": "config_snapshot", "acc_hash": "123", "player_name": "Zezima",
            "p_v": "6.0.16", "used_api": True,
            DESCRIPTION_KEY: json.dumps(snapshot)}
    data.update(extra)
    return data


def test_routed_as_a_supported_type():
    assert dispatch.is_supported("config_snapshot")
    assert dispatch.resolve_submission_type("config_snapshot", "Plugin configuration", (), ()) \
        == "config_snapshot"


def test_parse_rejects_junk_and_accepts_code_fences():
    assert parse_snapshot(None) is None
    assert parse_snapshot("not json") is None
    assert parse_snapshot(json.dumps({"no": "settings"})) is None
    assert parse_snapshot("x" * 5000) is None
    assert parse_snapshot("```json\n" + json.dumps(SNAPSHOT) + "\n```")["v"] == 1


def test_new_snapshot_creates_the_row():
    session = _session(SimpleNamespace(player_id=7))
    response = asyncio.run(config_snapshot_processor(_data(), external_session=session))
    assert response.success and response.notice is None
    row = session.add.call_args[0][0]
    assert row.player_id == 7
    assert json.loads(row.config_json)["customized"] == ["petEmbeds"]
    assert row.plugin_version == "6.0.16"
    assert row.runelite_version == "1.13.1"
    assert row.used_api is True
    assert row.previous_config_json is None


def test_changed_snapshot_keeps_the_previous_one():
    old = SimpleNamespace(config_json='{"v":1,"settings":{}}', config_hash="old",
                          captured_at="then", previous_config_json=None,
                          previous_captured_at=None)
    session = _session(SimpleNamespace(player_id=7), row=old)
    asyncio.run(config_snapshot_processor(_data(), external_session=session))
    assert old.previous_config_json == '{"v":1,"settings":{}}'
    assert old.previous_captured_at == "then"
    assert old.config_hash == "0123456789abcdef"


def test_resent_snapshot_does_not_overwrite_the_previous_one():
    old = SimpleNamespace(config_json="same", config_hash="0123456789abcdef",
                          captured_at="then", previous_config_json="older",
                          previous_captured_at="before")
    session = _session(SimpleNamespace(player_id=7), row=old)
    asyncio.run(config_snapshot_processor(_data(), external_session=session))
    assert old.previous_config_json == "older"


def test_unknown_account_is_ignored_not_created():
    session = _session(None)
    response = asyncio.run(config_snapshot_processor(_data(), external_session=session))
    assert response.success
    session.add.assert_not_called()


def test_missing_snapshot_is_rejected():
    session = _session(SimpleNamespace(player_id=7))
    data = _data()
    del data[DESCRIPTION_KEY]
    assert not asyncio.run(config_snapshot_processor(data, external_session=session)).success


def test_api_intake_keeps_the_embed_description():
    from api.routes.webhook import process_webhook_data

    items = asyncio.run(process_webhook_data({"embeds": [{
        "title": "Plugin configuration",
        "description": json.dumps(SNAPSHOT),
        "fields": [{"name": "type", "value": "config_snapshot"}],
    }]}))
    assert parse_snapshot(items[0][DESCRIPTION_KEY])["v"] == 1


def test_webhook_reader_keeps_the_embed_description():
    # Source-extracted: importing bots/webhook_bot pulls in the Discord client.
    import ast
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "..", "bots", "webhook_bot.py")
    tree = ast.parse(open(path).read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_embed_to_dict")
    scope = {"Embed": object}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), scope)
    _embed_to_dict = scope["_embed_to_dict"]

    embed = SimpleNamespace(description="{}", fields=[SimpleNamespace(name="type", value="config_snapshot")])
    assert _embed_to_dict(embed)[DESCRIPTION_KEY] == "{}"


def test_read_payload_lists_what_changed():
    from web_api.routes.plugin_config import payload

    previous = dict(SNAPSHOT, settings={"Tracking": {"lootEmbeds": True, "petEmbeds": True}})
    row = SimpleNamespace(config_json=json.dumps(SNAPSHOT), previous_config_json=json.dumps(previous),
                          captured_at=None, previous_captured_at=None, plugin_version="6.0.16",
                          runelite_version="1.13.1", used_api=False)
    body = payload(SimpleNamespace(player_id=7, player_name="Zezima"), row)
    snap = body["snapshot"]
    assert snap["transport"] == "webhook"
    assert snap["settings"]["Events"]["eventDisplayMode"] == "POPUP"
    assert {c["key"] for c in snap["changed_since_previous"]} == {"petEmbeds", "eventDisplayMode"}
    assert payload(SimpleNamespace(player_id=7, player_name="Zezima"), None)["snapshot"] is None


def test_missing_table_is_a_clean_rejection():
    from sqlalchemy.exc import ProgrammingError

    session = _session(SimpleNamespace(player_id=7))
    session.get.side_effect = ProgrammingError("SELECT", {}, Exception("no such table"))
    response = asyncio.run(config_snapshot_processor(_data(), external_session=session))
    assert not response.success
    session.rollback.assert_called_once()
