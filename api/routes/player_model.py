"""``POST /player/model`` — accept the plugin's character model upload.

Stores one binary glTF per outfit fingerprint. Repeat uploads of an outfit we
already hold are answered without touching the disk, which is the common case:
the plugin only re-uploads when it cannot tell whether we have a model, and a
player's outfit changes far less often than they play.

The uploaded bytes are attacker-controlled and are later handed to a browser to
render, so they are validated structurally before anything is written — see
``services/player_model.py``.
"""
import asyncio

from quart import Blueprint, jsonify, request

from api.core import get_db_session
from db.models import Player, PlayerState

player_model_bp = Blueprint("player_model", __name__)

# Generous ceiling on the whole multipart body; the per-file cap is enforced by
# the validator, which knows what a real model looks like.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@player_model_bp.post("/player/model")
async def upload_player_model():
    if not (request.content_type or "").startswith("multipart/form-data"):
        return jsonify({"error": "multipart/form-data required"}), 415

    content_length = request.content_length or 0
    if content_length > _MAX_UPLOAD_BYTES:
        return jsonify({"error": "Upload too large"}), 413

    form = await request.form
    files = await request.files

    acc_hash = (form.get("acc_hash") or "").strip()
    fingerprint = (form.get("fingerprint") or "").strip().lower()
    if not acc_hash or not fingerprint:
        return jsonify({"error": "acc_hash and fingerprint are required"}), 422

    model_file = files.get("model")
    if model_file is None:
        return jsonify({"error": "model file is required"}), 422

    model_bytes = model_file.read()
    pet_file = files.get("pet_model")
    pet_bytes = pet_file.read() if pet_file is not None else None

    try:
        result = await asyncio.to_thread(
            _store, acc_hash, fingerprint, model_bytes, pet_bytes
        )
    except Exception as exc:
        print(f"/player/model failed: {exc}")
        return jsonify({"error": "Could not store model"}), 500

    if result is None:
        # Same shape as /state/sync: an account we do not know about is not an
        # error the client can act on, so do not make it look like one.
        return jsonify({"accepted": False, "reason": "unknown_player"}), 202
    if result is False:
        return jsonify({"accepted": False, "reason": "invalid_model"}), 422

    # Render now, in the background, so the pictures already exist by the time a
    # personal best wants one — the notification path must never wait on a
    # multi-second screenshot, and nor must a leaderboard.
    if result.get("stored") and result.get("player_id"):
        asyncio.create_task(_render_in_background(result["player_id"], fingerprint))

    return jsonify({"accepted": True, **result}), 200


async def _render_in_background(player_id: int, fingerprint: str) -> None:
    """Pre-render both pictures of this outfit: the full-body still and the
    avatar crop. Failure of either is logged and otherwise ignored.

    Sequential on purpose. Each render is a headless chromium drawing WebGL on
    the CPU; two at once on the same box costs more in contention than the
    second one saves in wall clock, and nothing is waiting on either.
    """
    from services.gear_image import render_avatar_image, render_gear_image

    for label, render in (("gear", render_gear_image), ("avatar", render_avatar_image)):
        try:
            await render(player_id, fingerprint)
        except Exception as exc:
            print(f"Background {label} render failed for player {player_id}: {exc}")


def _store(acc_hash, fingerprint, model_bytes, pet_bytes):
    from services.player_model import (
        is_valid_fingerprint,
        model_exists,
        model_url,
        prune_old_models,
        store_model,
    )

    if not is_valid_fingerprint(fingerprint):
        return False

    db_session = get_db_session()
    try:
        player = (
            db_session.query(Player).filter(Player.account_hash == acc_hash).first()
        )
        if player is None:
            return None
        player_id = player.player_id

        if model_exists(player_id, fingerprint):
            return {"stored": False, "player_id": player_id,
                    "url": model_url(player_id, fingerprint)}

        url = store_model(player_id, fingerprint, model_bytes)
        if url is None:
            return False

        if pet_bytes:
            # A bad pet model must not fail an otherwise good upload.
            store_model(player_id, fingerprint, pet_bytes, pet=True)

        # Record which outfit is current so the renderer knows what to draw
        # without listing the directory.
        state = (
            db_session.query(PlayerState)
            .filter(PlayerState.player_id == player_id)
            .first()
        )
        if state is None:
            state = PlayerState(player_id=player_id)
            db_session.add(state)
        state.model_fingerprint = fingerprint
        db_session.commit()

        pruned = prune_old_models(player_id)
        return {"stored": True, "player_id": player_id, "url": url, "pruned": pruned}
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
