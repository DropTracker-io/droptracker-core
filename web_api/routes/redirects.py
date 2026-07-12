"""Admin-configurable URL redirects — superadmin CMS + middleware read path.

The front-end's Next.js middleware resolves redirects at request time, so an
admin can add/edit/remove one from ``/admin/redirects`` without a code deploy.
Same DB-table-as-CMS shape as ``docs.py``; every write is audit-logged.

Middleware read (cached, enabled only, minimal fields):
  GET /api/v1/redirects              -> RedirectRule[]

Admin (superadmin only):
  GET    /api/v1/admin/redirects           -> Redirect[] (full)
  POST   /api/v1/admin/redirects           { source, destination, ... } -> Redirect
  PATCH  /api/v1/admin/redirects/{id}      partial                       -> Redirect
  DELETE /api/v1/admin/redirects/{id}                                    -> { ok }

``source`` is a path-to-regexp pattern (same syntax as the static map in the
front-end's ``next.config.ts``). ``destination`` is an internal path (``/docs``)
or an absolute ``http(s)://`` URL. See db/models/web.py::SiteRedirect.
"""
from __future__ import annotations

import asyncio
import re

from quart import Blueprint, jsonify

from db import SiteRedirect
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user

redirects_bp = Blueprint("v1_redirects", __name__)

_EXTERNAL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _rule(r: SiteRedirect) -> dict:
    """Minimal shape consumed by the front-end middleware (enabled entries)."""
    return {
        "source": r.source,
        "destination": r.destination,
        "permanent": bool(r.permanent),
        "order": r.order,
        "forward_query": bool(r.forward_query),
    }


def _full(r: SiteRedirect) -> dict:
    """Full shape for the admin list/editor."""
    return {
        "id": r.id,
        "source": r.source,
        "destination": r.destination,
        "permanent": bool(r.permanent),
        "enabled": bool(r.enabled),
        "order": r.order,
        "forward_query": bool(r.forward_query),
        "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _validate(body: dict, *, require_core: bool) -> dict:
    """Coerce + validate an admin payload. ``require_core`` forces source and
    destination to be present (create); otherwise only supplied keys are read
    (patch)."""
    out: dict = {}

    if require_core or "source" in body:
        source = str(body.get("source") or "").strip()
        if not source.startswith("/"):
            abort_problem(422, "Invalid source", "Source must be a path starting with '/'.")
        if len(source) > 512:
            abort_problem(422, "Invalid source", "Source must be 512 characters or fewer.")
        out["source"] = source

    if require_core or "destination" in body:
        destination = str(body.get("destination") or "").strip()
        if not destination:
            abort_problem(422, "Invalid destination", "Destination is required.")
        if not (destination.startswith("/") or _EXTERNAL_RE.match(destination)):
            abort_problem(
                422,
                "Invalid destination",
                "Destination must be an internal path ('/…') or an absolute http(s):// URL.",
            )
        if len(destination) > 1024:
            abort_problem(422, "Invalid destination", "Destination must be 1024 characters or fewer.")
        out["destination"] = destination

    # Self-loop guard: reject a rule that points a path at itself.
    src = out.get("source")
    dst = out.get("destination")
    if src is not None and dst is not None and src == dst:
        abort_problem(422, "Redirect loop", "Source and destination must differ.")

    if "permanent" in body:
        out["permanent"] = _as_bool(body["permanent"], False)
    if "enabled" in body:
        out["enabled"] = _as_bool(body["enabled"], True)
    if "forward_query" in body:
        out["forward_query"] = _as_bool(body["forward_query"], True)
    if "order" in body:
        try:
            out["order"] = int(body["order"])
        except (TypeError, ValueError):
            abort_problem(422, "Invalid order", "'order' must be an integer.")
    if "note" in body:
        note = (body.get("note") or "").strip()
        out["note"] = note[:255] or None

    return out


@redirects_bp.get("/redirects")
async def list_rules():
    """Enabled redirects for the front-end middleware. Public/internal, cached."""

    def _load():
        with db_session() as s:
            rows = (
                s.query(SiteRedirect)
                .filter(SiteRedirect.enabled.is_(True))
                .order_by(SiteRedirect.order.asc(), SiteRedirect.id.asc())
                .all()
            )
            return [_rule(r) for r in rows]

    items = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(items), max_age=60)


@redirects_bp.get("/admin/redirects")
async def admin_list():
    actor = current_user_id()

    def _load():
        with db_session() as s:
            assert_superadmin(load_user(s, actor))
            rows = (
                s.query(SiteRedirect)
                .order_by(SiteRedirect.order.asc(), SiteRedirect.id.asc())
                .all()
            )
            return [_full(r) for r in rows]

    items = await asyncio.to_thread(_load)
    return private_no_store(jsonify(items))


@redirects_bp.post("/admin/redirects")
async def admin_create():
    actor = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, actor))

    await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate(body, require_core=True)
    fields.setdefault("permanent", False)
    fields.setdefault("enabled", True)
    fields.setdefault("forward_query", True)
    fields.setdefault("order", 100)

    def _create():
        with db_session() as s:
            if s.query(SiteRedirect).filter(SiteRedirect.source == fields["source"]).first():
                abort_problem(409, "Source taken", f"A redirect for '{fields['source']}' already exists.")
            r = SiteRedirect(author_user_id=actor, **fields)
            s.add(r)
            s.commit()
            return _full(r)

    payload = await asyncio.to_thread(_create)
    _audit(actor, "redirect.create", f"site_redirects:{fields['source']}", after=fields["destination"])
    return private_no_store(jsonify(payload))


@redirects_bp.patch("/admin/redirects/<int:redirect_id>")
async def admin_update(redirect_id: int):
    actor = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, actor))

    await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate(body, require_core=False)
    if not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")

    def _apply():
        with db_session() as s:
            r = s.query(SiteRedirect).filter(SiteRedirect.id == redirect_id).first()
            if not r:
                abort_problem(404, "Redirect not found", f"No redirect #{redirect_id}.")
            # Re-check the self-loop guard against the merged (existing + patch) row.
            new_source = fields.get("source", r.source)
            new_dest = fields.get("destination", r.destination)
            if new_source == new_dest:
                abort_problem(422, "Redirect loop", "Source and destination must differ.")
            if "source" in fields and fields["source"] != r.source:
                if s.query(SiteRedirect).filter(SiteRedirect.source == fields["source"]).first():
                    abort_problem(409, "Source taken", f"A redirect for '{fields['source']}' already exists.")
            for k, v in fields.items():
                setattr(r, k, v)
            s.commit()
            return _full(r)

    payload = await asyncio.to_thread(_apply)
    _audit(actor, "redirect.update", f"site_redirects:{redirect_id}", after=str(list(fields.keys())))
    return private_no_store(jsonify(payload))


@redirects_bp.delete("/admin/redirects/<int:redirect_id>")
async def admin_delete(redirect_id: int):
    actor = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, actor))

    await asyncio.to_thread(_check)

    def _delete():
        with db_session() as s:
            r = s.query(SiteRedirect).filter(SiteRedirect.id == redirect_id).first()
            if not r:
                abort_problem(404, "Redirect not found", f"No redirect #{redirect_id}.")
            source = r.source
            s.delete(r)
            s.commit()
            return source

    source = await asyncio.to_thread(_delete)
    _audit(actor, "redirect.delete", f"site_redirects:{redirect_id}", before=source)
    return jsonify({"ok": True})


def _audit(actor_user_id, action, target, before=None, after=None):
    try:
        from db import AuditLog

        with db_session() as s:
            s.add(AuditLog(
                actor_user_id=actor_user_id, group_id=None, action=action,
                target=target, before=before, after=after,
            ))
            s.commit()
    except Exception:
        pass
