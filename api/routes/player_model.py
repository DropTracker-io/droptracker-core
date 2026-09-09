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
from db.models import PersonalBestEntry, PersonalBestLoadout, Player, PlayerState

player_model_bp = Blueprint("player_model", __name__)

# Generous ceiling on the whole multipart body; the per-file cap is enforced by
# the validator, which knows what a real model looks like.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Carries the prune's protected-fingerprint set from the store thread to the
# background follow-up. Popped before the result is serialised: it is not part
# of the response, and a frozenset would not survive jsonify anyway.
_PROTECT_KEY = "_protect"


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

    # Everything a stored upload still owes — pruning the player's older
    # outfits and pre-rendering the gear image — happens after the response.
    # The client has nothing to do with either, and the personal-best
    # notification path must never wait on a multi-second screenshot.
    protect = result.pop(_PROTECT_KEY, frozenset())
    if result.get("stored") and result.get("player_id"):
        asyncio.create_task(
            _finish_in_background(result["player_id"], fingerprint, protect)
        )

    return jsonify({"accepted": True, **result}), 200


async def _finish_in_background(player_id: int, fingerprint: str,
                                protect: frozenset = frozenset()) -> None:
    """Post-upload work the client has no reason to wait for.

    The prune is a B2 LIST plus a DELETE or two per evicted outfit; run inside
    the request it was the largest single share of a stored upload's ~1s
    (2026-09-03 journal: ~74k stored uploads/day, nearly all of the slow
    ``POST /player/model`` lines). The render is a chromium screenshot.

    The two steps fail independently: a broken prune must not cost the
    picture, and a failed render must not leave the directory growing.
    """
    try:
        from services.player_model import prune_old_models

        await asyncio.to_thread(prune_old_models, player_id, protect=protect)
    except Exception as exc:
        print(f"Background model prune failed for player {player_id}: {exc}")
    try:
        from services.gear_image import render_gear_image

        await render_gear_image(player_id, fingerprint)
    except Exception as exc:
        print(f"Background gear render failed for player {player_id}: {exc}")


def _protected_fingerprints(db_session, player_id, pinned_fingerprint):
    """Outfits the prune must keep for this player, whatever their age.

    The pinned profile outfit is a promise to the player. So is every outfit
    a personal best was set in: the leaderboards render that model for as
    long as the time stands, and "what did you wear for that record" is not
    something outfit churn should be able to erase. Bounded by the player's
    personal-best rows (one per boss and team size), so the set stays small.

    Best-effort on the personal-best half: losing that protection costs at
    most an old outfit's model, while failing the upload would cost the new
    one.
    """
    keep = set()
    if pinned_fingerprint:
        keep.add(pinned_fingerprint)
    try:
        rows = (
            db_session.query(PersonalBestLoadout.model_fingerprint)
            .join(PersonalBestEntry, PersonalBestEntry.id == PersonalBestLoadout.pb_id)
            .filter(
                PersonalBestEntry.player_id == player_id,
                PersonalBestLoadout.model_fingerprint.isnot(None),
            )
            .all()
        )
        keep.update(fp for (fp,) in rows if fp)
    except Exception as exc:
        print(f"Could not load personal-best outfits for player {player_id}: {exc}")
    return frozenset(keep)


def _store(acc_hash, fingerprint, model_bytes, pet_bytes, pin=False):
    from services.player_model import (
        is_valid_fingerprint,
        model_exists,
        model_url,
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

        # The prune itself runs after the response (_finish_in_background);
        # the protected set is decided here, while the session is in hand.
        protect = _protected_fingerprints(db_session, player_id, pinned_fingerprint)
        return {"stored": True, "player_id": player_id, "url": url,
                "pinned": pin, _PROTECT_KEY: protect}
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
