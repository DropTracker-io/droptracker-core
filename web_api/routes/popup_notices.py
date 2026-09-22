"""Targeted site pop-up notices (web118a).

Staff write a Markdown notice in /admin/notices, pick an audience (see
``web_api/popup_audience``) and send it. Signed-in visitors who match see it as
a pop-up on their next page load until they close it. Closing is recorded here,
per user, so it stays closed on every device.

Visitor (session):
  GET  /api/v1/me/notices             -> { items: PopupNotice[] }  live, matching, not closed
  POST /api/v1/me/notices/seen        { ids }  -> { ok }  first-shown timestamps (stats)
  POST /api/v1/me/notices/dismiss     { ids }  -> { ok }  closed: never shown again

Admin (superadmin):
  GET    /api/v1/admin/notices                    -> { items: AdminPopupNotice[] }
  GET    /api/v1/admin/notices/{id}               -> AdminPopupNotice + receipts
  POST   /api/v1/admin/notices                    { ...fields, send? } -> AdminPopupNotice
  PATCH  /api/v1/admin/notices/{id}               partial fields -> AdminPopupNotice
  POST   /api/v1/admin/notices/{id}/send          draft -> live
  POST   /api/v1/admin/notices/{id}/end           live  -> ended
  DELETE /api/v1/admin/notices/{id}               -> { ok }
  POST   /api/v1/admin/notices/audience-preview   { audience } -> { count, everyone, sample, notes, summary }
  GET    /api/v1/admin/notices/options            -> { tiers }
  GET    /api/v1/admin/notices/lookup?kind=user|group&q=  -> { items }

The visitor read is on every signed-in page load, so it is built to cost
nothing in the usual case: the live-notice list is cached in-process for
``_LIVE_TTL`` seconds, and with no live notices the request never touches the
database. Webapi runs two workers, so a send/end is visible within that TTL.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from quart import Blueprint, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from db import Group, Player, PopupNotice, PopupNoticeReceipt, SubscriptionTier, User
from web_api.common import abort_problem, cache_delete, cache_get, cache_set, db_session, private_no_store
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user
from web_api.popup_audience import (
    FREE_TIER,
    AudienceError,
    audience_matches,
    describe_audience,
    facts_needed,
    load_viewer_facts,
    normalize_audience,
    parse_stored_audience,
    resolve_audience_user_ids,
)

popup_notices_bp = Blueprint("v1_popup_notices", __name__)

TONES = ("info", "important", "success")
SIZES = ("sm", "md", "lg")
TITLE_MAX = 120
BODY_MAX = 20_000
CTA_LABEL_MAX = 40
CTA_URL_MAX = 512

#: At most this many notices come back per page load; the rest wait their turn.
VIEWER_MAX = 5
#: Ids per seen/dismiss call.
IDS_MAX = 20

_LIVE_KEY = "popup_notices:live"
_LIVE_TTL = 20.0

_EXTERNAL_RE = re.compile(r"^https?://", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _ts(dt):
    return int(dt.timestamp()) if dt else None


def _from_ts(value, what: str):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        abort_problem(422, f"Invalid {what}", f"'{what}' must be a unix timestamp in seconds.")
    try:
        return datetime.fromtimestamp(int(value))
    except (TypeError, ValueError, OverflowError, OSError):
        abort_problem(422, f"Invalid {what}", f"'{what}' must be a unix timestamp in seconds.")


def state_of(notice, now: datetime) -> str:
    """The lifecycle a person would describe: draft, scheduled, live, expired
    or ended. Only ``live`` is shown to visitors."""
    if notice.status == "draft":
        return "draft"
    if notice.status == "ended":
        return "ended"
    if notice.expires_at and notice.expires_at <= now:
        return "expired"
    if notice.starts_at and notice.starts_at > now:
        return "scheduled"
    return "live"


def _invalidate_live() -> None:
    cache_delete(_LIVE_KEY)


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


# --------------------------------------------------------------------------- #
# Visitor
# --------------------------------------------------------------------------- #
def _viewer_payload(n: dict) -> dict:
    return {
        "id": n["id"],
        "title": n["title"],
        "body_md": n["body_md"],
        "cta_label": n["cta_label"],
        "cta_url": n["cta_url"],
        "tone": n["tone"],
        "size": n["size"],
        "sent_at": n["sent_at"],
    }


def _live_notices() -> list[dict]:
    """Every ``live`` notice that has not expired, with parsed rules. Cached
    in-process; scheduled ones are included and filtered per request so a
    start time takes effect on the minute, not on the next cache fill."""
    cached = cache_get(_LIVE_KEY, _LIVE_TTL)
    if cached is not None:
        return cached

    now = datetime.now()
    with db_session() as s:
        rows = (
            s.query(PopupNotice)
            .filter(
                PopupNotice.status == "live",
                or_(PopupNotice.expires_at.is_(None), PopupNotice.expires_at > now),
            )
            .order_by(PopupNotice.sent_at.asc(), PopupNotice.id.asc())
            .all()
        )
        live = [
            {
                "id": r.id,
                "title": r.title,
                "body_md": r.body_md,
                "cta_label": r.cta_label,
                "cta_url": r.cta_url,
                "tone": r.tone,
                "size": r.size,
                "sent_at": _ts(r.sent_at),
                "starts_at": r.starts_at,
                "expires_at": r.expires_at,
                "rules": parse_stored_audience(r.audience_json),
            }
            for r in rows
        ]
    cache_set(_LIVE_KEY, live)
    return live


def _in_window(n: dict, now: datetime) -> bool:
    if n["starts_at"] and n["starts_at"] > now:
        return False
    if n["expires_at"] and n["expires_at"] <= now:
        return False
    return bool(n["rules"])


@popup_notices_bp.get("/me/notices")
async def my_notices():
    user_id = current_user_id()

    def _load():
        now = datetime.now()
        candidates = [n for n in _live_notices() if _in_window(n, now)]
        if not candidates:
            return []
        with db_session() as s:
            closed = {
                int(nid)
                for (nid,) in s.query(PopupNoticeReceipt.notice_id).filter(
                    PopupNoticeReceipt.user_id == user_id,
                    PopupNoticeReceipt.notice_id.in_([n["id"] for n in candidates]),
                    PopupNoticeReceipt.dismissed_at.isnot(None),
                )
            }
            remaining = [n for n in candidates if n["id"] not in closed]
            if not remaining:
                return []
            facts = load_viewer_facts(s, user_id, facts_needed(n["rules"] for n in remaining))
            matched = [n for n in remaining if audience_matches(n["rules"], facts)]
        # "Important" first, then oldest first so a backlog reads in order.
        matched.sort(key=lambda n: (n["tone"] != "important", n["sent_at"] or 0, n["id"]))
        return [_viewer_payload(n) for n in matched[:VIEWER_MAX]]

    items = await asyncio.to_thread(_load)
    return private_no_store(jsonify({"items": items}))


async def _receipt_ids() -> list[int]:
    body = await json_body()
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        abort_problem(422, "Invalid ids", "'ids' must be a non-empty list of notice ids.")
    out: list[int] = []
    for v in ids:
        if isinstance(v, bool):
            abort_problem(422, "Invalid ids", "'ids' must hold notice ids.")
        try:
            n = int(v)
        except (TypeError, ValueError):
            abort_problem(422, "Invalid ids", "'ids' must hold notice ids.")
        if n not in out:
            out.append(n)
    if len(out) > IDS_MAX:
        abort_problem(422, "Too many ids", f"At most {IDS_MAX} notices per call.")
    return out


def _write_receipts(user_id: int, ids: list[int], *, dismiss: bool) -> None:
    """Upsert receipts. ``seen_at`` keeps its first value; closing stamps
    ``dismissed_at`` once. Ids that aren't notices are ignored, so a stale tab
    closing a deleted notice is not an error. One retry covers the race where
    another tab inserts the same row between our read and write."""
    for attempt in (0, 1):
        now = datetime.now()
        with db_session() as s:
            known = {
                int(i) for (i,) in s.query(PopupNotice.id).filter(PopupNotice.id.in_(ids))
            }
            if not known:
                return
            existing = {
                r.notice_id: r
                for r in s.query(PopupNoticeReceipt).filter(
                    PopupNoticeReceipt.user_id == user_id,
                    PopupNoticeReceipt.notice_id.in_(known),
                )
            }
            for nid in known:
                row = existing.get(nid)
                if row is None:
                    row = PopupNoticeReceipt(notice_id=nid, user_id=user_id)
                    s.add(row)
                if row.seen_at is None:
                    row.seen_at = now
                if dismiss and row.dismissed_at is None:
                    row.dismissed_at = now
            try:
                s.commit()
                return
            except IntegrityError:
                s.rollback()
                if attempt:
                    raise


@popup_notices_bp.post("/me/notices/seen")
async def mark_seen():
    user_id = current_user_id()
    ids = await _receipt_ids()
    await asyncio.to_thread(_write_receipts, user_id, ids, dismiss=False)
    return private_no_store(jsonify({"ok": True}))


@popup_notices_bp.post("/me/notices/dismiss")
async def dismiss():
    user_id = current_user_id()
    ids = await _receipt_ids()
    await asyncio.to_thread(_write_receipts, user_id, ids, dismiss=True)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Admin: validation
# --------------------------------------------------------------------------- #
def _known_tiers(s) -> tuple[set, set]:
    """(group-scope keys incl. FREE_TIER, every paid key) for audience checks."""
    group_keys = {FREE_TIER}
    paid_keys: set = set()
    for t in s.query(SubscriptionTier).all():
        if t.scope == "group":
            group_keys.add(t.key)
        if (t.price_cents or 0) > 0:
            paid_keys.add(t.key)
    return group_keys, paid_keys


def _validate_audience(s, raw) -> list[dict]:
    try:
        rules = normalize_audience(raw)
    except AudienceError as exc:
        abort_problem(422, "Invalid audience", str(exc))
    group_keys, paid_keys = _known_tiers(s)
    for r in rules:
        for key in r.get("group_tiers") or []:
            if key not in group_keys:
                abort_problem(422, "Invalid audience", f"'{key}' is not a group tier.")
        for key in r.get("tier_keys") or []:
            if key not in paid_keys:
                abort_problem(422, "Invalid audience", f"'{key}' is not a paid tier.")
    return rules


def _validate_fields(body: dict, *, partial: bool) -> dict:
    """Content, appearance and schedule. Audience is validated separately (it
    needs a DB session for the tier keys)."""
    out: dict = {}

    if not partial or "title" in body:
        title = str(body.get("title") or "").strip()
        if not (1 <= len(title) <= TITLE_MAX):
            abort_problem(422, "Invalid title", f"Title must be 1-{TITLE_MAX} characters.")
        out["title"] = title

    if not partial or "body_md" in body:
        text = str(body.get("body_md") or "").strip()
        if not text:
            abort_problem(422, "Invalid body", "The message body can't be empty.")
        if len(text) > BODY_MAX:
            abort_problem(422, "Invalid body", f"The message body must be under {BODY_MAX} characters.")
        out["body_md"] = text

    if not partial or "cta_label" in body or "cta_url" in body:
        label = str(body.get("cta_label") or "").strip()
        url = str(body.get("cta_url") or "").strip()
        if bool(label) != bool(url):
            abort_problem(422, "Invalid button", "A button needs both a label and a link.")
        if len(label) > CTA_LABEL_MAX:
            abort_problem(422, "Invalid button", f"Button label must be {CTA_LABEL_MAX} characters or fewer.")
        if url and not ((url.startswith("/") and not url.startswith("//")) or _EXTERNAL_RE.match(url)):
            abort_problem(
                422, "Invalid button", "Button link must be a site path ('/...') or an http(s):// URL."
            )
        if len(url) > CTA_URL_MAX:
            abort_problem(422, "Invalid button", f"Button link must be {CTA_URL_MAX} characters or fewer.")
        out["cta_label"] = label or None
        out["cta_url"] = url or None

    if not partial or "tone" in body:
        tone = body.get("tone") or "info"
        if tone not in TONES:
            abort_problem(422, "Invalid style", f"tone must be one of {', '.join(TONES)}.")
        out["tone"] = tone

    if not partial or "size" in body:
        size = body.get("size") or "md"
        if size not in SIZES:
            abort_problem(422, "Invalid width", f"size must be one of {', '.join(SIZES)}.")
        out["size"] = size

    if not partial or "starts_at" in body:
        out["starts_at"] = _from_ts(body.get("starts_at"), "starts_at")
    if not partial or "expires_at" in body:
        out["expires_at"] = _from_ts(body.get("expires_at"), "expires_at")

    return out


def _check_window(starts_at, expires_at, *, sending: bool) -> None:
    if starts_at and expires_at and expires_at <= starts_at:
        abort_problem(422, "Invalid schedule", "The end time must be after the start time.")
    if sending and expires_at and expires_at <= datetime.now():
        abort_problem(422, "Invalid schedule", "The end time is already in the past.")


# --------------------------------------------------------------------------- #
# Admin: serialisation
# --------------------------------------------------------------------------- #
def _labels(s, rule_sets) -> dict:
    """Display names for the ids and tier keys the rules mention, so the admin
    UI can render chips without one lookup per id."""
    user_ids: set = set()
    group_ids: set = set()
    for rules in rule_sets:
        for r in rules:
            user_ids.update(r.get("user_ids") or [])
            group_ids.update(r.get("group_ids") or [])
    users = {}
    if user_ids:
        for uid, name in s.query(User.user_id, User.username).filter(User.user_id.in_(user_ids)):
            users[str(uid)] = name or f"User {uid}"
    groups = {}
    if group_ids:
        for gid, name in s.query(Group.group_id, Group.group_name).filter(Group.group_id.in_(group_ids)):
            groups[str(gid)] = name or f"Group {gid}"
    tiers = {t.key: t.name for t in s.query(SubscriptionTier).all()}
    tiers.setdefault(FREE_TIER, "Free")
    return {"users": users, "groups": groups, "tiers": tiers}


def _summary(rules: list[dict], labels: dict) -> str:
    names = {("group", int(k)): v for k, v in labels["groups"].items()}
    names.update({("tier", k): v for k, v in labels["tiers"].items()})
    return describe_audience(rules, names) if rules else "Nobody (audience unreadable)"


def _admin_payload(n: PopupNotice, stats: dict, labels: dict, now: datetime) -> dict:
    rules = parse_stored_audience(n.audience_json)
    seen, closed = stats.get(n.id, (0, 0))
    return {
        "id": n.id,
        "title": n.title,
        "body_md": n.body_md,
        "cta_label": n.cta_label,
        "cta_url": n.cta_url,
        "tone": n.tone,
        "size": n.size,
        "audience": rules,
        "audience_summary": _summary(rules, labels),
        "status": n.status,
        "state": state_of(n, now),
        "starts_at": _ts(n.starts_at),
        "expires_at": _ts(n.expires_at),
        "sent_at": _ts(n.sent_at),
        "ended_at": _ts(n.ended_at),
        "created_at": _ts(n.created_at),
        "updated_at": _ts(n.updated_at),
        "audience_estimate": n.audience_estimate,
        "seen_count": int(seen),
        "dismissed_count": int(closed),
    }


def _stats(s, ids) -> dict:
    if not ids:
        return {}
    rows = (
        s.query(
            PopupNoticeReceipt.notice_id,
            func.count(PopupNoticeReceipt.id),
            func.count(PopupNoticeReceipt.dismissed_at),
        )
        .filter(PopupNoticeReceipt.notice_id.in_(list(ids)))
        .group_by(PopupNoticeReceipt.notice_id)
        .all()
    )
    return {int(nid): (seen, closed) for nid, seen, closed in rows}


def _estimate(s, rules: list[dict]) -> tuple[int, bool, list[int], list[str]]:
    ids, notes = resolve_audience_user_ids(s, rules)
    if ids is None:
        return int(s.query(func.count(User.user_id)).scalar() or 0), True, [], notes
    return len(ids), False, sorted(ids), notes


def _one(s, notice_id: int) -> PopupNotice:
    n = s.query(PopupNotice).filter(PopupNotice.id == notice_id).first()
    if n is None:
        abort_problem(404, "Notice not found", f"No pop-up notice {notice_id}.")
    return n


def _assert_admin(s, actor: int) -> None:
    assert_superadmin(load_user(s, actor))


def _full(s, n: PopupNotice) -> dict:
    rules = parse_stored_audience(n.audience_json)
    labels = _labels(s, [rules])
    out = _admin_payload(n, _stats(s, [n.id]), labels, datetime.now())
    out["labels"] = labels
    return out


# --------------------------------------------------------------------------- #
# Admin: routes
# --------------------------------------------------------------------------- #
@popup_notices_bp.get("/admin/notices")
async def admin_list():
    actor = current_user_id()

    def _load():
        with db_session() as s:
            _assert_admin(s, actor)
            rows = (
                s.query(PopupNotice)
                .order_by(PopupNotice.created_at.desc(), PopupNotice.id.desc())
                .limit(200)
                .all()
            )
            stats = _stats(s, [r.id for r in rows])
            labels = _labels(s, [parse_stored_audience(r.audience_json) for r in rows])
            now = datetime.now()
            return {
                "items": [_admin_payload(r, stats, labels, now) for r in rows],
                "labels": labels,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@popup_notices_bp.get("/admin/notices/<int:notice_id>")
async def admin_get(notice_id: int):
    actor = current_user_id()

    def _load():
        with db_session() as s:
            _assert_admin(s, actor)
            n = _one(s, notice_id)
            out = _full(s, n)
            rows = (
                s.query(PopupNoticeReceipt, User.username)
                .outerjoin(User, User.user_id == PopupNoticeReceipt.user_id)
                .filter(PopupNoticeReceipt.notice_id == notice_id)
                .order_by(PopupNoticeReceipt.seen_at.desc(), PopupNoticeReceipt.id.desc())
                .limit(100)
                .all()
            )
            out["receipts"] = [
                {
                    "user_id": int(r.user_id),
                    "name": name or f"User {r.user_id}",
                    "seen_at": _ts(r.seen_at),
                    "dismissed_at": _ts(r.dismissed_at),
                }
                for r, name in rows
            ]
            return out

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@popup_notices_bp.post("/admin/notices")
async def admin_create():
    actor = current_user_id()
    body = await json_body()
    fields = _validate_fields(body, partial=False)
    send = bool(body.get("send", False))
    _check_window(fields["starts_at"], fields["expires_at"], sending=send)

    def _create():
        with db_session() as s:
            _assert_admin(s, actor)
            rules = _validate_audience(s, body.get("audience"))
            n = PopupNotice(
                created_by=actor,
                audience_json=json.dumps(rules),
                status="draft",
                **fields,
            )
            if send:
                n.status = "live"
                n.sent_at = datetime.now()
                n.audience_estimate = _estimate(s, rules)[0]
            s.add(n)
            s.commit()
            return _full(s, n)

    payload = await asyncio.to_thread(_create)
    _invalidate_live()
    _audit(
        actor,
        "notice.send" if send else "notice.create",
        f"popup_notices:{payload['id']}",
        after=json.dumps({"title": payload["title"], "audience": payload["audience_summary"]}),
    )
    return private_no_store(jsonify(payload))


@popup_notices_bp.patch("/admin/notices/<int:notice_id>")
async def admin_update(notice_id: int):
    actor = current_user_id()
    body = await json_body()
    fields = _validate_fields(body, partial=True)

    def _apply():
        with db_session() as s:
            _assert_admin(s, actor)
            n = _one(s, notice_id)
            if n.status == "ended":
                abort_problem(409, "Notice ended", "An ended notice can't be edited. Duplicate it instead.")
            before = {"title": n.title, "audience": n.audience_json}
            if "audience" in body:
                n.audience_json = json.dumps(_validate_audience(s, body["audience"]))
            for key, value in fields.items():
                setattr(n, key, value)
            _check_window(n.starts_at, n.expires_at, sending=False)
            s.commit()
            return before, _full(s, n)

    before, payload = await asyncio.to_thread(_apply)
    _invalidate_live()
    _audit(
        actor,
        "notice.update",
        f"popup_notices:{notice_id}",
        before=json.dumps(before),
        after=json.dumps({"title": payload["title"], "audience": payload["audience_summary"]}),
    )
    return private_no_store(jsonify(payload))


@popup_notices_bp.post("/admin/notices/<int:notice_id>/send")
async def admin_send(notice_id: int):
    actor = current_user_id()

    def _apply():
        with db_session() as s:
            _assert_admin(s, actor)
            n = _one(s, notice_id)
            if n.status != "draft":
                abort_problem(409, "Already sent", "Only a draft can be sent.")
            rules = parse_stored_audience(n.audience_json)
            if not rules:
                abort_problem(422, "Invalid audience", "Choose who should see this notice first.")
            _check_window(n.starts_at, n.expires_at, sending=True)
            n.status = "live"
            n.sent_at = datetime.now()
            n.audience_estimate = _estimate(s, rules)[0]
            s.commit()
            return _full(s, n)

    payload = await asyncio.to_thread(_apply)
    _invalidate_live()
    _audit(
        actor,
        "notice.send",
        f"popup_notices:{notice_id}",
        after=json.dumps({"title": payload["title"], "audience": payload["audience_summary"]}),
    )
    return private_no_store(jsonify(payload))


@popup_notices_bp.post("/admin/notices/<int:notice_id>/end")
async def admin_end(notice_id: int):
    actor = current_user_id()

    def _apply():
        with db_session() as s:
            _assert_admin(s, actor)
            n = _one(s, notice_id)
            if n.status != "live":
                abort_problem(409, "Not live", "Only a sent notice can be ended.")
            n.status = "ended"
            n.ended_at = datetime.now()
            s.commit()
            return _full(s, n)

    payload = await asyncio.to_thread(_apply)
    _invalidate_live()
    _audit(actor, "notice.end", f"popup_notices:{notice_id}")
    return private_no_store(jsonify(payload))


@popup_notices_bp.delete("/admin/notices/<int:notice_id>")
async def admin_delete(notice_id: int):
    actor = current_user_id()

    def _apply():
        with db_session() as s:
            _assert_admin(s, actor)
            n = _one(s, notice_id)
            title = n.title
            s.query(PopupNoticeReceipt).filter(PopupNoticeReceipt.notice_id == notice_id).delete(
                synchronize_session=False
            )
            s.delete(n)
            s.commit()
            return title

    title = await asyncio.to_thread(_apply)
    _invalidate_live()
    _audit(actor, "notice.delete", f"popup_notices:{notice_id}", before=title)
    return private_no_store(jsonify({"ok": True}))


@popup_notices_bp.post("/admin/notices/audience-preview")
async def admin_audience_preview():
    actor = current_user_id()
    body = await json_body()

    def _load():
        with db_session() as s:
            _assert_admin(s, actor)
            rules = _validate_audience(s, body.get("audience"))
            count, everyone, ids, notes = _estimate(s, rules)
            sample = []
            if ids:
                rows = (
                    s.query(User.username)
                    .filter(User.user_id.in_(ids[:200]), User.username.isnot(None))
                    .order_by(User.username.asc())
                    .limit(8)
                    .all()
                )
                sample = [name for (name,) in rows]
            return {
                "count": count,
                "everyone": everyone,
                "sample": sample,
                "notes": notes,
                "summary": _summary(rules, _labels(s, [rules])),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@popup_notices_bp.get("/admin/notices/options")
async def admin_options():
    actor = current_user_id()

    def _load():
        with db_session() as s:
            _assert_admin(s, actor)
            tiers = []
            for t in (
                s.query(SubscriptionTier)
                .filter(SubscriptionTier.active.is_(True))
                .order_by(SubscriptionTier.scope.asc(), SubscriptionTier.price_cents.asc())
            ):
                if t.key == FREE_TIER:
                    continue
                tiers.append({
                    "key": t.key,
                    "name": t.name,
                    "scope": t.scope,
                    "paid": (t.price_cents or 0) > 0,
                })
            return {"tiers": tiers, "free_tier": FREE_TIER}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


_LOOKUP_LIMIT = 10


@popup_notices_bp.get("/admin/notices/lookup")
async def admin_lookup():
    actor = current_user_id()
    kind = (request.args.get("kind") or "user").strip()
    q = (request.args.get("q") or "").strip()
    if kind not in ("user", "group"):
        abort_problem(422, "Invalid kind", "kind must be 'user' or 'group'.")

    def _load():
        with db_session() as s:
            _assert_admin(s, actor)
            if len(q) < 2 and not q.lstrip("-").isdigit():
                return {"items": []}
            like = f"%{q}%"
            as_int = int(q) if q.lstrip("-").isdigit() and len(q) < 12 else None

            if kind == "group":
                cond = Group.group_name.ilike(like)
                if as_int is not None:
                    cond = or_(cond, Group.group_id == as_int)
                rows = s.query(Group.group_id, Group.group_name).filter(cond).order_by(
                    Group.group_name.asc()
                ).limit(_LOOKUP_LIMIT)
                return {
                    "items": [
                        {"id": int(gid), "name": name or f"Group {gid}", "detail": f"Group #{gid}"}
                        for gid, name in rows
                    ]
                }

            # Users: username, Discord id, user id, or any linked RSN.
            conds = [User.username.ilike(like), User.discord_id == q]
            if as_int is not None:
                conds.append(User.user_id == as_int)
            via_rsn = s.query(Player.user_id).filter(
                Player.player_name.ilike(like), Player.user_id.isnot(None)
            ).limit(_LOOKUP_LIMIT)
            conds.append(User.user_id.in_([u for (u,) in via_rsn]))
            users = (
                s.query(User)
                .filter(or_(*conds))
                .order_by(User.username.asc())
                .limit(_LOOKUP_LIMIT)
                .all()
            )
            rsns: dict = {}
            if users:
                for uid, name in s.query(Player.user_id, Player.player_name).filter(
                    Player.user_id.in_([u.user_id for u in users])
                ):
                    rsns.setdefault(int(uid), []).append(name)
            items = []
            for u in users:
                names = rsns.get(int(u.user_id), [])
                detail = f"User #{u.user_id}"
                if names:
                    shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
                    detail += f" · {shown}"
                items.append({
                    "id": int(u.user_id),
                    "name": u.username or f"User {u.user_id}",
                    "detail": detail,
                })
            return {"items": items}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))
