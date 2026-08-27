"""Data API (v2) key management.

  GET    /api/v1/me/api-keys                 - my keys
  POST   /api/v1/me/api-keys                 - mint one (supporter-gated)
  DELETE /api/v1/me/api-keys/{id}            - revoke mine
  GET    /api/v1/groups/{gid}/api-keys       - the group's keys (admin)
  POST   /api/v1/groups/{gid}/api-keys       - mint one (admin)
  DELETE /api/v1/groups/{gid}/api-keys/{id}  - revoke (admin)
  GET    /api/v1/admin/api-keys              - every key (developer)
  POST   /api/v1/admin/api-keys              - mint with custom limits (developer)
  PATCH  /api/v1/admin/api-keys/{id}         - promote a tier / set overrides
  GET    /api/v1/admin/api-usage             - who is spending what

The two self-serve mint routes are gated by ``DATA_API_SELF_SERVE_KEYS``
(default off) — see :func:`self_serve_enabled`. Staff minting and every other
route stay available, so keys can be handed out deliberately while the feature
is unannounced.

The plaintext token is returned by exactly one response — the mint — and is
not recoverable afterwards; only its SHA-256 is stored. Revocation is a
timestamp, never a delete, so a key that appears in old usage data can still
be identified.

Two policy rules live here rather than in ``db/api_keys.py``, because they are
about *who may ask*, not about what a key is:

* minting a **user** key needs the supporter entitlement (owner decision);
* every self-serve key is created on the lowest tier. Limits are earned by
  demonstrated behaviour and granted by staff, never bought — which is why the
  tier argument is accepted only on the admin route.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

from quart import Blueprint, jsonify, request

from db import api_keys as keys
from web_api.common import abort_problem, db_session
from web_api.deps import (
    assert_developer,
    assert_group_admin,
    current_user_id,
    load_user,
)

api_keys_bp = Blueprint("v1_api_keys", __name__)


def self_serve_enabled() -> bool:
    """Whether users and group admins may mint their own keys.

    Off until the Data API is announced. Staff can still mint keys for anyone
    (``POST /admin/api-keys`` and ``scripts/mint_api_key.py``), and a key made
    that way works normally — this gates only the self-serve doors, so the API
    can run in production while access to it is still handed out deliberately.

    Listing and revoking stay open regardless: someone who has been given a key
    should be able to see and kill it.
    """
    return os.getenv("DATA_API_SELF_SERVE_KEYS", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _assert_self_serve_open() -> None:
    """404 rather than 403 — an unlaunched feature should not advertise itself."""
    if not self_serve_enabled():
        abort_problem(404, "Not Found", "This endpoint is not available.")


#: Entitlement that unlocks self-serve user keys.
SUPPORTER_ENTITLEMENT_CANDIDATES = ("api_access", "supporter", "premium_profile")


def _serialize(row, include_token: str | None = None) -> dict:
    now = datetime.utcnow()
    if row.revoked_at is not None:
        state = "revoked"
    elif row.expires_at is not None and row.expires_at <= now:
        state = "expired"
    else:
        state = "active"
    payload = {
        "id": int(row.id),
        "label": row.label,
        "state": state,
        "tier": row.tier_key,
        "owner_type": "user" if row.owner_user_id is not None else "group",
        "owner_user_id": row.owner_user_id,
        "group_id": row.group_id,
        "display": f"dtk_{row.id}_{row.token_prefix}...",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "overrides": {
            field: getattr(row, field) for field in keys.LIMIT_FIELDS
            if getattr(row, field) is not None
        },
    }
    if include_token:
        payload["token"] = include_token
        payload["warning"] = ("This token is shown once and cannot be retrieved "
                              "again. Store it now.")
    return payload


def _user_may_mint(session, user_id: int) -> bool:
    """Supporter (or staff) — the gate on self-serve user keys."""
    user = load_user(session, user_id)
    if user is not None and (getattr(user, "is_superadmin", False)
                             or getattr(user, "is_developer", False)):
        return True
    try:
        from db.entitlements import resolve_user_entitlements

        resolved = resolve_user_entitlements(session, user_id) or {}
    except Exception:
        return False
    return any(bool(resolved.get(name)) for name in SUPPORTER_ENTITLEMENT_CANDIDATES)


# ── user-owned keys ──────────────────────────────────────────────────────────

@api_keys_bp.route("/me/api-keys", methods=["GET"])
async def list_my_keys():
    user_id = current_user_id()

    def work():
        from db.models import ApiKey

        with db_session() as session:
            rows = (session.query(ApiKey)
                    .filter(ApiKey.owner_user_id == user_id)
                    .order_by(ApiKey.id.desc()).all())
            return {"keys": [_serialize(r) for r in rows],
                    "may_mint": self_serve_enabled() and _user_may_mint(session, user_id)}

    return jsonify(await asyncio.to_thread(work))


@api_keys_bp.route("/me/api-keys", methods=["POST"])
async def mint_my_key():
    _assert_self_serve_open()
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}
    label = str(body.get("label") or "")[:64]

    def work():
        from db.models import ApiKey

        with db_session() as session:
            if not _user_may_mint(session, user_id):
                return None, "supporter_required"
            live = (session.query(ApiKey)
                    .filter(ApiKey.owner_user_id == user_id,
                            ApiKey.revoked_at.is_(None)).count())
            if live >= 5:
                return None, "too_many_keys"
            row, token = keys.create_key(session, owner_user_id=user_id,
                                         label=label, created_by_user_id=user_id)
            session.commit()
            return _serialize(row, include_token=token), None

    payload, error = await asyncio.to_thread(work)
    if error == "supporter_required":
        abort_problem(403, "Forbidden",
                      "A supporter subscription is required to create an API key.",
                      extra={"code": "supporter_required"})
    if error == "too_many_keys":
        abort_problem(409, "Conflict",
                      "You already have the maximum of 5 active keys. Revoke one first.",
                      extra={"code": "too_many_keys"})
    return jsonify(payload), 201


@api_keys_bp.route("/me/api-keys/<int:key_id>", methods=["DELETE"])
async def revoke_my_key(key_id: int):
    user_id = current_user_id()

    def work():
        from db.models import ApiKey

        with db_session() as session:
            row = (session.query(ApiKey)
                   .filter(ApiKey.id == key_id,
                           ApiKey.owner_user_id == user_id).first())
            if row is None:
                return False
            if row.revoked_at is None:
                row.revoked_at = datetime.utcnow()
                session.commit()
            return True

    if not await asyncio.to_thread(work):
        abort_problem(404, "Not Found", "No such key.")
    return jsonify({"ok": True})


# ── group-owned keys ─────────────────────────────────────────────────────────

@api_keys_bp.route("/groups/<int:group_id>/api-keys", methods=["GET"])
async def list_group_keys(group_id: int):
    user_id = current_user_id()

    def work():
        from db.models import ApiKey

        with db_session() as session:
            assert_group_admin(session, user_id, group_id)
            rows = (session.query(ApiKey)
                    .filter(ApiKey.group_id == group_id)
                    .order_by(ApiKey.id.desc()).all())
            return {"keys": [_serialize(r) for r in rows]}

    return jsonify(await asyncio.to_thread(work))


@api_keys_bp.route("/groups/<int:group_id>/api-keys", methods=["POST"])
async def mint_group_key(group_id: int):
    _assert_self_serve_open()
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}
    label = str(body.get("label") or "")[:64]

    def work():
        from db.models import ApiKey

        with db_session() as session:
            assert_group_admin(session, user_id, group_id)
            live = (session.query(ApiKey)
                    .filter(ApiKey.group_id == group_id,
                            ApiKey.revoked_at.is_(None)).count())
            if live >= 5:
                return None, "too_many_keys"
            row, token = keys.create_key(session, group_id=group_id, label=label,
                                         created_by_user_id=user_id)
            session.commit()
            return _serialize(row, include_token=token), None

    payload, error = await asyncio.to_thread(work)
    if error == "too_many_keys":
        abort_problem(409, "Conflict",
                      "This group already has the maximum of 5 active keys.",
                      extra={"code": "too_many_keys"})
    return jsonify(payload), 201


@api_keys_bp.route("/groups/<int:group_id>/api-keys/<int:key_id>", methods=["DELETE"])
async def revoke_group_key(group_id: int, key_id: int):
    user_id = current_user_id()

    def work():
        from db.models import ApiKey

        with db_session() as session:
            assert_group_admin(session, user_id, group_id)
            row = (session.query(ApiKey)
                   .filter(ApiKey.id == key_id, ApiKey.group_id == group_id).first())
            if row is None:
                return False
            if row.revoked_at is None:
                row.revoked_at = datetime.utcnow()
                session.commit()
            return True

    if not await asyncio.to_thread(work):
        abort_problem(404, "Not Found", "No such key.")
    return jsonify({"ok": True})


# ── staff ────────────────────────────────────────────────────────────────────

@api_keys_bp.route("/admin/api-keys", methods=["GET"])
async def admin_list_keys():
    user_id = current_user_id()

    def work():
        from db.models import ApiKey, ApiKeyTier

        with db_session() as session:
            assert_developer(load_user(session, user_id))
            rows = session.query(ApiKey).order_by(ApiKey.id.desc()).limit(500).all()
            tiers = session.query(ApiKeyTier).order_by(ApiKeyTier.sort_order).all()
            return {
                "keys": [_serialize(r) for r in rows],
                "tiers": [{
                    "key": t.tier_key, "name": t.display_name,
                    "requests_per_min": t.requests_per_min,
                    "cost_units_per_min": t.cost_units_per_min,
                    "requests_per_day": t.requests_per_day,
                    "max_concurrency": t.max_concurrency,
                    "enabled": bool(t.enabled),
                } for t in tiers],
            }

    return jsonify(await asyncio.to_thread(work))


@api_keys_bp.route("/admin/api-keys", methods=["POST"])
async def admin_mint_key():
    """Mint for anyone, on any tier, with any overrides — the ACP path."""
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}

    def work():
        from db.models import ApiKeyTier

        with db_session() as session:
            assert_developer(load_user(session, user_id))
            owner_user_id = body.get("owner_user_id")
            group_id = body.get("group_id")
            if (owner_user_id is None) == (group_id is None):
                return None, "one_owner_required"
            tier_key = str(body.get("tier") or keys.DEFAULT_TIER)
            if session.query(ApiKeyTier).filter(
                    ApiKeyTier.tier_key == tier_key).first() is None:
                return None, "unknown_tier"

            row, token = keys.create_key(
                session,
                owner_user_id=int(owner_user_id) if owner_user_id is not None else None,
                group_id=int(group_id) if group_id is not None else None,
                label=str(body.get("label") or "")[:64],
                tier_key=tier_key,
                created_by_user_id=user_id,
            )
            for field in keys.LIMIT_FIELDS:
                if body.get(field) is not None:
                    setattr(row, field, int(body[field]))
            if body.get("notes"):
                row.notes = str(body["notes"])
            session.commit()
            return _serialize(row, include_token=token), None

    payload, error = await asyncio.to_thread(work)
    if error == "one_owner_required":
        abort_problem(400, "Bad Request",
                      "Provide exactly one of owner_user_id or group_id.")
    if error == "unknown_tier":
        abort_problem(400, "Bad Request", "No such tier.")
    return jsonify(payload), 201


@api_keys_bp.route("/admin/api-keys/<int:key_id>", methods=["PATCH"])
async def admin_update_key(key_id: int):
    """Promote a key's tier, set/clear overrides, revoke or un-revoke."""
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}

    def work():
        from db.models import ApiKey, ApiKeyTier

        with db_session() as session:
            assert_developer(load_user(session, user_id))
            row = session.query(ApiKey).filter(ApiKey.id == key_id).first()
            if row is None:
                return None
            if "tier" in body:
                tier_key = str(body["tier"])
                if session.query(ApiKeyTier).filter(
                        ApiKeyTier.tier_key == tier_key).first() is None:
                    return "unknown_tier"
                row.tier_key = tier_key
            for field in keys.LIMIT_FIELDS:
                if field in body:
                    # null clears the override and returns the key to its tier.
                    setattr(row, field,
                            None if body[field] is None else int(body[field]))
            if "label" in body:
                row.label = str(body["label"])[:64]
            if "notes" in body:
                row.notes = str(body["notes"])
            if "revoked" in body:
                row.revoked_at = datetime.utcnow() if body["revoked"] else None
            session.commit()
            return _serialize(row)

    result = await asyncio.to_thread(work)
    if result is None:
        abort_problem(404, "Not Found", "No such key.")
    if result == "unknown_tier":
        abort_problem(400, "Bad Request", "No such tier.")
    return jsonify(result)


@api_keys_bp.route("/admin/api-usage", methods=["GET"])
async def admin_api_usage():
    """Per-key spend, latency and error counts over the last N hours."""
    user_id = current_user_id()
    try:
        hours = max(1, min(int(request.args.get("hours", 24)), 168))
    except ValueError:
        hours = 24

    def work():
        from data_api import usage
        from db.models import ApiKey

        with db_session() as session:
            assert_developer(load_user(session, user_id))
            window = usage.read_window(hours)
            ids = [entry["key_id"] for entry in window.get("keys", [])]
            if ids:
                rows = session.query(ApiKey).filter(ApiKey.id.in_(ids)).all()
                by_id = {int(r.id): r for r in rows}
                for entry in window["keys"]:
                    row = by_id.get(entry["key_id"])
                    if row is not None:
                        entry["label"] = row.label
                        entry["tier"] = row.tier_key
                        entry["owner_type"] = ("user" if row.owner_user_id is not None
                                               else "group")
                        entry["group_id"] = row.group_id
                        entry["owner_user_id"] = row.owner_user_id
            return window

    return jsonify(await asyncio.to_thread(work))
