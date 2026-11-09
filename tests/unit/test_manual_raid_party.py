"""/manual-submit refuses raid loot that doesn't say who was in the raid.

A manual submission carries no party for the server to read, so a blank split
used to score a team raid as solo (Frontier PvM, 2026-11-01: a 4-man Fractured
Archive Horn of Bran). The website, Discord and the data API all forward here,
so this is the one gate every path passes; the rule itself is
``data.submissions.manual_policy.raid_party_problem``.
"""
from __future__ import annotations

import pytest

from api.routes.webhook import MANUAL_SUBMIT_KEY_HEADER
from data.submissions.manual_policy import RAID_PARTY_REQUIRED, RAID_SOLO_CONFLICT
from tests.unit.test_manual_submit_image import client, manual_key  # noqa: F401 (fixtures)


async def _post(client, key, monkeypatch, **fields):
    seen = {}

    async def fake_drop_processor(processed_data, external_session=None):
        seen["processed_data"] = dict(processed_data)
        return None

    monkeypatch.setattr("data.submissions.drop_processor", fake_drop_processor, raising=False)
    body = {
        "submission_type": "drop",
        "player_name": "Mui Gokuu",
        "item_name": "Horn of Bran",
        "npc_name": "Fractured Archive",
        "value": 425960325,
        **fields,
    }
    resp = await client.post("/manual-submit", headers={MANUAL_SUBMIT_KEY_HEADER: key}, json=body)
    return resp, seen


@pytest.mark.asyncio
async def test_raid_drop_without_a_party_is_refused(client, manual_key, monkeypatch):
    resp, seen = await _post(client, manual_key, monkeypatch)
    assert resp.status_code == 400
    assert (await resp.get_json())["error"] == RAID_PARTY_REQUIRED
    assert "processed_data" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize("solo", [True, "true"])  # JSON, and multipart form text
async def test_solo_raid_goes_through(client, manual_key, monkeypatch, solo):
    resp, seen = await _post(client, manual_key, monkeypatch, raid_solo=solo)
    assert resp.status_code == 200
    assert seen["processed_data"]["npc_name"] == "Fractured Archive"


@pytest.mark.asyncio
async def test_split_raid_goes_through(client, manual_key, monkeypatch):
    resp, seen = await _post(client, manual_key, monkeypatch,
                             players_included="Vendusz,jishieOSRS", split_size=4)
    assert resp.status_code == 200
    assert seen["processed_data"]["players_included"] == ["Vendusz", "jishieOSRS"]


@pytest.mark.asyncio
async def test_solo_plus_split_is_refused(client, manual_key, monkeypatch):
    resp, seen = await _post(client, manual_key, monkeypatch,
                             raid_solo=True, players_included=["Vendusz"])
    assert resp.status_code == 400
    assert (await resp.get_json())["error"] == RAID_SOLO_CONFLICT


@pytest.mark.asyncio
async def test_non_raid_drop_is_untouched(client, manual_key, monkeypatch):
    resp, seen = await _post(client, manual_key, monkeypatch,
                             item_name="Tanzanite fang", npc_name="Zulrah")
    assert resp.status_code == 200
    assert seen["processed_data"]["npc_name"] == "Zulrah"
