"""``POST /dev-sync/testers`` — the dev instance's end of the tester roster push.

Production (``workers/dev_sync.py``) seals a full snapshot of the Bug Tester
roster with the shared ``DEV_SYNC_KEY`` and posts it here whenever it changes.
Applying it makes a new tester a member of dev's Bug Testers group, gives them
the role in the dev guild and lets the dev bot DM them — within seconds of
``/bug-tester add`` in production, instead of at the next database restore.

The route only exists on a dev instance. Anywhere else, or without the key
configured, it answers 404 exactly as if it were not there.

The seal is the authentication: Fernet authenticates as well as encrypts, and
refuses tokens older than ``services.tester_roster.MAX_TOKEN_AGE_SECONDS``.
"""
import asyncio
from datetime import timedelta

from quart import Blueprint, jsonify, request
from quart_rate_limiter import rate_limit

dev_sync_bp = Blueprint("dev_sync", __name__)

#: A roster is a few kilobytes; anything near this is not one.
_MAX_BODY_BYTES = 2 * 1024 * 1024


@dev_sync_bp.post("/dev-sync/testers")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def receive_tester_roster():
    # Lazy imports: tests/conftest.py stubs `services`, and route modules are
    # imported by create_app() in every test that touches `api`.
    from services import tester_roster
    from utils.dev_guild_guard import is_dev_mode

    key = tester_roster.sync_key()
    if not is_dev_mode() or not key:
        return jsonify({"error": "Resource not found"}), 404

    raw = await request.get_data(as_text=True)
    if not raw or len(raw) > _MAX_BODY_BYTES:
        return jsonify({"error": "Expected a sealed roster in the request body"}), 400

    try:
        roster = tester_roster.unseal(raw, key)
    except tester_roster.InvalidSnapshot:
        return jsonify({"error": "The roster could not be opened"}), 403

    try:
        status, body = await asyncio.to_thread(tester_roster.apply_snapshot, roster)
    except Exception as exc:
        print(f"[dev-sync] applying the tester roster failed: {exc}")
        return jsonify({"error": "The roster could not be applied"}), 500
    return jsonify(body), status
