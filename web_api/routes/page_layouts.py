"""Customizable public pages (web137a): group pages, player profiles, and the
default profile layout a group gives its members.

Public (anonymous, called by the Next BFF for the page render):

  GET  /api/v1/groups/{id}/page-layout           the group page layout, or null
  GET  /api/v1/players/{id}/page-layout          the profile layout in force, or null

Builder (session; group admin, or the player's own account):

  GET    /api/v1/groups/{id}/page-layout/draft        draft + published + can_publish
  PUT    /api/v1/groups/{id}/page-layout/draft        save draft (any admin, no entitlement)
  POST   /api/v1/groups/{id}/page-layout/publish      needs the custom_site entitlement
  POST   /api/v1/groups/{id}/page-layout/unpublish    back to the standard page
  DELETE /api/v1/groups/{id}/page-layout/draft        discard unpublished changes

  .../groups/{id}/member-layout/...                   same five, for members' profiles
  .../players/{id}/page-layout/...                    same five, owner only; publish
                                                      needs the custom_profile user perk

Saving a draft is free for everyone so anyone can try the builder and see the
result in the preview; publishing is the paid step. Whether a published
layout is SHOWN is decided at read time by the same entitlement (60s cache),
so a lapsed subscription falls back to the standard page without losing the
design, and resubscribing brings it straight back.

Which layout a profile shows: the player's own (if their account has
custom_profile), else the member layout of the first group they joined that
has one published and holds custom_site, else the standard profile.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional

from quart import Blueprint, jsonify

from db import AuditLog
from db.entitlements import group_has_entitlement, user_has_entitlement
from db.models import PageLayout
from utils.redis import redis_client
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
)
from web_api.page_layouts_shared import (
    MAX_LAYOUT_JSON_BYTES,
    LayoutError,
    validate_layout,
)

page_layouts_bp = Blueprint("v1_page_layouts", __name__)

GROUP_ENTITLEMENT = "custom_site"
PLAYER_ENTITLEMENT = "custom_profile"
GLOBAL_GROUP_ID = 2

_SAVES_PER_HOUR = 600  # autosave is bursty: one edit session easily does 200+
_PUBLISHES_PER_HOUR = 30


def _rate_limited(bucket: str, key_id: int, limit: int) -> bool:
    conn = getattr(redis_client, "client", None)
    if conn is None:
        return False
    try:
        key = f"web:ratelimit:layout:{bucket}:{key_id}"
        count = conn.incr(key)
        if count == 1:
            conn.expire(key, 3600)
        return count > limit
    except Exception:
        return False


def _load(s, scope: str, subject_id: int) -> Optional[PageLayout]:
    return (
        s.query(PageLayout)
        .filter(PageLayout.scope == scope, PageLayout.subject_id == subject_id)
        .first()
    )


def _parse(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _audit(s, user_id: int, group_id: Optional[int], action: str, target: str) -> None:
    s.add(AuditLog(actor_user_id=user_id, group_id=group_id, action=action, target=target))


# --- who may edit what --------------------------------------------------------


def _assert_can_edit(s, user_id: int, scope: str, subject_id: int) -> Optional[int]:
    """Abort unless the caller may edit this layout. Returns the group id the
    layout belongs to (for the audit log), or None for a player layout."""
    if scope in ("group", "member_default"):
        assert_group_admin(s, user_id, subject_id, manageable_guild_ids(user_id))
        return subject_id
    from db.models import Player

    player = s.query(Player).filter(Player.player_id == subject_id).first()
    if player is None:
        abort_problem(404, "Not found", "No such player.")
    if player.user_id != user_id and not is_superadmin(load_user(s, user_id)):
        abort_problem(
            403, "Forbidden", "Only the account that owns this player can customize its profile.",
            extra={"code": "player_owner_required"},
        )
    return None


def _can_publish(s, user_id: int, scope: str, subject_id: int) -> bool:
    if is_superadmin(load_user(s, user_id)):
        return True
    if scope in ("group", "member_default"):
        from web_api.entitlements import resolve_group_entitlements

        return bool(resolve_group_entitlements(s, subject_id).get(GROUP_ENTITLEMENT))
    return user_has_entitlement(user_id, PLAYER_ENTITLEMENT)


def _upsell(scope: str) -> str:
    if scope == "player":
        return "Publishing a custom profile is a Supporter perk. Your draft is saved."
    return "Publishing a custom page needs a Supporter plan for this group. Your draft is saved."


# --- shared builder handlers --------------------------------------------------


def _builder_state(s, user_id: int, scope: str, subject_id: int) -> dict:
    row = _load(s, scope, subject_id)
    draft = _parse(row.draft_json) if row else None
    published = _parse(row.published_json) if row else None
    return {
        "scope": scope,
        "subject_id": subject_id,
        "draft": draft,
        "published": published,
        "has_unpublished_changes": bool(row and row.draft_json != (row.published_json or "")),
        "can_publish": _can_publish(s, user_id, scope, subject_id),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "published_at": row.published_at.isoformat() if row and row.published_at else None,
    }


async def _get_draft(scope: str, subject_id: int):
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            _assert_can_edit(s, user_id, scope, subject_id)
            return _builder_state(s, user_id, scope, subject_id)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


async def _put_draft(scope: str, subject_id: int):
    user_id = current_user_id()
    body = await json_body()
    raw = body.get("layout")
    try:
        layout = validate_layout(raw, scope)
    except LayoutError as e:
        abort_problem(422, "Can't save this layout", str(e))
    encoded = json.dumps(layout, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_LAYOUT_JSON_BYTES:
        abort_problem(422, "Can't save this layout", "This page is too large. Remove some blocks or text.")
    if _rate_limited("save", user_id, _SAVES_PER_HOUR):
        abort_problem(429, "Slow down", "Too many saves in the last hour. Try again shortly.")

    def _run():
        with db_session() as s:
            _assert_can_edit(s, user_id, scope, subject_id)
            row = _load(s, scope, subject_id)
            if row is None:
                row = PageLayout(scope=scope, subject_id=subject_id, draft_json=encoded)
                s.add(row)
            else:
                row.draft_json = encoded
            row.updated_by_user_id = user_id
            row.updated_at = datetime.utcnow()
            s.commit()
            return _builder_state(s, user_id, scope, subject_id)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


async def _publish(scope: str, subject_id: int):
    user_id = current_user_id()
    if _rate_limited("publish", user_id, _PUBLISHES_PER_HOUR):
        abort_problem(429, "Slow down", "Too many publishes in the last hour. Try again shortly.")

    def _run():
        with db_session() as s:
            group_id = _assert_can_edit(s, user_id, scope, subject_id)
            if not _can_publish(s, user_id, scope, subject_id):
                abort_problem(403, "Supporter perk", _upsell(scope), extra={"code": "entitlement_required"})
            row = _load(s, scope, subject_id)
            if row is None or not row.draft_json:
                abort_problem(409, "Nothing to publish", "Save a layout first.")
            row.published_json = row.draft_json
            row.published_at = datetime.utcnow()
            _audit(s, user_id, group_id, "page_layout.publish", f"{scope}:{subject_id}")
            s.commit()
            return _builder_state(s, user_id, scope, subject_id)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


async def _unpublish(scope: str, subject_id: int):
    """Back to the standard page. Free, so a group that stops paying, or just
    changes its mind, can always get the default back. The draft is kept."""
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            group_id = _assert_can_edit(s, user_id, scope, subject_id)
            row = _load(s, scope, subject_id)
            if row is not None and row.published_json is not None:
                row.published_json = None
                row.published_at = None
                _audit(s, user_id, group_id, "page_layout.unpublish", f"{scope}:{subject_id}")
                s.commit()
            return _builder_state(s, user_id, scope, subject_id)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


async def _discard(scope: str, subject_id: int):
    """Throw away unpublished edits: the draft goes back to what's live, or the
    row goes entirely when nothing was ever published."""
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            _assert_can_edit(s, user_id, scope, subject_id)
            row = _load(s, scope, subject_id)
            if row is not None:
                if row.published_json:
                    row.draft_json = row.published_json
                else:
                    s.delete(row)
                s.commit()
            return _builder_state(s, user_id, scope, subject_id)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


# --- public reads -----------------------------------------------------------------


def _group_layout(s, group_id: int) -> Optional[dict]:
    row = _load(s, "group", group_id)
    if row is None or not row.published_json:
        return None
    if not group_has_entitlement(group_id, GROUP_ENTITLEMENT):
        return None
    return _parse(row.published_json)


def _player_layout(s, player_id: int) -> tuple[Optional[dict], Optional[str], Optional[int]]:
    """(layout, source, group_id) in force for a profile."""
    from db.models import Player, user_group_association

    player = s.query(Player).filter(Player.player_id == player_id).first()
    if player is None:
        return None, None, None
    own = _load(s, "player", player_id)
    if (
        own is not None
        and own.published_json
        and player.user_id is not None
        and user_has_entitlement(player.user_id, PLAYER_ENTITLEMENT)
    ):
        return _parse(own.published_json), "player", None

    # Groups in the order the player joined them (association id), skipping
    # the global group every player is in.
    rows = (
        s.query(user_group_association.c.group_id)
        .filter(
            user_group_association.c.player_id == player_id,
            user_group_association.c.group_id != GLOBAL_GROUP_ID,
        )
        .order_by(user_group_association.c.id.asc())
        .all()
    )
    group_ids = []
    for (gid,) in rows:
        if gid not in group_ids:
            group_ids.append(gid)
    if not group_ids:
        return None, None, None
    layouts = {
        r.subject_id: r
        for r in s.query(PageLayout).filter(
            PageLayout.scope == "member_default",
            PageLayout.subject_id.in_(group_ids),
            PageLayout.published_json.isnot(None),
        )
    }
    for gid in group_ids:
        row = layouts.get(gid)
        if row is not None and group_has_entitlement(gid, GROUP_ENTITLEMENT):
            return _parse(row.published_json), "group", gid
    return None, None, None


@page_layouts_bp.get("/groups/<int:group_id>/page-layout")
async def group_page_layout(group_id: int):
    def _run():
        with db_session() as s:
            return _group_layout(s, group_id)

    layout = await asyncio.to_thread(_run)
    return with_cache_headers(jsonify({"layout": layout}), max_age=15)


@page_layouts_bp.get("/players/<int:player_id>/page-layout")
async def player_page_layout(player_id: int):
    def _run():
        with db_session() as s:
            return _player_layout(s, player_id)

    layout, source, group_id = await asyncio.to_thread(_run)
    return with_cache_headers(
        jsonify({"layout": layout, "source": source, "group_id": group_id}), max_age=15
    )


# --- builder routes -------------------------------------------------------------
# Three scopes x five verbs, registered explicitly so each shows up by name in
# the route table and the OpenAPI file.

_SCOPE_PATHS = (
    ("group", "/groups/<int:subject_id>/page-layout"),
    ("member_default", "/groups/<int:subject_id>/member-layout"),
    ("player", "/players/<int:subject_id>/page-layout"),
)


def _register(scope: str, base: str) -> None:
    ep = scope.replace("_", "-")

    async def get_draft(subject_id: int):
        return await _get_draft(scope, subject_id)

    async def put_draft(subject_id: int):
        return await _put_draft(scope, subject_id)

    async def publish(subject_id: int):
        return await _publish(scope, subject_id)

    async def unpublish(subject_id: int):
        return await _unpublish(scope, subject_id)

    async def discard(subject_id: int):
        return await _discard(scope, subject_id)

    page_layouts_bp.add_url_rule(f"{base}/draft", f"{ep}-get-draft", get_draft, methods=["GET"])
    page_layouts_bp.add_url_rule(f"{base}/draft", f"{ep}-put-draft", put_draft, methods=["PUT"])
    page_layouts_bp.add_url_rule(f"{base}/draft", f"{ep}-discard", discard, methods=["DELETE"])
    page_layouts_bp.add_url_rule(f"{base}/publish", f"{ep}-publish", publish, methods=["POST"])
    page_layouts_bp.add_url_rule(f"{base}/unpublish", f"{ep}-unpublish", unpublish, methods=["POST"])


for _scope, _base in _SCOPE_PATHS:
    _register(_scope, _base)


# --- highlights feed (the Highlights blocks) -------------------------------------
# A typed, filterable version of the profiles' "recent submissions": notified
# drops, collection log slots and PBs, plus pets (which live in their own
# table). Public, hidden players omitted, same shape as SubmissionSchema.

HIGHLIGHT_TYPES = ("drop", "pet", "clog", "pb")
_HIGHLIGHT_MAX = 25


def _parse_types() -> tuple[str, ...]:
    from quart import request

    raw = (request.args.get("types") or "").split(",")
    picked = tuple(t for t in HIGHLIGHT_TYPES if t in raw)
    return picked or HIGHLIGHT_TYPES


def _parse_limit() -> int:
    from quart import request

    try:
        return max(1, min(_HIGHLIGHT_MAX, int(request.args.get("limit") or 8)))
    except ValueError:
        return 8


def _highlights(s, *, group_id: Optional[int], player_id: Optional[int], types, limit: int) -> list:
    from sqlalchemy import or_

    from db.models import NotifiedSubmission, Player, PlayerPet, user_group_association
    from web_api.common import hidden_player_ids, period_to_partition
    from web_api.routes.profiles import IMG_BASE, _build_submissions

    hidden = hidden_player_ids()
    out: list = []

    cols = {"drop": NotifiedSubmission.drop_id, "clog": NotifiedSubmission.clog_id, "pb": NotifiedSubmission.pb_id}
    wanted = [cols[t].isnot(None) for t in types if t in cols]
    if wanted:
        q = s.query(NotifiedSubmission).filter(or_(*wanted))
        q = q.filter(NotifiedSubmission.group_id == group_id) if group_id else q.filter(
            NotifiedSubmission.player_id == player_id
        )
        rows = [r for r in q.order_by(NotifiedSubmission.date_added.desc()).limit(limit * 2).all()
                if r.player_id not in hidden]
        ids = {r.player_id for r in rows if r.player_id}
        names = {p.player_id: p.player_name for p in s.query(Player).filter(Player.player_id.in_(ids))} if ids else {}
        out.extend(_build_submissions(rows, s, period_to_partition("month"), names))

    if "pet" in types:
        pq = s.query(PlayerPet, Player.player_name).join(Player, Player.player_id == PlayerPet.player_id)
        if group_id:
            members = s.query(user_group_association.c.player_id).filter(
                user_group_association.c.group_id == group_id
            )
            pq = pq.filter(PlayerPet.player_id.in_(members))
        else:
            pq = pq.filter(PlayerPet.player_id == player_id)
        for pet, name in pq.filter(PlayerPet.date_added.isnot(None)).order_by(PlayerPet.date_added.desc()).limit(limit):
            if pet.player_id in hidden:
                continue
            out.append({
                "id": int(pet.id),
                "type": "pet",
                "label": pet.pet_name,
                "image_url": f"{IMG_BASE}/itemdb/{pet.item_id}.png" if pet.item_id else None,
                "item_id": int(pet.item_id) if pet.item_id else None,
                "player_id": pet.player_id,
                "player_name": name,
                "ts": int(pet.date_added.timestamp()),
            })

    for o in out:
        if o.get("image_url") is None:
            o.pop("image_url", None)
    out.sort(key=lambda o: o.get("ts") or 0, reverse=True)
    return out[:limit]


@page_layouts_bp.get("/groups/<int:group_id>/highlights")
async def group_highlights(group_id: int):
    types, limit = _parse_types(), _parse_limit()

    def _run():
        with db_session() as s:
            return _highlights(s, group_id=group_id, player_id=None, types=types, limit=limit)

    items = await asyncio.to_thread(_run)
    return with_cache_headers(jsonify({"items": items}), max_age=30)


@page_layouts_bp.get("/players/<int:player_id>/highlights")
async def player_highlights(player_id: int):
    types, limit = _parse_types(), _parse_limit()

    def _run():
        from web_api.common import hidden_player_ids

        if player_id in hidden_player_ids():
            return []
        with db_session() as s:
            return _highlights(s, group_id=None, player_id=player_id, types=types, limit=limit)

    items = await asyncio.to_thread(_run)
    return with_cache_headers(jsonify({"items": items}), max_age=30)
