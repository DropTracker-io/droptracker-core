"""Docs CMS (backend Task 15/16) — user-editable documentation pages.

Public reads (cached):
  GET /api/v1/docs             -> DocSummary[] (sorted by category, then order)
  GET /api/v1/docs/{slug}      -> Doc (full body_md)

Writes (superadmin only):
  POST   /api/v1/admin/docs             { slug, title, ... }  -> { id }
  PATCH  /api/v1/admin/docs/{slug}      partial                -> Doc
  DELETE /api/v1/admin/docs/{slug}                             -> { ok }

Replaces the old build-time static `.mdx` files — this is the whole point of
the feature (edit docs without a code deploy) — so writes are a plain DB
table, not a git-backed content pipeline. Every write is audit-logged.
"""
from __future__ import annotations

import asyncio
import re

from quart import Blueprint, jsonify, request

from db import DocsPage, User
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user

docs_bp = Blueprint("v1_docs", __name__)

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _slugify(raw: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return s[:120]


def _summary(d: DocsPage) -> dict:
    return {
        "slug": d.slug,
        "title": d.title,
        "description": d.description,
        "category": d.category,
        "order": d.order,
    }


def _full(d: DocsPage) -> dict:
    return {**_summary(d), "content": d.body_md}


@docs_bp.get("/docs")
async def list_docs():
    def _load():
        with db_session() as s:
            rows = s.query(DocsPage).order_by(DocsPage.category.asc(), DocsPage.order.asc()).all()
            return [_summary(d) for d in rows]

    items = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(items), max_age=60)


@docs_bp.get("/docs/<slug>")
async def get_doc(slug: str):
    def _load():
        with db_session() as s:
            d = s.query(DocsPage).filter(DocsPage.slug == slug).first()
            return _full(d) if d else None

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Doc not found", f"No doc page '{slug}'.")
    return with_cache_headers(jsonify(payload), max_age=60)


def _validate(body: dict, *, require_slug: bool) -> dict:
    out = {}
    if require_slug or "slug" in body:
        raw_slug = str(body.get("slug") or "").strip()
        if not raw_slug:
            abort_problem(422, "Missing slug", "'slug' is required.")
        slug = _slugify(raw_slug)
        if not _SLUG_RE.match(slug):
            abort_problem(422, "Invalid slug", "Slug must be lowercase letters, numbers, and hyphens.")
        out["slug"] = slug
    if require_slug or "title" in body:
        title = str(body.get("title") or "").strip()
        if not (1 <= len(title) <= 200):
            abort_problem(422, "Invalid title", "Title must be 1-200 characters.")
        out["title"] = title
    if require_slug or "content" in body:
        content = str(body.get("content") or "").strip()
        if not content:
            abort_problem(422, "Invalid content", "content must not be empty.")
        out["body_md"] = content
    if "description" in body:
        out["description"] = (body.get("description") or "").strip() or None
    if require_slug or "category" in body:
        out["category"] = str(body.get("category") or "General").strip()[:80] or "General"
    if "order" in body:
        try:
            out["order"] = int(body["order"])
        except (TypeError, ValueError):
            abort_problem(422, "Invalid order", "'order' must be an integer.")
    return out


@docs_bp.post("/admin/docs")
async def create_doc():
    actor = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, actor))

    await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate(body, require_slug=True)
    fields.setdefault("order", 100)

    def _create():
        with db_session() as s:
            if s.query(DocsPage).filter(DocsPage.slug == fields["slug"]).first():
                abort_problem(409, "Slug taken", f"A doc page with slug '{fields['slug']}' already exists.")
            d = DocsPage(author_user_id=actor, **fields)
            s.add(d)
            s.commit()
            return d.id

    doc_id = await asyncio.to_thread(_create)
    _audit(actor, "docs.create", f"docs_pages:{fields['slug']}", after=fields["title"])
    return jsonify({"id": doc_id})


@docs_bp.patch("/admin/docs/<slug>")
async def update_doc(slug: str):
    actor = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, actor))

    await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate(body, require_slug=False)
    if not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")

    def _apply():
        with db_session() as s:
            d = s.query(DocsPage).filter(DocsPage.slug == slug).first()
            if not d:
                abort_problem(404, "Doc not found", f"No doc page '{slug}'.")
            if "slug" in fields and fields["slug"] != slug:
                if s.query(DocsPage).filter(DocsPage.slug == fields["slug"]).first():
                    abort_problem(409, "Slug taken", f"A doc page with slug '{fields['slug']}' already exists.")
            for k, v in fields.items():
                setattr(d, k, v)
            s.commit()
            return _full(d)

    payload = await asyncio.to_thread(_apply)
    _audit(actor, "docs.update", f"docs_pages:{slug}", after=str(list(fields.keys())))
    return private_no_store(jsonify(payload))


@docs_bp.delete("/admin/docs/<slug>")
async def delete_doc(slug: str):
    actor = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, actor))

    await asyncio.to_thread(_check)

    def _delete():
        with db_session() as s:
            d = s.query(DocsPage).filter(DocsPage.slug == slug).first()
            if not d:
                abort_problem(404, "Doc not found", f"No doc page '{slug}'.")
            s.delete(d)
            s.commit()

    await asyncio.to_thread(_delete)
    _audit(actor, "docs.delete", f"docs_pages:{slug}")
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
