"""Tools & communities directory (/tools on the site): staff and maintainer CRUD (web146a).

The page is a staff preview for now, so even reads need a session: superadmins
see everything, and a listing's maintainers can open the page and edit their
own listings. Field rules live in web_api/community_listings.py.

  GET    /api/v1/tools/listings                          -> { listings, can_admin }
  POST   /api/v1/tools/listings                          superadmin; full listing
  PATCH  /api/v1/tools/listings/{id}                     superadmin, or maintainer (content fields)
  DELETE /api/v1/tools/listings/{id}                     superadmin
  POST   /api/v1/tools/listings/{id}/maintainers         superadmin; { user_id }
  DELETE /api/v1/tools/listings/{id}/maintainers/{uid}   superadmin

Every write is audit-logged.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from web_api.common import abort_problem, db_session, private_no_store
from web_api.community_listings import (
    SECTIONS,
    ListingError,
    can_view_directory,
    check_edit_allowed,
    parse_json_list,
    validate_listing,
    visible_to,
)
from web_api.deps import current_user_id, is_superadmin, json_body, load_user

community_listings_bp = Blueprint("v1_community_listings", __name__)


def _models():
    from db.models.community_listing import CommunityListing, CommunityListingMaintainer

    return CommunityListing, CommunityListingMaintainer


def _raise(err: ListingError):
    abort_problem(err.status, err.title, err.detail)


def _maintained_ids(s, user_id: int) -> set[int]:
    _, Maintainer = _models()
    rows = s.query(Maintainer.listing_id).filter(Maintainer.user_id == user_id).all()
    return {r[0] for r in rows}


def _serialize(s, row, *, can_edit: bool, with_maintainers: bool) -> dict:
    out = {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "section": row.section if row.section in SECTIONS else SECTIONS[-1],
        "summary": row.summary,
        "details": row.details,
        "fit_label": row.fit_label,
        "fit_note": row.fit_note,
        "tags": [t for t in parse_json_list(row.tags_json) if isinstance(t, str)],
        "links": [
            {"label": str(l.get("label")), "href": str(l.get("href"))}
            for l in parse_json_list(row.links_json)
            if isinstance(l, dict) and l.get("href")
        ],
        "logo_url": row.logo_url,
        "accent": row.accent,
        "sort_order": row.sort_order,
        "visible": bool(row.visible),
        "can_edit": can_edit,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if with_maintainers:
        from db.models.user import User

        ids = [m.user_id for m in row.maintainers]
        names = {}
        if ids:
            for uid, username in s.query(User.user_id, User.username).filter(User.user_id.in_(ids)):
                names[uid] = username
        out["maintainers"] = [
            {"user_id": uid, "username": names.get(uid) or f"user:{uid}"} for uid in ids
        ]
    return out


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


def _require_admin(s, actor):
    user = load_user(s, actor)
    if not is_superadmin(user):
        abort_problem(403, "Forbidden", "Site staff access is required.",
                      extra={"code": "staff_required"})
    return user


def _get_listing(s, listing_id: int):
    Listing, _ = _models()
    row = s.get(Listing, listing_id)
    if row is None:
        abort_problem(404, "Not found", "That listing does not exist.")
    return row


@community_listings_bp.get("/tools/listings")
async def list_listings():
    actor = current_user_id()

    def _load():
        Listing, _ = _models()
        with db_session() as s:
            admin = is_superadmin(load_user(s, actor))
            mine = _maintained_ids(s, actor)
            if not can_view_directory(is_admin=admin, maintains_any=bool(mine)):
                abort_problem(403, "Forbidden", "This page is a staff preview.",
                              extra={"code": "staff_required"})
            rows = s.query(Listing).order_by(Listing.sort_order, Listing.id).all()
            listings = []
            for row in rows:
                maintainer = row.id in mine
                if not visible_to(bool(row.visible), is_admin=admin, is_maintainer=maintainer):
                    continue
                can_edit = admin or maintainer
                listings.append(_serialize(s, row, can_edit=can_edit, with_maintainers=can_edit))
            return {"listings": listings, "can_admin": admin}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@community_listings_bp.post("/tools/listings")
async def create_listing():
    actor = current_user_id()

    def _check():
        with db_session() as s:
            _require_admin(s, actor)

    await asyncio.to_thread(_check)
    body = await json_body()
    try:
        fields = validate_listing(body, partial=False)
    except ListingError as err:
        _raise(err)

    def _create():
        from sqlalchemy.exc import IntegrityError

        Listing, _ = _models()
        with db_session() as s:
            row = Listing(created_by=actor, updated_by=actor, **fields)
            s.add(row)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                abort_problem(409, "Slug taken", "Another listing already uses that slug.")
            return _serialize(s, row, can_edit=True, with_maintainers=True)

    created = await asyncio.to_thread(_create)
    _audit(actor, "tools.listing.create", f"community_listings:{created['id']}",
           after=created["name"])
    return private_no_store(jsonify(created)), 201


@community_listings_bp.patch("/tools/listings/<int:listing_id>")
async def update_listing(listing_id: int):
    actor = current_user_id()
    body = await json_body()

    def _update():
        from sqlalchemy.exc import IntegrityError

        with db_session() as s:
            row = _get_listing(s, listing_id)
            admin = is_superadmin(load_user(s, actor))
            maintainer = listing_id in _maintained_ids(s, actor)
            try:
                check_edit_allowed(body.keys() if isinstance(body, dict) else (),
                                   is_admin=admin, is_maintainer=maintainer)
                fields = validate_listing(body, partial=True)
            except ListingError as err:
                _raise(err)
            before = row.name
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_by = actor
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                abort_problem(409, "Slug taken", "Another listing already uses that slug.")
            return before, sorted(fields), _serialize(s, row, can_edit=True, with_maintainers=True)

    before, changed, updated = await asyncio.to_thread(_update)
    _audit(actor, "tools.listing.update", f"community_listings:{listing_id}",
           before=before, after=", ".join(changed))
    return private_no_store(jsonify(updated))


@community_listings_bp.delete("/tools/listings/<int:listing_id>")
async def delete_listing(listing_id: int):
    actor = current_user_id()

    def _delete():
        with db_session() as s:
            _require_admin(s, actor)
            row = _get_listing(s, listing_id)
            name = row.name
            s.delete(row)
            s.commit()
            return name

    name = await asyncio.to_thread(_delete)
    _audit(actor, "tools.listing.delete", f"community_listings:{listing_id}", before=name)
    return private_no_store(jsonify({"ok": True}))


def _parse_user_id(raw) -> int:
    # user_id 0 is a real account and ids run negative: never truthiness-test.
    if isinstance(raw, bool):
        raw = None
    try:
        return int(raw)
    except (TypeError, ValueError):
        abort_problem(422, "Invalid user", "user_id must be a whole number.")


@community_listings_bp.post("/tools/listings/<int:listing_id>/maintainers")
async def add_maintainer(listing_id: int):
    actor = current_user_id()
    body = await json_body()
    user_id = _parse_user_id(body.get("user_id") if isinstance(body, dict) else None)

    def _add():
        _, Maintainer = _models()
        with db_session() as s:
            _require_admin(s, actor)
            row = _get_listing(s, listing_id)
            if load_user(s, user_id) is None:
                abort_problem(404, "Unknown user", "No user has that id.")
            if not any(m.user_id == user_id for m in row.maintainers):
                row.maintainers.append(Maintainer(user_id=user_id, added_by=actor))
                s.commit()
            return _serialize(s, row, can_edit=True, with_maintainers=True)

    updated = await asyncio.to_thread(_add)
    _audit(actor, "tools.listing.maintainer.add", f"community_listings:{listing_id}",
           after=str(user_id))
    return private_no_store(jsonify(updated))


@community_listings_bp.delete("/tools/listings/<int:listing_id>/maintainers/<user_id>")
async def remove_maintainer(listing_id: int, user_id: str):
    actor = current_user_id()
    uid = _parse_user_id(user_id)

    def _remove():
        with db_session() as s:
            _require_admin(s, actor)
            row = _get_listing(s, listing_id)
            for m in list(row.maintainers):
                if m.user_id == uid:
                    row.maintainers.remove(m)
            s.commit()
            return _serialize(s, row, can_edit=True, with_maintainers=True)

    updated = await asyncio.to_thread(_remove)
    _audit(actor, "tools.listing.maintainer.remove", f"community_listings:{listing_id}",
           before=str(uid))
    return private_no_store(jsonify(updated))
