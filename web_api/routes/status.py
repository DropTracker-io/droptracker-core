"""Service status + known-issues board — public read, developer CRUD.

Feeds three surfaces from one source of truth:
  * the #status Discord channel (core bot renders from DB/Redis directly),
  * /admin/status in the web CP (this module's admin routes),
  * a future public status page (the public GET below).

Public:
  GET /api/v1/status                     -> { services, categories[] } (open issues only)

Admin (developer-or-superadmin; every write audit-logs and bumps status:issues:rev so
the core bot re-renders the channel within its next sweep):
  GET    /api/v1/admin/status/issues            -> { categories: [...incl. resolved] }
  POST   /api/v1/admin/status/categories        { name, emoji?, order? }
  PATCH  /api/v1/admin/status/categories/{id}   partial
  DELETE /api/v1/admin/status/categories/{id}   (cascades its issues)
  POST   /api/v1/admin/status/issues            { category_id, title, ... }
  PATCH  /api/v1/admin/status/issues/{id}       partial; status=resolved stamps resolved_at
  DELETE /api/v1/admin/status/issues/{id}

See db/models/known_issues.py and services/status_metrics.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from quart import Blueprint, jsonify

from db import ISSUE_SEVERITIES, ISSUE_STATUSES, KnownIssue, KnownIssueCategory
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import assert_developer, current_user_id, json_body, load_user

status_bp = Blueprint("v1_status", __name__)


def _category(c: KnownIssueCategory, issues: list[dict]) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "emoji": c.emoji,
        "order": c.order,
        "issues": issues,
    }


def _issue(i: KnownIssue) -> dict:
    return {
        "id": i.id,
        "category_id": i.category_id,
        "title": i.title,
        "description": i.description,
        "severity": i.severity,
        "status": i.status,
        "order": i.order,
        "created_by": i.created_by,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "updated_at": i.updated_at.isoformat() if i.updated_at else None,
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
    }


def _load_tree(s, *, include_resolved: bool) -> list[dict]:
    cats = (
        s.query(KnownIssueCategory)
        .order_by(KnownIssueCategory.order, KnownIssueCategory.id)
        .all()
    )
    out = []
    for c in cats:
        issues = [
            _issue(i)
            for i in c.issues
            if include_resolved or i.status != "resolved"
        ]
        if not include_resolved and not issues:
            continue
        out.append(_category(c, issues))
    return out


def _require_developer(s, actor) -> str:
    """Assert developer (or superadmin) and return a display label for created_by stamps."""
    user = load_user(s, actor)
    assert_developer(user)
    username = getattr(user, "username", None)
    return (username or f"user:{actor}")[:80]


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


def _bump_rev() -> None:
    """Nudge the core bot to re-render the #status issues card. Fail-open."""
    try:
        from services.status_metrics import bump_issues_rev

        bump_issues_rev()
    except Exception:
        pass


def _validate_category(body: dict, *, require_core: bool) -> dict:
    fields: dict = {}
    if require_core or "name" in body:
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 100:
            abort_problem(422, "Invalid name", "Category name must be 1-100 characters.")
        fields["name"] = name
    if "emoji" in body:
        emoji = body.get("emoji")
        emoji = str(emoji).strip() if emoji is not None else None
        if emoji is not None and len(emoji) > 32:
            abort_problem(422, "Invalid emoji", "Emoji must be at most 32 characters.")
        fields["emoji"] = emoji or None
    if "order" in body:
        try:
            fields["order"] = int(body.get("order"))
        except (TypeError, ValueError):
            abort_problem(422, "Invalid order", "Order must be an integer.")
    if not require_core and not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")
    return fields


def _validate_issue(body: dict, *, require_core: bool) -> dict:
    fields: dict = {}
    if require_core or "category_id" in body:
        try:
            fields["category_id"] = int(body.get("category_id"))
        except (TypeError, ValueError):
            abort_problem(422, "Invalid category", "category_id must be an integer.")
    if require_core or "title" in body:
        title = str(body.get("title") or "").strip()
        if not title or len(title) > 200:
            abort_problem(422, "Invalid title", "Issue title must be 1-200 characters.")
        fields["title"] = title
    if "description" in body:
        desc = body.get("description")
        desc = str(desc).strip() if desc is not None else None
        if desc is not None and len(desc) > 2000:
            abort_problem(422, "Invalid description", "Description must be at most 2000 characters.")
        fields["description"] = desc or None
    if "severity" in body:
        severity = str(body.get("severity") or "").strip().lower()
        if severity not in ISSUE_SEVERITIES:
            abort_problem(422, "Invalid severity",
                          f"Severity must be one of: {', '.join(ISSUE_SEVERITIES)}.")
        fields["severity"] = severity
    if "status" in body:
        status = str(body.get("status") or "").strip().lower()
        if status not in ISSUE_STATUSES:
            abort_problem(422, "Invalid status",
                          f"Status must be one of: {', '.join(ISSUE_STATUSES)}.")
        fields["status"] = status
    if "order" in body:
        try:
            fields["order"] = int(body.get("order"))
        except (TypeError, ValueError):
            abort_problem(422, "Invalid order", "Order must be an integer.")
    if not require_core and not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")
    return fields


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

@status_bp.get("/status")
async def public_status():
    def _load():
        from services.status_metrics import collect_service_snapshot

        with db_session() as s:
            categories = _load_tree(s, include_resolved=False)
        snapshot = collect_service_snapshot()
        return {"services": snapshot, "categories": categories}

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=30)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@status_bp.get("/admin/status/issues")
async def admin_issues():
    actor = current_user_id()

    def _load():
        with db_session() as s:
            _require_developer(s, actor)
            return _load_tree(s, include_resolved=True)

    categories = await asyncio.to_thread(_load)
    return private_no_store(jsonify({"categories": categories}))


@status_bp.post("/admin/status/categories")
async def admin_create_category():
    actor = current_user_id()

    def _check():
        with db_session() as s:
            _require_developer(s, actor)

    await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate_category(body, require_core=True)

    def _create():
        with db_session() as s:
            cat = KnownIssueCategory(**fields)
            s.add(cat)
            s.commit()
            return _category(cat, [])

    created = await asyncio.to_thread(_create)
    _audit(actor, "status.category.create", f"known_issue_categories:{created['id']}",
           after=created["name"])
    _bump_rev()
    return jsonify(created), 201


@status_bp.patch("/admin/status/categories/<int:category_id>")
async def admin_update_category(category_id: int):
    actor = current_user_id()

    def _check():
        with db_session() as s:
            _require_developer(s, actor)

    await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate_category(body, require_core=False)

    def _update():
        with db_session() as s:
            cat = s.get(KnownIssueCategory, category_id)
            if cat is None:
                abort_problem(404, "Not found", "No such category.")
            for k, v in fields.items():
                setattr(cat, k, v)
            s.commit()
            return _category(cat, [_issue(i) for i in cat.issues])

    updated = await asyncio.to_thread(_update)
    _audit(actor, "status.category.update", f"known_issue_categories:{category_id}")
    _bump_rev()
    return jsonify(updated)


@status_bp.delete("/admin/status/categories/<int:category_id>")
async def admin_delete_category(category_id: int):
    actor = current_user_id()

    def _delete():
        with db_session() as s:
            _require_developer(s, actor)
            cat = s.get(KnownIssueCategory, category_id)
            if cat is None:
                abort_problem(404, "Not found", "No such category.")
            name = cat.name
            s.delete(cat)
            s.commit()
            return name

    name = await asyncio.to_thread(_delete)
    _audit(actor, "status.category.delete", f"known_issue_categories:{category_id}",
           before=name)
    _bump_rev()
    return jsonify({"ok": True})


@status_bp.post("/admin/status/issues")
async def admin_create_issue():
    actor = current_user_id()

    def _check():
        with db_session() as s:
            return _require_developer(s, actor)

    author = await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate_issue(body, require_core=True)

    def _create():
        with db_session() as s:
            if s.get(KnownIssueCategory, fields["category_id"]) is None:
                abort_problem(422, "Invalid category", "No such category.")
            issue = KnownIssue(created_by=author, **fields)
            if issue.status == "resolved":
                issue.resolved_at = datetime.utcnow()
            s.add(issue)
            s.commit()
            return _issue(issue)

    created = await asyncio.to_thread(_create)
    _audit(actor, "status.issue.create", f"known_issues:{created['id']}",
           after=created["title"])
    _bump_rev()
    return jsonify(created), 201


@status_bp.patch("/admin/status/issues/<int:issue_id>")
async def admin_update_issue(issue_id: int):
    actor = current_user_id()

    def _check():
        with db_session() as s:
            _require_developer(s, actor)

    await asyncio.to_thread(_check)
    body = await json_body()
    fields = _validate_issue(body, require_core=False)

    def _update():
        with db_session() as s:
            issue = s.get(KnownIssue, issue_id)
            if issue is None:
                abort_problem(404, "Not found", "No such issue.")
            if "category_id" in fields and \
                    s.get(KnownIssueCategory, fields["category_id"]) is None:
                abort_problem(422, "Invalid category", "No such category.")
            prev_status = issue.status
            for k, v in fields.items():
                setattr(issue, k, v)
            if issue.status == "resolved" and prev_status != "resolved":
                issue.resolved_at = datetime.utcnow()
            elif issue.status != "resolved":
                issue.resolved_at = None
            s.commit()
            return _issue(issue)

    updated = await asyncio.to_thread(_update)
    _audit(actor, "status.issue.update", f"known_issues:{issue_id}")
    _bump_rev()
    return jsonify(updated)


@status_bp.delete("/admin/status/issues/<int:issue_id>")
async def admin_delete_issue(issue_id: int):
    actor = current_user_id()

    def _delete():
        with db_session() as s:
            _require_developer(s, actor)
            issue = s.get(KnownIssue, issue_id)
            if issue is None:
                abort_problem(404, "Not found", "No such issue.")
            title = issue.title
            s.delete(issue)
            s.commit()
            return title

    title = await asyncio.to_thread(_delete)
    _audit(actor, "status.issue.delete", f"known_issues:{issue_id}", before=title)
    _bump_rev()
    return jsonify({"ok": True})
