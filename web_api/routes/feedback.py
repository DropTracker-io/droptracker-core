"""Clan leader feedback (web155a).

Staff ask chosen people for feedback from /admin/feedback: a request carries an
audience in the pop-up notice rule format (``web_api/popup_audience``), and a
matching signed-in visitor sees a small feedback card until they answer, press
"Not now" (back in 14 days) or "Don't ask again". Anyone signed in can also
send feedback any time from /feedback.

Every answer is stored and posted to the staff "raw" Discord channel through
the outbox, with Publish / Dismiss buttons (``services/feedback_core.py``,
``services/feedback_buttons_discord.py``).

Visitor (session):
  GET  /api/v1/me/feedback/prompt   -> { prompt: {request_id} | null, groups }
  GET  /api/v1/me/feedback/groups   -> { groups }   clans the visitor leads
  POST /api/v1/me/feedback/seen     { request_id }  -> { ok }
  POST /api/v1/me/feedback/snooze   { request_id, forever? } -> { ok }
  POST /api/v1/me/feedback          { request_id?, group_id?, rating, liked_text?,
                                      improve_text?, quote_consent, attribution,
                                      contact_ok } -> { ok, id }

Admin (superadmin):
  GET    /api/v1/admin/feedback                          -> { requests, responses, labels }
  POST   /api/v1/admin/feedback/requests                 { title, audience, send? }
  PATCH  /api/v1/admin/feedback/requests/{id}            { title?, audience? }
  POST   /api/v1/admin/feedback/requests/{id}/send       draft -> live
  POST   /api/v1/admin/feedback/requests/{id}/end        live -> ended
  DELETE /api/v1/admin/feedback/requests/{id}
  POST   /api/v1/admin/feedback/responses/{id}/repost    post to the raw channel again

The audience preview and people/group pickers are the notice ones
(``/admin/notices/audience-preview`` and ``/admin/notices/lookup``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from quart import Blueprint, jsonify
from sqlalchemy import func

from db import FeedbackReceipt, FeedbackRequest, FeedbackResponse, Group, SubscriptionTier, User
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

feedback_bp = Blueprint("v1_feedback", __name__)
logger = logging.getLogger("web_api.feedback")

TITLE_MAX = 120
TEXT_MAX = 500
RATING_MIN, RATING_MAX = 1, 10
ATTRIBUTIONS = ("clan", "full", "anon")
SNOOZE = timedelta(days=14)
#: Answers one person may send per hour (the /feedback page has no request
#: to hang a one-answer limit on).
HOURLY_LIMIT = 5
ADMIN_RESPONSES_MAX = 300
#: The site-wide DropTracker.io group every player is in. Not a clan anyone
#: leads in the usual sense, so it is never offered as "answering for".
GLOBAL_GROUP_ID = 2

_LIVE_KEY = "feedback:live"
_LIVE_TTL = 20.0


def _ts(dt):
    return int(dt.timestamp()) if dt else None


def _audit(actor_user_id, action, target, before=None, after=None):
    try:
        from db import AuditLog

        with db_session() as s:
            s.add(AuditLog(actor_user_id=actor_user_id, group_id=None, action=action,
                           target=target, before=before, after=after))
            s.commit()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Visitor
# --------------------------------------------------------------------------- #
def _live_requests() -> list[dict]:
    """Live requests with parsed rules, cached in-process like pop-up notices.
    A failed read means "nothing live": this runs on signed-in page loads."""
    cached = cache_get(_LIVE_KEY, _LIVE_TTL)
    if cached is not None:
        return cached
    try:
        with db_session() as s:
            rows = (s.query(FeedbackRequest.id, FeedbackRequest.audience_json)
                    .filter(FeedbackRequest.status == "live")
                    .order_by(FeedbackRequest.sent_at.asc(), FeedbackRequest.id.asc()).all())
            live = [{"id": int(rid), "rules": parse_stored_audience(aj)} for rid, aj in rows]
    except Exception:
        logger.exception("feedback: could not read live requests; showing none")
        live = []
    cache_set(_LIVE_KEY, live)
    return live


def _member_counts(s, group_ids) -> dict:
    from db.models import user_group_association as uga

    if not group_ids:
        return {}
    rows = (s.query(uga.c.group_id, func.count(func.distinct(uga.c.player_id)))
            .filter(uga.c.group_id.in_(list(group_ids)), uga.c.player_id.isnot(None))
            .group_by(uga.c.group_id).all())
    return {int(g): int(n) for g, n in rows}


def leader_groups(s, user_id: int) -> list[dict]:
    """Clans the user owns or admins: owners first, then by name. Each carries
    the snapshot a feedback answer stores (role, tier, member count)."""
    from web_api.popup_audience import _leader_roles, _paid_group_tiers

    roles = _leader_roles(s, user_id)
    roles.pop(GLOBAL_GROUP_ID, None)
    if not roles:
        return []
    groups = {int(gid): (name, added) for gid, name, added in
              s.query(Group.group_id, Group.group_name, Group.date_added)
              .filter(Group.group_id.in_(list(roles)))}
    tiers = _paid_group_tiers(s, list(groups))
    tier_names = {t.key: t.name for t in s.query(SubscriptionTier).all()}
    counts = _member_counts(s, list(groups))
    out = []
    for gid, (name, added) in groups.items():
        tier = tiers.get(gid, FREE_TIER)
        out.append({
            "group_id": gid,
            "name": name or f"Group {gid}",
            "role": roles[gid],
            "tier": tier,
            "tier_name": tier_names.get(tier, "Free" if tier == FREE_TIER else tier),
            "member_count": counts.get(gid, 0),
            "since": _ts(added),
        })
    out.sort(key=lambda g: (g["role"] != "owner", g["name"].lower()))
    return out


def _closed_ids(s, user_id: int, request_ids: list[int], now: datetime) -> set:
    closed = set()
    for r in s.query(FeedbackReceipt).filter(FeedbackReceipt.user_id == user_id,
                                             FeedbackReceipt.request_id.in_(request_ids)):
        if r.responded_at or r.declined_at or (r.snoozed_until and r.snoozed_until > now):
            closed.add(int(r.request_id))
    return closed


@feedback_bp.get("/me/feedback/prompt")
async def my_prompt():
    user_id = current_user_id()

    def _load():
        live = [r for r in _live_requests() if r["rules"]]
        if not live:
            return {"prompt": None, "groups": []}
        now = datetime.now()
        with db_session() as s:
            closed = _closed_ids(s, user_id, [r["id"] for r in live], now)
            remaining = [r for r in live if r["id"] not in closed]
            if not remaining:
                return {"prompt": None, "groups": []}
            facts = load_viewer_facts(s, user_id, facts_needed(r["rules"] for r in remaining))
            match = next((r for r in remaining if audience_matches(r["rules"], facts)), None)
            if match is None:
                return {"prompt": None, "groups": []}
            return {"prompt": {"request_id": match["id"]}, "groups": leader_groups(s, user_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@feedback_bp.get("/me/feedback/groups")
async def my_groups():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            return {"groups": leader_groups(s, user_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


def _int(value, what: str, *, required: bool = True):
    if value is None or value == "":
        if required:
            abort_problem(422, f"Invalid {what}", f"'{what}' is required.")
        return None
    if isinstance(value, bool):
        abort_problem(422, f"Invalid {what}", f"'{what}' must be a number.")
    try:
        return int(value)
    except (TypeError, ValueError):
        abort_problem(422, f"Invalid {what}", f"'{what}' must be a number.")


def _receipt(s, request_id: int, user_id: int) -> FeedbackReceipt:
    row = (s.query(FeedbackReceipt)
           .filter(FeedbackReceipt.request_id == request_id, FeedbackReceipt.user_id == user_id)
           .first())
    if row is None:
        row = FeedbackReceipt(request_id=request_id, user_id=user_id)
        s.add(row)
    return row


def _write_receipt(user_id: int, request_id: int, **changes) -> None:
    """Upsert one receipt. Unknown requests are ignored (a stale tab). One
    retry covers two tabs inserting the same row at once."""
    from sqlalchemy.exc import IntegrityError

    for attempt in (0, 1):
        with db_session() as s:
            if s.query(FeedbackRequest.id).filter(FeedbackRequest.id == request_id).first() is None:
                return
            row = _receipt(s, request_id, user_id)
            for key, value in changes.items():
                if key == "seen_at" and row.seen_at is not None:
                    continue
                setattr(row, key, value)
            try:
                s.commit()
                return
            except IntegrityError:
                s.rollback()
                if attempt:
                    raise


@feedback_bp.post("/me/feedback/seen")
async def mark_seen():
    user_id = current_user_id()
    body = await json_body()
    request_id = _int(body.get("request_id"), "request_id")
    await asyncio.to_thread(_write_receipt, user_id, request_id, seen_at=datetime.now())
    return private_no_store(jsonify({"ok": True}))


@feedback_bp.post("/me/feedback/snooze")
async def snooze():
    user_id = current_user_id()
    body = await json_body()
    request_id = _int(body.get("request_id"), "request_id")
    now = datetime.now()
    if body.get("forever"):
        changes = {"declined_at": now}
    else:
        changes = {"snoozed_until": now + SNOOZE}
    await asyncio.to_thread(_write_receipt, user_id, request_id, **changes)
    return private_no_store(jsonify({"ok": True}))


def validate_answer(body: dict) -> dict:
    """Shape and limits of an answer. Group ownership is checked separately
    (it needs the database)."""
    rating = _int(body.get("rating"), "rating")
    if not (RATING_MIN <= rating <= RATING_MAX):
        abort_problem(422, "Invalid rating", f"Pick a score from {RATING_MIN} to {RATING_MAX}.")
    out = {"rating": rating}
    for key, label in (("liked_text", "What's working best"), ("improve_text", "What would you change")):
        text = str(body.get(key) or "").strip()
        if len(text) > TEXT_MAX:
            abort_problem(422, "Too long", f"'{label}' must be {TEXT_MAX} characters or fewer.")
        out[key] = text or None
    attribution = body.get("attribution") or "clan"
    if attribution not in ATTRIBUTIONS:
        abort_problem(422, "Invalid attribution", "attribution must be clan, full or anon.")
    out["attribution"] = attribution
    out["quote_consent"] = bool(body.get("quote_consent"))
    out["contact_ok"] = bool(body.get("contact_ok"))
    out["request_id"] = _int(body.get("request_id"), "request_id", required=False)
    out["group_id"] = _int(body.get("group_id"), "group_id", required=False)
    return out


def _raw_post(s, resp: FeedbackResponse, *, actor_user_id=None) -> None:
    """Queue the staff copy of an answer for the raw Discord channel."""
    from services import feedback_core as core
    from services.discord_outbox import enqueue

    user = load_user(s, resp.user_id)
    group = None
    if resp.group_id is not None:
        group = s.query(Group).filter(Group.group_id == resp.group_id).first()
    tier_name = None
    if resp.group_tier:
        tier_name = (s.query(SubscriptionTier.name).filter(SubscriptionTier.key == resp.group_tier).scalar()
                     or ("Free" if resp.group_tier == FREE_TIER else resp.group_tier))
    source = None
    if resp.request_id is not None:
        source = s.query(FeedbackRequest.title).filter(FeedbackRequest.id == resp.request_id).scalar()
    embed = core.raw_embed(
        response_id=resp.id, rating=int(resp.rating), liked_text=resp.liked_text,
        improve_text=resp.improve_text, quote_consent=bool(resp.quote_consent),
        attribution=resp.attribution, contact_ok=bool(resp.contact_ok),
        username=user.username if user else None, user_id=resp.user_id,
        discord_id=user.discord_id if user else None,
        group_name=group.group_name if group else None, role=resp.role, tier_name=tier_name,
        member_count=resp.member_count, group_since=group.date_added if group else None,
        source=source,
    )
    enqueue(
        s, channel_id=str(core.RAW_CHANNEL_ID), embed=embed,
        components=core.raw_buttons(resp.id, core.is_quotable(resp.quote_consent, resp.liked_text)),
        kind="message", ref_type="feedback_response", ref_id=resp.id,
        actor_user_id=actor_user_id, commit=False,
    )


@feedback_bp.post("/me/feedback")
async def submit():
    user_id = current_user_id()
    answer = validate_answer(await json_body())

    def _save():
        now = datetime.now()
        with db_session() as s:
            recent = (s.query(func.count(FeedbackResponse.id))
                      .filter(FeedbackResponse.user_id == user_id,
                              FeedbackResponse.created_at > now - timedelta(hours=1)).scalar() or 0)
            if recent >= HOURLY_LIMIT:
                abort_problem(429, "Slow down", "That's a lot of feedback for one hour. Try again later.")

            request_id = answer["request_id"]
            if request_id is not None:
                if s.query(FeedbackRequest.id).filter(FeedbackRequest.id == request_id).first() is None:
                    request_id = None  # ended and deleted: keep the answer anyway
                else:
                    receipt = _receipt(s, request_id, user_id)
                    if receipt.responded_at is not None:
                        abort_problem(409, "Already answered", "You've already answered this one. Thanks!")

            group = None
            if answer["group_id"] is not None:
                group = next((g for g in leader_groups(s, user_id)
                              if g["group_id"] == answer["group_id"]), None)
                if group is None:
                    abort_problem(422, "Invalid clan", "You can only answer for a clan you own or admin.")

            resp = FeedbackResponse(
                request_id=request_id, user_id=user_id,
                group_id=group["group_id"] if group else None,
                role=group["role"] if group else None,
                group_tier=group["tier"] if group else None,
                member_count=group["member_count"] if group else None,
                rating=answer["rating"], liked_text=answer["liked_text"],
                improve_text=answer["improve_text"], quote_consent=answer["quote_consent"],
                attribution=answer["attribution"], contact_ok=answer["contact_ok"],
                status="new",
            )
            s.add(resp)
            if request_id is not None:
                receipt.responded_at = now
                if receipt.seen_at is None:
                    receipt.seen_at = now
            s.commit()
            # The answer is saved; Discord is best effort and never fails it.
            try:
                _raw_post(s, resp, actor_user_id=user_id)
                s.commit()
            except Exception:
                s.rollback()
                logger.exception("feedback: could not queue the Discord post for #%s", resp.id)
            return int(resp.id)

    rid = await asyncio.to_thread(_save)
    return private_no_store(jsonify({"ok": True, "id": rid}))


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def _assert_admin(s, actor: int) -> None:
    assert_superadmin(load_user(s, actor))


def _validate_audience(s, raw) -> list[dict]:
    from web_api.routes.popup_notices import _known_tiers

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


def _title(body: dict) -> str:
    title = str(body.get("title") or "").strip()
    if not (1 <= len(title) <= TITLE_MAX):
        abort_problem(422, "Invalid name", f"Name must be 1-{TITLE_MAX} characters.")
    return title


def _labels(s, rule_sets) -> dict:
    from web_api.routes.popup_notices import _labels as notice_labels

    return notice_labels(s, rule_sets)


def _summary(rules, labels) -> str:
    names = {("group", int(k)): v for k, v in labels["groups"].items()}
    names.update({("tier", k): v for k, v in labels["tiers"].items()})
    return describe_audience(rules, names) if rules else "Nobody"


def _request_stats(s, ids) -> dict:
    if not ids:
        return {}
    out = {i: {"seen": 0, "answered": 0, "snoozed": 0, "declined": 0} for i in ids}
    now = datetime.now()
    for r in s.query(FeedbackReceipt).filter(FeedbackReceipt.request_id.in_(list(ids))):
        st = out[int(r.request_id)]
        if r.seen_at:
            st["seen"] += 1
        if r.responded_at:
            st["answered"] += 1
        elif r.declined_at:
            st["declined"] += 1
        elif r.snoozed_until and r.snoozed_until > now:
            st["snoozed"] += 1
    avg = dict(s.query(FeedbackResponse.request_id, func.avg(FeedbackResponse.rating))
               .filter(FeedbackResponse.request_id.in_(list(ids)))
               .group_by(FeedbackResponse.request_id).all())
    for rid, value in avg.items():
        out[int(rid)]["average"] = round(float(value), 1) if value is not None else None
    return out


def _request_payload(r: FeedbackRequest, stats: dict, labels: dict) -> dict:
    rules = parse_stored_audience(r.audience_json)
    st = stats.get(r.id, {})
    return {
        "id": r.id,
        "title": r.title,
        "audience": rules,
        "audience_summary": _summary(rules, labels),
        "status": r.status,
        "audience_estimate": r.audience_estimate,
        "sent_at": _ts(r.sent_at),
        "ended_at": _ts(r.ended_at),
        "created_at": _ts(r.created_at),
        "seen_count": st.get("seen", 0),
        "answered_count": st.get("answered", 0),
        "snoozed_count": st.get("snoozed", 0),
        "declined_count": st.get("declined", 0),
        "average_rating": st.get("average"),
    }


def _response_payloads(s, rows) -> list[dict]:
    user_ids = {r.user_id for r in rows}
    group_ids = {r.group_id for r in rows if r.group_id is not None}
    request_ids = {r.request_id for r in rows if r.request_id is not None}
    users = {int(u): name for u, name in
             s.query(User.user_id, User.username).filter(User.user_id.in_(user_ids))} if user_ids else {}
    groups = {int(g): name for g, name in
              s.query(Group.group_id, Group.group_name).filter(Group.group_id.in_(group_ids))} if group_ids else {}
    titles = {int(i): t for i, t in
              s.query(FeedbackRequest.id, FeedbackRequest.title)
              .filter(FeedbackRequest.id.in_(request_ids))} if request_ids else {}
    return [{
        "id": r.id,
        "request_id": r.request_id,
        "request_title": titles.get(r.request_id) if r.request_id is not None else None,
        "user_id": r.user_id,
        "username": users.get(r.user_id) or f"User {r.user_id}",
        "group_id": r.group_id,
        "group_name": groups.get(r.group_id) if r.group_id is not None else None,
        "role": r.role,
        "group_tier": r.group_tier,
        "member_count": r.member_count,
        "rating": int(r.rating),
        "liked_text": r.liked_text,
        "improve_text": r.improve_text,
        "quote_consent": bool(r.quote_consent),
        "attribution": r.attribution,
        "contact_ok": bool(r.contact_ok),
        "status": r.status,
        "posted": r.raw_message_id is not None,
        "published_at": _ts(r.published_at),
        "created_at": _ts(r.created_at),
    } for r in rows]


def _one_request(s, request_id: int) -> FeedbackRequest:
    r = s.query(FeedbackRequest).filter(FeedbackRequest.id == request_id).first()
    if r is None:
        abort_problem(404, "Not found", f"No feedback request {request_id}.")
    return r


def _full_request(s, r: FeedbackRequest) -> dict:
    rules = parse_stored_audience(r.audience_json)
    labels = _labels(s, [rules])
    out = _request_payload(r, _request_stats(s, [r.id]), labels)
    out["labels"] = labels
    return out


def _go_live(s, r: FeedbackRequest) -> None:
    rules = parse_stored_audience(r.audience_json)
    if not rules:
        abort_problem(422, "Invalid audience", "Choose who to ask first.")
    ids, _notes = resolve_audience_user_ids(s, rules)
    r.audience_estimate = (len(ids) if ids is not None
                           else int(s.query(func.count(User.user_id)).scalar() or 0))
    r.status = "live"
    r.sent_at = datetime.now()


@feedback_bp.get("/admin/feedback")
async def admin_overview():
    actor = current_user_id()

    def _load():
        with db_session() as s:
            _assert_admin(s, actor)
            requests = s.query(FeedbackRequest).order_by(FeedbackRequest.id.desc()).all()
            rule_sets = [parse_stored_audience(r.audience_json) for r in requests]
            labels = _labels(s, rule_sets)
            stats = _request_stats(s, [r.id for r in requests])
            responses = (s.query(FeedbackResponse).order_by(FeedbackResponse.id.desc())
                         .limit(ADMIN_RESPONSES_MAX).all())
            return {
                "requests": [_request_payload(r, stats, labels) for r in requests],
                "responses": _response_payloads(s, responses),
                "labels": labels,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@feedback_bp.post("/admin/feedback/requests")
async def admin_create():
    actor = current_user_id()
    body = await json_body()

    def _save():
        with db_session() as s:
            _assert_admin(s, actor)
            title = _title(body)
            rules = _validate_audience(s, body.get("audience"))
            r = FeedbackRequest(title=title, audience_json=json.dumps(rules), status="draft",
                                created_by=actor)
            s.add(r)
            s.flush()
            if body.get("send"):
                _go_live(s, r)
            s.commit()
            return _full_request(s, r)

    out = await asyncio.to_thread(_save)
    cache_delete(_LIVE_KEY)
    _audit(actor, "feedback.request.create", f"feedback_requests:{out['id']}", after=out["status"])
    return private_no_store(jsonify(out))


@feedback_bp.patch("/admin/feedback/requests/<int:request_id>")
async def admin_update(request_id: int):
    actor = current_user_id()
    body = await json_body()

    def _save():
        with db_session() as s:
            _assert_admin(s, actor)
            r = _one_request(s, request_id)
            if "title" in body:
                r.title = _title(body)
            if "audience" in body:
                r.audience_json = json.dumps(_validate_audience(s, body.get("audience")))
            s.commit()
            return _full_request(s, r)

    out = await asyncio.to_thread(_save)
    cache_delete(_LIVE_KEY)
    return private_no_store(jsonify(out))


@feedback_bp.post("/admin/feedback/requests/<int:request_id>/send")
async def admin_send(request_id: int):
    actor = current_user_id()

    def _save():
        with db_session() as s:
            _assert_admin(s, actor)
            r = _one_request(s, request_id)
            if r.status != "draft":
                abort_problem(409, "Already sent", "Only a draft can be sent.")
            _go_live(s, r)
            s.commit()
            return _full_request(s, r)

    out = await asyncio.to_thread(_save)
    cache_delete(_LIVE_KEY)
    _audit(actor, "feedback.request.send", f"feedback_requests:{request_id}")
    return private_no_store(jsonify(out))


@feedback_bp.post("/admin/feedback/requests/<int:request_id>/end")
async def admin_end(request_id: int):
    actor = current_user_id()

    def _save():
        with db_session() as s:
            _assert_admin(s, actor)
            r = _one_request(s, request_id)
            if r.status != "live":
                abort_problem(409, "Not live", "Only a live request can be ended.")
            r.status = "ended"
            r.ended_at = datetime.now()
            s.commit()
            return _full_request(s, r)

    out = await asyncio.to_thread(_save)
    cache_delete(_LIVE_KEY)
    _audit(actor, "feedback.request.end", f"feedback_requests:{request_id}")
    return private_no_store(jsonify(out))


@feedback_bp.delete("/admin/feedback/requests/<int:request_id>")
async def admin_delete(request_id: int):
    """Deletes the request and who-was-asked receipts. Answers are kept."""
    actor = current_user_id()

    def _save():
        with db_session() as s:
            _assert_admin(s, actor)
            r = _one_request(s, request_id)
            title = r.title
            s.query(FeedbackResponse).filter(FeedbackResponse.request_id == request_id).update(
                {"request_id": None}, synchronize_session=False)
            s.query(FeedbackReceipt).filter(FeedbackReceipt.request_id == request_id).delete(
                synchronize_session=False)
            s.delete(r)
            s.commit()
            return title

    title = await asyncio.to_thread(_save)
    cache_delete(_LIVE_KEY)
    _audit(actor, "feedback.request.delete", f"feedback_requests:{request_id}", before=title)
    return private_no_store(jsonify({"ok": True}))


@feedback_bp.post("/admin/feedback/responses/<int:response_id>/repost")
async def admin_repost(response_id: int):
    """Post an answer to the raw channel again (the first post failed or was
    deleted). The old post's buttons keep working; status lives in the row."""
    actor = current_user_id()

    def _save():
        with db_session() as s:
            _assert_admin(s, actor)
            resp = s.query(FeedbackResponse).filter(FeedbackResponse.id == response_id).first()
            if resp is None:
                abort_problem(404, "Not found", f"No feedback answer {response_id}.")
            _raw_post(s, resp, actor_user_id=actor)
            s.commit()

    await asyncio.to_thread(_save)
    return private_no_store(jsonify({"ok": True}))
