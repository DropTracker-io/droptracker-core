"""Unit tests for manual-submission screenshot handling on /manual-submit.

Two regressions this pins (both shipped with the Discord ``/submit`` command,
which is the only caller that uploads a multipart ``image_file`` — the website
form uploads to B2 and passes an ``image_url`` instead):

1. The route stored ``download_image``'s *filesystem* path on ``image_url``,
   so every surface that renders that value verbatim (event submission review,
   Discord embeds, the site) requested ``droptracker.io/store/droptracker/...``
   and got a 404. It must store the public URL the file is served at, which
   ``download_image`` reports back as ``image_path``.
2. The per-type payload fields are only merged into ``processed_data`` inside
   the ``match`` block, which runs *after* the upload is saved, so the naming
   code saw nothing and wrote every image to ``.../drop/unknown/unknown_*``.
"""

import io

import pytest
from quart import Quart
from werkzeug.datastructures import FileStorage

from api.routes.webhook import (
    MANUAL_SUBMIT_KEY_HEADER,
    _image_naming_hints,
    webhook_bp,
)

PUBLIC_PREFIX = "https://www.droptracker.io/img/user-upload/"
LOCAL_PREFIX = "/store/droptracker/disc/static/assets/img/user-upload/"


class TestImageNamingHints:
    def test_drop_carries_npc_and_item(self):
        hints = _image_naming_hints("drop", {"npc_name": "Zulrah", "item_name": "Tanzanite fang"})
        assert hints == {
            "source": "Zulrah",
            "npc_name": "Zulrah",
            "item": "Tanzanite fang",
        }

    def test_collection_log_falls_back_to_npc_name(self):
        hints = _image_naming_hints("collection_log", {"npc_name": "Vorkath", "item_name": "Draconic visage"})
        assert hints == {"source": "Vorkath", "item": "Draconic visage"}

    def test_personal_best_carries_boss_time_and_team_size(self):
        hints = _image_naming_hints(
            "personal_best", {"boss_name": "Zulrah", "time_ms": 123000, "team_size": 1}
        )
        assert hints == {
            "boss_name": "Zulrah",
            "npc_name": "Zulrah",
            "team_size": 1,
            "time": 123000,
        }

    def test_combat_achievement_carries_task_and_tier(self):
        hints = _image_naming_hints("combat_achievement", {"task": "Perfect Zulrah", "tier": "master"})
        assert hints == {"task_name": "Perfect Zulrah", "task_tier": "master"}

    def test_pet_names_by_source_and_pet(self):
        hints = _image_naming_hints("pet", {"source": "Zulrah", "pet_name": "Pet snakeling"})
        assert hints == {"source": "Zulrah", "item": "Pet snakeling"}

    def test_absent_and_blank_fields_are_dropped_not_stringified(self):
        # download_image defaults on key *absence*; a present None would be
        # written into the path as the literal "None".
        assert _image_naming_hints("drop", {"npc_name": None, "item_name": ""}) == {}

    def test_unknown_type_yields_no_hints(self):
        assert _image_naming_hints("quest", {"quest_name": "Dragon Slayer"}) == {}


@pytest.fixture()
def client():
    app = Quart(__name__)
    app.register_blueprint(webhook_bp)
    return app.test_client()


@pytest.fixture()
def manual_key(monkeypatch):
    monkeypatch.setenv("MANUAL_SUBMIT_KEY", "sekrit")
    return "sekrit"


async def _post_drop_with_image(client, key, monkeypatch, download_image):
    """POST a multipart drop submission, capturing what the processor received."""
    seen = {}

    async def fake_drop_processor(processed_data, external_session=None):
        seen["processed_data"] = dict(processed_data)
        return None

    monkeypatch.setattr("api.routes.webhook.download_image", download_image)
    monkeypatch.setattr("data.submissions.drop_processor", fake_drop_processor, raising=False)

    resp = await client.post(
        "/manual-submit",
        headers={MANUAL_SUBMIT_KEY_HEADER: key},
        files={
            "image_file": FileStorage(
                io.BytesIO(b"png-bytes"), filename="proof.png", content_type="image/png"
            )
        },
        form={
            "submission_type": "drop",
            "player_name": "Test Player",
            "item_name": "Tanzanite fang",
            "npc_name": "Zulrah",
            "value": "3000000",
            "quantity": "1",
        },
    )
    return resp, seen


class TestManualSubmitImageUrl:
    async def test_stores_public_url_not_filesystem_path(self, client, manual_key, monkeypatch):
        async def fake_download_image(sub_type, player, player_wom_id, file_data, processed_data):
            processed_data["image_path"] = f"{PUBLIC_PREFIX}670784/drop/Zulrah/Zulrah_Tanzanite_fang.png"
            return f"{LOCAL_PREFIX}670784/drop/Zulrah/Zulrah_Tanzanite_fang.png"

        resp, seen = await _post_drop_with_image(client, manual_key, monkeypatch, fake_download_image)

        assert resp.status_code == 200
        image_url = seen["processed_data"]["image_url"]
        assert image_url.startswith(PUBLIC_PREFIX)
        assert not image_url.startswith(LOCAL_PREFIX)
        assert seen["processed_data"]["downloaded"] is True

    async def test_names_image_from_payload_not_unknown(self, client, manual_key, monkeypatch):
        captured = {}

        async def fake_download_image(sub_type, player, player_wom_id, file_data, processed_data):
            captured["naming"] = dict(processed_data)
            processed_data["image_path"] = f"{PUBLIC_PREFIX}1/drop/Zulrah/Zulrah_Tanzanite_fang.png"
            return "/tmp/whatever.png"

        resp, seen = await _post_drop_with_image(client, manual_key, monkeypatch, fake_download_image)

        assert resp.status_code == 200
        naming = captured["naming"]
        assert naming["source"] == "Zulrah"
        assert naming["item"] == "Tanzanite fang"
        # The hint keys are scoped to the naming copy: "source" means the NPC to
        # the clog/pet processors, so it must not leak into the drop payload.
        assert "source" not in seen["processed_data"]
        assert "image_path" not in seen["processed_data"]

    async def test_falls_back_to_returned_path_when_no_public_url(self, client, manual_key, monkeypatch):
        # Defensive: if download_image ever stops reporting image_path, the
        # submission still carries a reference rather than losing the image.
        async def fake_download_image(sub_type, player, player_wom_id, file_data, processed_data):
            return "/tmp/whatever.png"

        resp, seen = await _post_drop_with_image(client, manual_key, monkeypatch, fake_download_image)

        assert resp.status_code == 200
        assert seen["processed_data"]["image_url"] == "/tmp/whatever.png"

    async def test_image_failure_does_not_fail_the_submission(self, client, manual_key, monkeypatch):
        async def boom(sub_type, player, player_wom_id, file_data, processed_data):
            raise RuntimeError("disk full")

        resp, seen = await _post_drop_with_image(client, manual_key, monkeypatch, boom)

        assert resp.status_code == 200
        assert seen["processed_data"]["downloaded"] is False
        assert seen["processed_data"]["image_url"] is None
