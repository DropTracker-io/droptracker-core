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

    # The plugin's "Send Player Model" button: the player chose this outfit
    # for their profile, so record it as pinned rather than merely current.
    pin = (form.get("pin") or "").strip() in ("1", "true")

    model_file = files.get("model")
    if model_file is None:
        return jsonify({"error": "model file is required"}), 422

    model_bytes = model_file.read()
    pet_file = files.get("pet_model")
    pet_bytes = pet_file.read() if pet_file is not None else None

    try:
        result = await asyncio.to_thread(
            _store, acc_hash, fingerprint, model_bytes, pet_bytes, pin
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

    # Render now, in the background, so the picture already exists by the time a
    # personal best wants it — the notification path must never wait on a
    # multi-second screenshot.
    if result.get("stored") and result.get("player_id"):
        asyncio.create_task(_render_in_background(result["player_id"], fingerprint))

    return jsonify({"accepted": True, **result}), 200


async def _render_in_background(player_id: int, fingerprint: str) -> None:
    """Pre-render the gear image. Failure is logged and otherwise ignored."""
    try:
        from services.gear_image import render_gear_image

        await render_gear_image(player_id, fingerprint)
    except Exception as exc:
        print(f"Background gear render failed for player {player_id}: {exc}")


def _store(acc_hash, fingerprint, model_bytes, pet_bytes, pin=False):
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

        state = (
            db_session.query(PlayerState)
            .filter(PlayerState.player_id == player_id)
            .first()
        )

        if model_exists(player_id, fingerprint):
            # An outfit we already hold is a no-op for the disk — but a pin of
            # it is still news: the player just chose it for their profile.
            if pin:
                if state is None:
                    state = PlayerState(player_id=player_id)
                    db_session.add(state)
                state.model_fingerprint = fingerprint
                state.pinned_model_fingerprint = fingerprint
                db_session.commit()
            return {"stored": False, "player_id": player_id, "pinned": pin,
                    "url": model_url(player_id, fingerprint)}

        url = store_model(player_id, fingerprint, model_bytes)
        if url is None:
            return False

        if pet_bytes:
            # A bad pet model must not fail an otherwise good upload.
            store_model(player_id, fingerprint, pet_bytes, pet=True)

        # Record which outfit is current so the renderer knows what to draw
        # without listing the directory.
        if state is None:
            state = PlayerState(player_id=player_id)
            db_session.add(state)
        state.model_fingerprint = fingerprint
        if pin:
            state.pinned_model_fingerprint = fingerprint
        pinned_fingerprint = state.pinned_model_fingerprint
        db_session.commit()

        # The pinned outfit is a promise to the player; age must not delete it.
        protect = frozenset({pinned_fingerprint} if pinned_fingerprint else ())
        pruned = prune_old_models(player_id, protect=protect)
        return {"stored": True, "player_id": player_id, "url": url,
                "pinned": pin, "pruned": pruned}
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
