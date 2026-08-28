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
  GET    /api/v1/api-key-reveals/{token}     - claim a one-time key link

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
import json
import os
import re
from datetime import datetime

from quart import Blueprint, jsonify, request
from sqlalchemy import func

from db import api_keys as keys
from web_api.common import abort_problem, db_session
from web_api.deps import (
    assert_developer,
    assert_group_admin,
    assert_superadmin,
    current_user_id,
    load_user,
)


def _audit(actor_user_id, action, target, before=None, after=None) -> None:
    """Mirror of routes/admin._audit — key grants belong in the audit log."""
    try:
        from db.models import AuditLog

        with db_session() as s:
            s.add(AuditLog(actor_user_id=actor_user_id, group_id=None,
                           action=action, target=target,
                           before=before, after=after))
            s.commit()
    except Exception:
        pass

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
        "scope": getattr(row, "scope", None)
                 or ("user" if row.owner_user_id is not None else "group"),
        "owner_type": getattr(row, "scope", None)
                      or ("user" if row.owner_user_id is not None else "group"),
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
                # One serializer for both endpoints. These used to differ
                # ("key"/"name" here, "tier_key"/"display_name" there), which
                # meant a client that validated the tier shape worked against
                # one endpoint and threw against the other.
                "tiers": [_tier_row(t) for t in tiers],
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
            assert_superadmin(load_user(session, user_id))
            owner_user_id = body.get("owner_user_id")
            group_id = body.get("group_id")
            # Default to whichever owner was given, so existing callers keep
            # working; 'global' has to be asked for by name.
            key_scope = body.get("scope") or (
                "user" if owner_user_id is not None else "group"
            )
            if key_scope not in keys.SCOPES:
                return None, "bad_scope"
            if key_scope == "global":
                if owner_user_id is not None or group_id is not None:
                    return None, "global_has_no_owner"
            elif (owner_user_id is None) == (group_id is None):
                return None, "one_owner_required"
            tier_key = str(body.get("tier") or keys.DEFAULT_TIER)
            if session.query(ApiKeyTier).filter(
                    ApiKeyTier.tier_key == tier_key).first() is None:
                return None, "unknown_tier"

            row, token = keys.create_key(
                session,
                owner_user_id=int(owner_user_id) if owner_user_id is not None else None,
                group_id=int(group_id) if group_id is not None else None,
                scope=key_scope,
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

            payload = _serialize(row, include_token=token)

            # Optional one-time delivery: instead of the minter copying the
            # secret out of this response and pasting it somewhere, hand the
            # recipient a link only they can open, once.
            if body.get("deliver_link"):
                from db import api_key_reveals as reveals

                audience_user = row.owner_user_id
                audience_group = row.group_id
                if audience_user is None and audience_group is None:
                    # A global key has no owner, so someone must be named.
                    audience_user = body.get("deliver_to_user_id")
                    if audience_user is None:
                        return payload, "deliver_needs_recipient"
                    audience_user = int(audience_user)

                _row, reveal_token = reveals.create_reveal(
                    session,
                    api_key_id=int(row.id),
                    plaintext=token,
                    audience_user_id=audience_user,
                    audience_group_id=None if audience_user is not None else audience_group,
                    created_by_user_id=user_id,
                )
                session.commit()
                payload["reveal_url"] = f"{_site_base()}/api-keys/claim/{reveal_token}"
                payload["reveal_dm_sent"] = _dm_reveal(
                    session, audience_user, audience_group, payload["reveal_url"],
                    row.label,
                )
            return payload, None

    payload, error = await asyncio.to_thread(work)
    if error == "one_owner_required":
        abort_problem(400, "Bad Request",
                      "Provide exactly one of owner_user_id or group_id, "
                      "or scope='global' for an all-access key.")
    if error == "bad_scope":
        abort_problem(400, "Bad Request",
                      f"scope must be one of {', '.join(keys.SCOPES)}.")
    if error == "global_has_no_owner":
        abort_problem(400, "Bad Request",
                      "A global key reads every group and player, so it has no "
                      "owner — send scope='global' with neither owner_user_id "
                      "nor group_id.")
    if error == "unknown_tier":
        abort_problem(400, "Bad Request", "No such tier.")
    if error == "deliver_needs_recipient":
        abort_problem(400, "Bad Request",
                      "A global key has no owner, so deliver_to_user_id is required "
                      "to send it as a link. The key was created; deliver it manually.")
    return jsonify(payload), 201


@api_keys_bp.route("/admin/api-keys/<int:key_id>", methods=["PATCH"])
async def admin_update_key(key_id: int):
    """Promote a key's tier, set/clear overrides, revoke or un-revoke."""
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}

    def work():
        from db.models import ApiKey, ApiKeyTier

        with db_session() as session:
            assert_superadmin(load_user(session, user_id))
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


# ── tier definitions ─────────────────────────────────────────────────────────
#
# Tiers were seeded by migration and previously could only be changed by
# another one, which made "promote a key once its usage proves itself" a
# deploy rather than an operation. They are editable here instead.
#
# A tier's limits apply to every key on it the moment they are saved: the data
# API resolves them per request and holds no cache.

def _tier_row(row) -> dict:
    return {
        "tier_key": row.tier_key,
        "display_name": row.display_name,
        "requests_per_min": int(row.requests_per_min),
        "cost_units_per_min": int(row.cost_units_per_min),
        "requests_per_day": int(row.requests_per_day),
        "max_concurrency": int(row.max_concurrency),
        "enabled": bool(row.enabled),
        "sort_order": int(row.sort_order or 0),
    }


#: Sanity bounds. Generous — the point is to stop a slipped digit granting
#: something absurd, not to second-guess a deliberate choice.
_TIER_BOUNDS = {
    "requests_per_min": (1, 100_000),
    "cost_units_per_min": (1, 100_000_000),
    "requests_per_day": (1, 100_000_000),
    "max_concurrency": (1, 256),
}

_TIER_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@api_keys_bp.route("/admin/api-key-tiers", methods=["GET"])
async def admin_list_tiers():
    user_id = current_user_id()

    def work():
        from db.models import ApiKey, ApiKeyTier

        with db_session() as session:
            assert_developer(load_user(session, user_id))
            rows = session.query(ApiKeyTier).order_by(ApiKeyTier.sort_order).all()
            # How many live keys each tier is responsible for — the number that
            # says whether editing it is routine or consequential.
            counts = dict(
                session.query(ApiKey.tier_key, func.count())
                .filter(ApiKey.revoked_at.is_(None))
                .group_by(ApiKey.tier_key).all()
            )
            return {"tiers": [dict(_tier_row(r), active_keys=int(counts.get(r.tier_key, 0)))
                              for r in rows]}

    return jsonify(await asyncio.to_thread(work))


@api_keys_bp.route("/admin/api-key-tiers/<tier_key>", methods=["PUT"])
async def admin_put_tier(tier_key: str):
    """Create or update a tier. Superadmin — this changes what keys may do."""
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}

    if not _TIER_KEY_RE.match(tier_key or ""):
        abort_problem(422, "Invalid tier key",
                      "A tier key is lowercase letters, digits and underscores, "
                      "2-32 characters, starting with a letter.")

    values = {}
    for field, (low, high) in _TIER_BOUNDS.items():
        if field not in body:
            continue
        raw = body[field]
        if isinstance(raw, bool) or not isinstance(raw, int):
            abort_problem(422, "Invalid value", f"'{field}' must be a whole number.")
        if not low <= raw <= high:
            abort_problem(422, "Out of range",
                          f"'{field}' must be between {low:,} and {high:,}.")
        values[field] = raw

    def work():
        from db.models import ApiKeyTier

        with db_session() as session:
            assert_superadmin(load_user(session, user_id))
            row = (session.query(ApiKeyTier)
                   .filter(ApiKeyTier.tier_key == tier_key).first())
            before = _tier_row(row) if row is not None else None

            if row is None:
                missing = [f for f in _TIER_BOUNDS if f not in values]
                if missing:
                    abort_problem(422, "Incomplete tier",
                                  "A new tier needs every limit: "
                                  + ", ".join(sorted(missing)) + ".")
                row = ApiKeyTier(
                    tier_key=tier_key,
                    display_name=str(body.get("display_name") or tier_key)[:64],
                    enabled=bool(body.get("enabled", True)),
                    sort_order=int(body.get("sort_order") or 0),
                    **values,
                )
                session.add(row)
            else:
                for field, value in values.items():
                    setattr(row, field, value)
                if "display_name" in body:
                    row.display_name = str(body["display_name"])[:64]
                if "enabled" in body:
                    row.enabled = bool(body["enabled"])
                if "sort_order" in body:
                    row.sort_order = int(body["sort_order"] or 0)
            session.commit()
            return before, _tier_row(row)

    before, after = await asyncio.to_thread(work)
    _audit(user_id, "api_key_tier.put", tier_key,
           before=json.dumps(before) if before else None, after=json.dumps(after))
    return jsonify(after)


@api_keys_bp.route("/admin/api-key-tiers/<tier_key>", methods=["DELETE"])
async def admin_delete_tier(tier_key: str):
    """Remove a tier — refused while any live key still sits on it.

    A key whose tier vanished falls back to the hard floor in
    ``db.api_keys.effective_limits``, which would silently throttle a consumer
    to a fraction of what they were granted. Move the keys first.
    """
    user_id = current_user_id()

    def work():
        from db.models import ApiKey, ApiKeyTier

        with db_session() as session:
            assert_superadmin(load_user(session, user_id))
            row = (session.query(ApiKeyTier)
                   .filter(ApiKeyTier.tier_key == tier_key).first())
            if row is None:
                return "missing", None
            live = (session.query(ApiKey)
                    .filter(ApiKey.tier_key == tier_key,
                            ApiKey.revoked_at.is_(None)).count())
            if live:
                return "in_use", live
            before = _tier_row(row)
            session.delete(row)
            session.commit()
            return "deleted", before

    outcome, detail = await asyncio.to_thread(work)
    if outcome == "missing":
        abort_problem(404, "Not Found", f"No tier '{tier_key}'.")
    if outcome == "in_use":
        abort_problem(409, "Tier in use",
                      f"{detail} active key(s) still use '{tier_key}'. Move them to "
                      "another tier first, or disable this one instead of deleting it.")
    _audit(user_id, "api_key_tier.delete", tier_key, before=json.dumps(detail))
    return jsonify({"ok": True})


def _site_base() -> str:
    return (os.getenv("SITE_URL") or "https://www.droptracker.io").rstrip("/")


def _dm_reveal(session, audience_user_id, audience_group_id, url: str,
               label: str) -> bool:
    """DM the recipient their link. Best-effort: the URL is returned regardless.

    Goes through the Discord outbox because the web API must never open a
    gateway connection. A group reveal DMs the group's owner, since "any
    admin" is not a person to send a message to.
    """
    try:
        from db.models import GroupAdmin, User
        from services.discord_outbox import enqueue

        target_user_id = audience_user_id
        if target_user_id is None and audience_group_id is not None:
            owner = (session.query(GroupAdmin)
                     .filter(GroupAdmin.group_id == int(audience_group_id),
                             GroupAdmin.role == "owner").first())
            target_user_id = getattr(owner, "user_id", None)
        if target_user_id is None:
            return False

        user = session.query(User).filter(User.user_id == int(target_user_id)).first()
        discord_id = getattr(user, "discord_id", None)
        if not discord_id:
            return False

        name = f" for **{label}**" if label else ""
        enqueue(
            channel_id=str(discord_id),
            kind="dm",
            content=(
                f"Your DropTracker API key{name} is ready.\n\n"
                f"{url}\n\n"
                "The link opens once, only for you while signed in, and expires in "
                "72 hours. The key itself is shown a single time — store it "
                "somewhere safe before closing the page."
            ),
        )
        return True
    except Exception:
        return False


# ── one-time delivery ────────────────────────────────────────────────────────

@api_keys_bp.route("/api-key-reveals/<reveal_token>", methods=["GET"])
async def claim_api_key_reveal(reveal_token: str):
    """Hand over a minted key, once, to the person it was meant for.

    Requires a signed-in session: the link alone is not authorisation. Every
    way of failing that could confirm a token exists — unknown, expired, or
    the wrong viewer — returns the same body, so the URL cannot be probed.
    "Already viewed" is reported honestly, because by then the holder knows
    the link was real and needs to be told it has been spent (possibly by
    someone else).
    """
    user_id = current_user_id()

    def work():
        from db import api_key_reveals as reveals

        with db_session() as session:
            return reveals.claim(session, reveal_token, user_id)

    outcome, payload = await asyncio.to_thread(work)

    if outcome == "ok":
        return jsonify({
            "token": payload["token"],
            "key_id": payload["key_id"],
            "label": payload["label"],
            "tier": payload["tier"],
            "scope": payload["scope"],
            "group_id": payload["group_id"],
            "warning": "This link is now spent. The token is not recoverable — "
                       "store it before leaving this page.",
        })

    if outcome == "already_viewed":
        abort_problem(410, "Already used",
                      "This link has already been opened. Keys are shown once, so "
                      "if you did not see it, ask staff for a new one.",
                      extra={"code": "already_viewed"})

    # unknown / expired / not your link — deliberately identical.
    abort_problem(404, "Not Found",
                  "This link is not valid, has expired, or is not yours to open.",
                  extra={"code": "unavailable"})
