"""Bearer-key authentication for every /v2 request.

Token: ``Authorization: Bearer dtk_<key_id>_<secret>``. Nothing else is
accepted — in particular no ``?api_key=`` query parameter, which would leak
credentials into access logs, browser history and Referer headers (the legacy
group export accepts it; this API deliberately does not).

Failure modes are indistinguishable on the wire: a missing row, a wrong
secret, a revoked or expired key are all the same 401 body. The reason is
kept only for logs/metrics. 401 vs 403 split: 401 = "we do not know who you
are", 403 (later phases) = "we know exactly who you are and the answer is no".
"""
import asyncio

from quart import g, jsonify, request

_UNAUTHORIZED = {"error": "unauthorized",
                 "detail": "A valid 'Authorization: Bearer dtk_...' key is required."}


def _extract_token() -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""


def _load_and_verify(key_id: int, secret: str):
    """Runs in a thread: fetch the row, verify, build the descriptor."""
    from db import api_keys as keys
    from data_api.core import SessionLocal

    session = SessionLocal()
    try:
        row, tier = keys.load_key(session, key_id)
        ok, reason = keys.verify_key(row, secret)
        if not ok:
            return None, reason
        return keys.key_descriptor(row, tier), "ok"
    finally:
        session.close()


async def authenticate_request():
    """before_request hook body: returns a response to reject, None to allow.

    On success ``g.api_key`` carries the descriptor dict
    (key_id/tier/owner/limits) for the route and the metering middleware.
    """
    from db import api_keys as keys

    parsed = keys.parse_token(_extract_token())
    if parsed is None:
        return jsonify(_UNAUTHORIZED), 401

    descriptor, _reason = await asyncio.to_thread(_load_and_verify, *parsed)
    if descriptor is None:
        return jsonify(_UNAUTHORIZED), 401

    g.api_key = descriptor
    return None
