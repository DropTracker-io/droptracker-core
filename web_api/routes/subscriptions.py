"""Task 11 — recurring subscriptions (group upgrades + user supporter).

  GET  /api/v1/subscriptions/tiers                          (public, cached; ?scope=group|user)
  GET  /api/v1/groups/{id}/subscription                     (group admin)
  POST /api/v1/groups/{id}/subscription/checkout            (group admin) {tier_key}
  POST /api/v1/groups/{id}/subscription/cancel              (group admin)
  POST /api/v1/groups/{id}/subscription/resume              (group admin)
  POST /api/v1/groups/{id}/subscription/portal              (group admin)
  GET  /api/v1/users/me/subscription                        (authed)
  POST /api/v1/users/me/subscription/checkout               (authed) {tier_key}
  POST /api/v1/users/me/subscription/cancel                 (authed)
  POST /api/v1/users/me/subscription/resume                 (authed)
  POST /api/v1/users/me/subscription/portal                 (authed)
  POST /api/v1/webhooks/billing                             (provider-signed)

Provider lifecycle lives in ``web_api/billing.py`` (Stripe + manual fallback);
the provider webhook is the source of truth for status/period.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from quart import Blueprint, jsonify, request
from sqlalchemy import func

from db import Group, GroupSubscription, Player, SubscriptionTier, User, UserSubscription
from db.models.associations import user_group_association
from db.entitlements import (
    NITRO_PROVIDER,
    effective_group_subscription,
    effective_group_tiers,
    paid_group_tiers_desc,
    subscription_is_live,
)
from services.nitro_attribution import NITRO_BOOST_CENTS
from web_api import billing
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.entitlements import resolve_group_entitlements, resolve_user_entitlements
from web_api.entitlements_registry import (
    parse_stored_entitlements,
    resolve_tier_entitlements,
)
from web_api.payments import record_payment
from web_api.tier_flair import normalize_flair
from web_api.deps import (
    assert_group_admin,
    assert_group_member,
    current_user_id,
    is_group_admin_role,
    json_body,
    load_user,
    manageable_guild_ids,
    resolve_group_role,
)

subscriptions_bp = Blueprint("v1_subscriptions", __name__)


def _serialize_tier(t: SubscriptionTier) -> dict:
    try:
        features = json.loads(t.features) if t.features else []
    except Exception:
        features = []
    scope = getattr(t, "scope", None) or "group"
    stored = parse_stored_entitlements(t.entitlements, scope)
    return {
        "key": t.key,
        "name": t.name,
        "description": t.description or "",
        "scope": scope,
        "price_cents": int(t.price_cents or 0),
        "currency": t.currency or "USD",
        "interval": t.interval or "month",
        "features": features if isinstance(features, list) else [],
        "entitlements": resolve_tier_entitlements(stored, scope),
        "flair": normalize_flair(getattr(t, "flair", None)),
        "recommended": bool(t.recommended),
    }


def _serialize_sub(group_id: int, sub: GroupSubscription | None, entitlements: dict | None = None) -> dict:
    """Serialize ONE leg row (admin comp/audit payloads). The group-level
    effective view is ``_serialize_group_sub``."""
    if sub is None or sub.status == "none":
        base = {
            "group_id": group_id,
            "tier_key": None,
            "status": "none",
            "provider": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
        }
        if entitlements is not None:
            base["entitlements"] = entitlements
        return base
    payload = {
        "group_id": group_id,
        "tier_key": sub.tier_key,
        "status": sub.status,
        "provider": sub.provider,
        "current_period_end": int(sub.current_period_end.timestamp())
        if sub.current_period_end
        else None,
        "cancel_at_period_end": bool(sub.cancel_at_period_end),
    }
    if entitlements is not None:
        payload["entitlements"] = entitlements
    return payload


def _leg_payload(leg: GroupSubscription, names: dict, viewer_user_id: int | None) -> dict:
    return {
        "id": int(leg.id),
        "user_id": int(leg.user_id) if leg.user_id is not None else None,
        "user_name": names.get(leg.user_id),
        "tier_key": leg.tier_key,
        "amount_cents": int(leg.amount_cents) if leg.amount_cents is not None else None,
        "provider": leg.provider,
        "status": leg.status,
        "current_period_end": int(leg.current_period_end.timestamp())
        if leg.current_period_end
        else None,
        "cancel_at_period_end": bool(leg.cancel_at_period_end),
        "mine": viewer_user_id is not None and leg.user_id == viewer_user_id,
    }


def _serialize_group_sub(
    s,
    group_id: int,
    entitlements: dict | None = None,
    viewer_user_id: int | None = None,
) -> dict:
    """Effective pool view: computed tier + per-leg breakdown.

    ``tier_key`` is the tier the group's live legs collectively cover — the
    same resolution entitlements and flair use. ``provider`` is kept for
    payload compat: the single distinct live-leg provider, or null when mixed.
    """
    resolved = effective_group_subscription(s, group_id)
    tier = resolved["tier"]
    live_providers = {leg.provider for leg in resolved["live_legs"] if leg.provider}
    payload = {
        "group_id": group_id,
        "tier_key": tier.key if tier is not None else None,
        "status": resolved["status"],
        "provider": next(iter(live_providers)) if len(live_providers) == 1 else None,
        "current_period_end": int(resolved["current_period_end"].timestamp())
        if resolved["current_period_end"]
        else None,
        "cancel_at_period_end": bool(resolved["cancel_at_period_end"]),
        "total_monthly_cents": int(resolved["total_monthly_cents"]),
    }
    payer_ids = [leg.user_id for leg in resolved["legs"] if leg.user_id is not None]
    names: dict = {}
    if payer_ids:
        rows = s.query(User.user_id, User.username).filter(User.user_id.in_(payer_ids)).all()
        names = {uid: name for uid, name in rows}
    payload["legs"] = [_leg_payload(leg, names, viewer_user_id) for leg in resolved["legs"]]
    # Nitro-boost contribution summary: how many of the group's members boost
    # the DropTracker Discord and how much monthly pool credit that adds.
    nitro_leg = next(
        (
            leg
            for leg in resolved["live_legs"]
            if leg.provider == NITRO_PROVIDER and leg.amount_cents
        ),
        None,
    )
    if nitro_leg is not None and NITRO_BOOST_CENTS > 0:
        payload["nitro"] = {
            "booster_count": int(nitro_leg.amount_cents) // NITRO_BOOST_CENTS,
            "monthly_cents": int(nitro_leg.amount_cents),
            "per_boost_cents": NITRO_BOOST_CENTS,
        }
    else:
        payload["nitro"] = None
    if entitlements is not None:
        payload["entitlements"] = entitlements
    return payload


@subscriptions_bp.get("/subscriptions/tiers")
async def list_tiers():
    # Default to group tiers so pre-scope clients never see user tiers as
    # group checkout options. ?scope=user for supporter tiers, ?scope=all for both.
    scope = request.args.get("scope", "group")
    if scope not in ("group", "user", "all"):
        abort_problem(422, "Invalid scope", "scope must be 'group', 'user' or 'all'.")
    # Free ($0) tiers are the implicit fallback plan for unsubscribed groups
    # (see db/entitlements._FALLBACK_TIER_KEYS), not a purchasable product —
    # hide them from public listings by default so they never render as a
    # checkout option. Admin surfaces (event limits, tier manager) opt in with
    # ?include_free=1 to configure them.
    include_free = request.args.get("include_free", "").lower() in ("1", "true", "yes")

    def _load():
        with db_session() as s:
            q = s.query(SubscriptionTier).filter(SubscriptionTier.active == True)  # noqa: E712
            if scope != "all":
                q = q.filter(SubscriptionTier.scope == scope)
            if not include_free:
                q = q.filter(SubscriptionTier.price_cents > 0)
            tiers = q.order_by(SubscriptionTier.price_cents.asc()).all()
            return [_serialize_tier(t) for t in tiers]

    tiers = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(tiers), max_age=300)


# --------------------------------------------------------------------------- #
# Public "wall of supporters" — groups and individuals with a live PAID
# subscription, shown on the homepage to thank the people funding the project.
# No personal data beyond public names; hidden users/players are excluded, and
# complimentary badge grants (no payment) are excluded by construction (they
# have no subscription row).
# --------------------------------------------------------------------------- #
# Only these statuses can be live; pre-filtering avoids loading canceled rows.
_SUPPORTER_QUERY_STATUSES = ("active", "trialing")


def _load_group_supporters(s, limit: int) -> list[dict]:
    """Groups whose live contribution pool covers a paid tier, most recent
    supporter first. Flair is attached when the covered tier grants one."""
    active_legs = (
        s.query(GroupSubscription)
        .filter(GroupSubscription.status.in_(_SUPPORTER_QUERY_STATUSES))
        .all()
    )
    live_legs = [leg for leg in active_legs if subscription_is_live(leg)]
    if not live_legs:
        return []
    candidate_ids = {int(leg.group_id) for leg in live_legs}
    # {group_id: (tier, total_monthly_cents)} for the paid-tier subset.
    effective = effective_group_tiers(s, list(candidate_ids))
    if not effective:
        return []

    # Recency: newest live leg per qualifying group (when the pool last grew).
    since: dict[int, datetime] = {}
    for leg in live_legs:
        gid = int(leg.group_id)
        if gid not in effective or not leg.created_at:
            continue
        if gid not in since or leg.created_at > since[gid]:
            since[gid] = leg.created_at

    ids = list(effective.keys())
    names = dict(
        s.query(Group.group_id, Group.group_name).filter(Group.group_id.in_(ids)).all()
    )
    member_counts = dict(
        s.query(
            user_group_association.c.group_id,
            func.count(user_group_association.c.player_id),
        )
        .filter(user_group_association.c.group_id.in_(ids))
        .group_by(user_group_association.c.group_id)
        .all()
    )

    rows: list[dict] = []
    for gid, (tier, _total) in effective.items():
        row = {
            "id": gid,
            "name": names.get(gid) or f"Group {gid}",
            "tier_name": tier.name,
            "member_count": int(member_counts.get(gid, 0)),
            "since": int(since[gid].timestamp()) if gid in since else None,
        }
        style = normalize_flair(getattr(tier, "flair", None))
        if style != "none":
            row["flair"] = {"tier_key": tier.key, "tier_name": tier.name, "style": style}
        rows.append(row)

    rows.sort(key=lambda r: (r["since"] or 0), reverse=True)
    return rows[:limit]


def _load_user_supporters(s, limit: int) -> list[dict]:
    """Individual supporters with a live paid user subscription, most recent
    first. Each is represented by a visible linked player (privacy: hidden
    users and hidden players are excluded); supporters with no visible player
    to link to are omitted."""
    subs = (
        s.query(UserSubscription)
        .filter(UserSubscription.status.in_(_SUPPORTER_QUERY_STATUSES))
        .order_by(UserSubscription.created_at.desc())
        .all()
    )
    live = [sub for sub in subs if subscription_is_live(sub)]
    if not live:
        return []
    user_ids = [int(sub.user_id) for sub in live]

    users = {u.user_id: u for u in s.query(User).filter(User.user_id.in_(user_ids)).all()}
    # Representative visible player per user: highest total level wins, then
    # lowest player_id for a stable tie-break. MySQL sorts NULL levels last.
    rep: dict[int, tuple[int, str]] = {}
    player_rows = (
        s.query(Player.player_id, Player.player_name, Player.user_id)
        .filter(
            Player.user_id.in_(user_ids),
            Player.hidden == False,  # noqa: E712
            Player.player_name.isnot(None),
        )
        .order_by(Player.total_level.desc(), Player.player_id.asc())
        .all()
    )
    for pid, pname, uid in player_rows:
        rep.setdefault(int(uid), (int(pid), pname))

    rows: list[dict] = []
    for sub in live:  # preserves created_at-desc ordering
        uid = int(sub.user_id)
        user = users.get(uid)
        if user is None or bool(user.hidden):
            continue
        rep_player = rep.get(uid)
        if rep_player is None:
            continue  # nothing public to link to
        rows.append(
            {
                "user_id": uid,
                "player_id": rep_player[0],
                "name": rep_player[1],
                "since": int(sub.created_at.timestamp()) if sub.created_at else None,
            }
        )
        if len(rows) >= limit:
            break
    return rows


@subscriptions_bp.get("/supporters")
async def list_supporters():
    """Public homepage "supporters" section: paid subscriber groups + players."""
    try:
        limit = int(request.args.get("limit", 60))
    except (TypeError, ValueError):
        limit = 60
    limit = max(1, min(limit, 200))

    def _load():
        with db_session() as s:
            return {
                "groups": _load_group_supporters(s, limit),
                "players": _load_user_supporters(s, limit),
            }

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=300)


def _require_admin_and_sub(user_id, group_id):
    with db_session() as s:
        user = load_user(s, user_id)
        assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
        entitlements = resolve_group_entitlements(s, group_id, user=user)
        return _serialize_group_sub(
            s, group_id, entitlements=entitlements, viewer_user_id=user_id
        )


@subscriptions_bp.get("/groups/<int:group_id>/subscription")
async def get_subscription(group_id: int):
    user_id = current_user_id()
    payload = await asyncio.to_thread(_require_admin_and_sub, user_id, group_id)
    return private_no_store(jsonify(payload))


@subscriptions_bp.get("/groups/<int:group_id>/subscription/summary")
async def subscription_summary(group_id: int):
    """Public pool summary for the group page "Support this clan" card.

    No personal data: effective tier, pool total, and what the next tier
    costs on top of the current pool.
    """

    def _load():
        with db_session() as s:
            resolved = effective_group_subscription(s, group_id)
            tier = resolved["tier"]
            total = int(resolved["total_monthly_cents"])
            covered = int(tier.price_cents) if tier is not None else 0
            # Cheapest paid tier the pool does NOT yet cover.
            next_tier = None
            for t in sorted(paid_group_tiers_desc(s), key=lambda t: int(t.price_cents)):
                if int(t.price_cents) > covered:
                    next_tier = {
                        "key": t.key,
                        "name": t.name,
                        "price_cents": int(t.price_cents),
                        "delta_cents": max(0, int(t.price_cents) - total),
                    }
                    break
            return {
                "group_id": group_id,
                "tier_key": tier.key if tier is not None else None,
                "tier_name": tier.name if tier is not None else None,
                "total_monthly_cents": total,
                "next_tier": next_tier,
            }

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=60)


@subscriptions_bp.post("/groups/<int:group_id>/subscription/checkout")
async def checkout(group_id: int):
    """Add a contribution leg toward ``tier_key``.

    Pool model: any group MEMBER may contribute; the charge is the difference
    between the target tier's monthly price and the group's current live pool
    total. 422 when the pool already covers the tier.
    """
    user_id = current_user_id()
    body = await json_body()
    tier_key = body.get("tier_key")
    if not tier_key:
        abort_problem(422, "Missing tier_key", "'tier_key' is required.")

    def _prepare():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_member(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            tier = (
                s.query(SubscriptionTier)
                .filter(
                    SubscriptionTier.key == tier_key,
                    SubscriptionTier.scope == "group",
                    SubscriptionTier.active == True,  # noqa: E712
                )
                .first()
            )
            if not tier:
                abort_problem(404, "Unknown tier", f"No active group tier '{tier_key}'.")
            resolved = effective_group_subscription(s, group_id)
            delta = int(tier.price_cents) - int(resolved["total_monthly_cents"])
            if delta <= 0:
                abort_problem(
                    422,
                    "Tier already covered",
                    "The group's current contributions already cover this tier.",
                )
            # Detach a lightweight snapshot for the provider call.
            return {
                "key": tier.key,
                "interval": tier.interval,
                "provider_price_id": tier.provider_price_id,
                "currency": tier.currency,
                "delta_cents": delta,
            }

    tier_snapshot = await asyncio.to_thread(_prepare)

    # Provider call (may be network I/O for Stripe).
    class _T:  # minimal attr holder for billing.start_group_leg_checkout
        key = tier_snapshot["key"]
        interval = tier_snapshot["interval"]
        provider_price_id = tier_snapshot["provider_price_id"]
        currency = tier_snapshot["currency"]

    try:
        result = await asyncio.to_thread(
            billing.start_group_leg_checkout,
            group_id,
            user_id,
            _T(),
            tier_snapshot["delta_cents"],
        )
    except Exception as e:
        abort_problem(502, "Checkout failed", str(e))

    # Manual provider grants immediately — persist the new leg now.
    apply = result.get("apply")
    if apply:
        def _persist():
            with db_session() as s:
                leg = GroupSubscription(
                    group_id=group_id,
                    user_id=apply.get("user_id"),
                    tier_key=apply["tier_key"],
                    amount_cents=apply.get("amount_cents"),
                    status=apply["status"],
                    provider=apply["provider"],
                    current_period_end=apply["current_period_end"],
                    cancel_at_period_end=apply["cancel_at_period_end"],
                )
                s.add(leg)
                s.commit()

        await asyncio.to_thread(_persist)

    return jsonify({"url": result.get("url")})


def _resolve_leg(s, user_id: int, group_id: int, leg_id: int | None):
    """Load the leg a cancel/resume targets and enforce payer-or-admin auth.

    ``leg_id=None`` (legacy group-wide endpoints) targets the caller's own
    leg. Group admins may manage any leg; members only their own.
    """
    user = load_user(s, user_id)
    role = resolve_group_role(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
    if role is None:
        abort_problem(403, "Forbidden", "Group membership is required.")
    q = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id)
    if leg_id is not None:
        leg = q.filter(GroupSubscription.id == leg_id).first()
        if not leg or leg.status == "none":
            abort_problem(404, "No such contribution", "No matching contribution found.")
        if leg.user_id != user_id and not is_group_admin_role(role):
            abort_problem(403, "Forbidden", "You can only manage your own contribution.")
    else:
        leg = (
            q.filter(GroupSubscription.user_id == user_id)
            .order_by(GroupSubscription.created_at.desc())
            .first()
        )
        if not leg or leg.status == "none":
            abort_problem(404, "No subscription", "You have no contribution to this group.")
    return leg


async def _leg_action(group_id: int, leg_id: int | None, action):
    """Shared cancel/resume flow: resolve+authorize, provider call, persist."""
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            leg = _resolve_leg(s, user_id, group_id, leg_id)
            return leg.id, leg.provider_subscription_id

    resolved_leg_id, prov_sub_id = await asyncio.to_thread(_load)

    class _S:
        provider_subscription_id = prov_sub_id

    try:
        fields = await asyncio.to_thread(action, _S())
    except Exception as e:
        abort_problem(502, "Subscription update failed", str(e))

    def _persist():
        with db_session() as s:
            leg = s.query(GroupSubscription).filter(GroupSubscription.id == resolved_leg_id).first()
            leg.cancel_at_period_end = fields["cancel_at_period_end"]
            s.commit()
            return _serialize_group_sub(s, group_id, viewer_user_id=user_id)

    return jsonify(await asyncio.to_thread(_persist))


@subscriptions_bp.post("/groups/<int:group_id>/subscription/legs/<int:leg_id>/cancel")
async def cancel_leg(group_id: int, leg_id: int):
    return await _leg_action(group_id, leg_id, billing.cancel)


@subscriptions_bp.post("/groups/<int:group_id>/subscription/legs/<int:leg_id>/resume")
async def resume_leg(group_id: int, leg_id: int):
    return await _leg_action(group_id, leg_id, billing.resume)


# Legacy group-wide endpoints — now operate on the caller's own leg.
@subscriptions_bp.post("/groups/<int:group_id>/subscription/cancel")
async def cancel_subscription(group_id: int):
    return await _leg_action(group_id, None, billing.cancel)


@subscriptions_bp.post("/groups/<int:group_id>/subscription/resume")
async def resume_subscription(group_id: int):
    return await _leg_action(group_id, None, billing.resume)


@subscriptions_bp.post("/groups/<int:group_id>/subscription/portal")
async def portal(group_id: int):
    """Provider billing portal for the CALLER's own contribution leg."""
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_member(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            leg = (
                s.query(GroupSubscription)
                .filter(
                    GroupSubscription.group_id == group_id,
                    GroupSubscription.user_id == user_id,
                )
                .order_by(GroupSubscription.created_at.desc())
                .first()
            )
            return leg.provider_customer_id if leg else None

    customer_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_customer_id = customer_id

    url = await asyncio.to_thread(billing.billing_portal, group_id, _S())
    return jsonify({"url": url})


# --------------------------------------------------------------------------- #
# User supporter subscription — same lifecycle, scoped to the signed-in user.
# --------------------------------------------------------------------------- #
def _serialize_user_sub(user_id: int, sub: UserSubscription | None, entitlements: dict | None = None) -> dict:
    if sub is None or sub.status == "none":
        base = {
            "user_id": user_id,
            "tier_key": None,
            "status": "none",
            "provider": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
        }
        if entitlements is not None:
            base["entitlements"] = entitlements
        return base
    payload = {
        "user_id": user_id,
        "tier_key": sub.tier_key,
        "status": sub.status,
        "provider": sub.provider,
        "amount_cents": int(sub.amount_cents) if sub.amount_cents else None,
        "current_period_end": int(sub.current_period_end.timestamp())
        if sub.current_period_end
        else None,
        "cancel_at_period_end": bool(sub.cancel_at_period_end),
    }
    if entitlements is not None:
        payload["entitlements"] = entitlements
    return payload


def _load_user_sub(s, user_id: int) -> UserSubscription | None:
    return s.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()


@subscriptions_bp.get("/users/me/subscription")
async def get_my_subscription():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                abort_problem(401, "Not authenticated", "User not found for this session.")
            sub = _load_user_sub(s, user_id)
            entitlements = resolve_user_entitlements(s, user_id, user=user)
            return _serialize_user_sub(user_id, sub, entitlements=entitlements)

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


# Sanity ceiling for pay-what-you-want amounts ($1,000/month).
_PWYW_MAX_CENTS = 100_000


@subscriptions_bp.post("/users/me/subscription/checkout")
async def user_checkout():
    user_id = current_user_id()
    body = await json_body()
    tier_key = body.get("tier_key")
    if not tier_key:
        abort_problem(422, "Missing tier_key", "'tier_key' is required.")
    amount_cents = body.get("amount_cents")
    if amount_cents is not None and (
        isinstance(amount_cents, bool) or not isinstance(amount_cents, int)
    ):
        abort_problem(422, "Invalid amount", "'amount_cents' must be an integer.")

    def _prepare():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                abort_problem(401, "Not authenticated", "User not found for this session.")
            tier = (
                s.query(SubscriptionTier)
                .filter(
                    SubscriptionTier.key == tier_key,
                    SubscriptionTier.scope == "user",
                    SubscriptionTier.active == True,  # noqa: E712
                )
                .first()
            )
            if not tier:
                abort_problem(404, "Unknown tier", f"No active user tier '{tier_key}'.")
            minimum = int(tier.price_cents or 0)
            chosen = amount_cents if amount_cents is not None else minimum
            if chosen < minimum:
                abort_problem(
                    422,
                    "Amount below minimum",
                    f"The minimum for this tier is {minimum} cents per {tier.interval}.",
                )
            if chosen > _PWYW_MAX_CENTS:
                abort_problem(422, "Amount too large", f"The maximum is {_PWYW_MAX_CENTS} cents.")
            return {
                "key": tier.key,
                "interval": tier.interval,
                "provider_price_id": tier.provider_price_id,
                "currency": tier.currency,
                "price_cents": minimum,
                "amount_cents": chosen,
            }

    tier_snapshot = await asyncio.to_thread(_prepare)

    class _T:  # minimal attr holder for billing.start_user_checkout
        key = tier_snapshot["key"]
        interval = tier_snapshot["interval"]
        provider_price_id = tier_snapshot["provider_price_id"]
        currency = tier_snapshot["currency"]
        price_cents = tier_snapshot["price_cents"]

    try:
        result = await asyncio.to_thread(
            billing.start_user_checkout, user_id, _T(), None, tier_snapshot["amount_cents"]
        )
    except Exception as e:
        abort_problem(502, "Checkout failed", str(e))

    apply = result.get("apply")
    if apply:
        def _persist():
            with db_session() as s:
                sub = _load_user_sub(s, user_id)
                if not sub:
                    sub = UserSubscription(user_id=user_id)
                    s.add(sub)
                sub.tier_key = apply["tier_key"]
                sub.status = apply["status"]
                sub.provider = apply["provider"]
                sub.current_period_end = apply["current_period_end"]
                sub.cancel_at_period_end = apply["cancel_at_period_end"]
                sub.amount_cents = apply.get("amount_cents")
                s.commit()

        await asyncio.to_thread(_persist)
        _invalidate_user_entitlements(user_id)

    return jsonify({"url": result.get("url")})


@subscriptions_bp.post("/users/me/subscription/cancel")
async def user_cancel_subscription():
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "You have no active supporter subscription.")
            return sub.provider_subscription_id

    prov_sub_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_subscription_id = prov_sub_id

    try:
        fields = await asyncio.to_thread(billing.cancel, _S())
    except Exception as e:
        abort_problem(502, "Cancel failed", str(e))

    def _persist():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            sub.cancel_at_period_end = fields["cancel_at_period_end"]
            s.commit()
            return _serialize_user_sub(user_id, sub)

    return jsonify(await asyncio.to_thread(_persist))


@subscriptions_bp.post("/users/me/subscription/resume")
async def user_resume_subscription():
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "You have no supporter subscription.")
            return sub.provider_subscription_id

    prov_sub_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_subscription_id = prov_sub_id

    try:
        fields = await asyncio.to_thread(billing.resume, _S())
    except Exception as e:
        abort_problem(502, "Resume failed", str(e))

    def _persist():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            sub.cancel_at_period_end = fields["cancel_at_period_end"]
            s.commit()
            return _serialize_user_sub(user_id, sub)

    return jsonify(await asyncio.to_thread(_persist))


@subscriptions_bp.post("/users/me/subscription/portal")
async def user_portal():
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            return sub.provider_customer_id if sub else None

    customer_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_customer_id = customer_id

    url = await asyncio.to_thread(billing.user_billing_portal, _S())
    return jsonify({"url": url})


def _invalidate_user_entitlements(user_id: int | None) -> None:
    """Best-effort in-process cache bust after a subscription change."""
    try:
        from db.entitlements import invalidate_user_entitlement_cache

        invalidate_user_entitlement_cache(user_id)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Provider webhook — source of truth for status/period (no client trust).
# --------------------------------------------------------------------------- #
@subscriptions_bp.post("/webhooks/billing")
async def billing_webhook():
    raw = await request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    event = billing.verify_webhook(raw, signature)
    if event is None:
        abort_problem(400, "Invalid webhook", "Signature verification failed or unconfigured.")

    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    def _extract_amount_cents() -> int | None:
        """Best-effort recurring amount from Stripe payloads (PWYW capture)."""
        try:
            if etype == "checkout.session.completed":
                total = obj.get("amount_total")
                return int(total) if total else None
            if str(etype).startswith("customer.subscription."):
                items = ((obj.get("items") or {}).get("data")) or []
                if items:
                    price = items[0].get("price") or {}
                    amt = price.get("unit_amount")
                    if amt:
                        return int(amt)
                plan = obj.get("plan") or {}
                amt = plan.get("amount")
                return int(amt) if amt else None
        except Exception:
            pass
        return None

    def _apply_fields(sub, meta: dict):
        if etype == "checkout.session.completed":
            sub.provider = "stripe"
            sub.status = "active"
            sub.provider_customer_id = obj.get("customer")
            sub.provider_subscription_id = obj.get("subscription")
            if meta.get("tier_key"):
                sub.tier_key = meta["tier_key"]
        elif etype in ("customer.subscription.updated", "customer.subscription.created"):
            sub.status = obj.get("status", sub.status)
            sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
            cpe = obj.get("current_period_end")
            if not cpe:
                # Stripe API 2025-03+ moved current_period_end off the
                # Subscription object onto its items.
                items = ((obj.get("items") or {}).get("data")) or []
                if items:
                    cpe = items[0].get("current_period_end")
            if cpe:
                sub.current_period_end = datetime.fromtimestamp(int(cpe))
        elif etype == "customer.subscription.deleted":
            sub.status = "canceled"
        elif etype == "invoice.payment_failed":
            sub.status = "past_due"

    def _provider_subscription_id() -> str | None:
        """The Stripe subscription id this event concerns, across shapes."""
        if etype == "checkout.session.completed":
            return obj.get("subscription")
        if str(etype).startswith("customer.subscription."):
            return obj.get("id")
        if str(etype).startswith("invoice."):
            sub_id = obj.get("subscription")
            if not sub_id:
                # Stripe API 2025-03+: invoices reference the subscription via
                # parent.subscription_details.
                details = (obj.get("parent") or {}).get("subscription_details") or {}
                sub_id = details.get("subscription")
            return sub_id
        return None

    def _record_ledger():
        """invoice.paid / charge.refunded → subscription_payments rows.

        Idempotent via the unique external_id; unmatched events are skipped
        (they belong to some other product on the same Stripe account).
        """
        if etype in ("invoice.paid", "invoice.payment_succeeded"):
            amount = obj.get("amount_paid")
            prov_sub = _provider_subscription_id()
            if not amount or not prov_sub:
                return
            paid_ts = ((obj.get("status_transitions") or {}).get("paid_at")) or obj.get("created")
            paid_at = datetime.fromtimestamp(int(paid_ts)) if paid_ts else None
            with db_session() as s:
                leg = (
                    s.query(GroupSubscription)
                    .filter(GroupSubscription.provider_subscription_id == prov_sub)
                    .first()
                )
                if leg is not None:
                    fields = {
                        "scope": "group",
                        "group_id": leg.group_id,
                        "user_id": leg.user_id,
                        "subscription_id": leg.id,
                        "tier_key": leg.tier_key,
                    }
                else:
                    usub = (
                        s.query(UserSubscription)
                        .filter(UserSubscription.provider_subscription_id == prov_sub)
                        .first()
                    )
                    if usub is None:
                        return
                    fields = {
                        "scope": "user",
                        "user_id": usub.user_id,
                        "subscription_id": usub.id,
                        "tier_key": usub.tier_key,
                    }
            record_payment(
                provider="stripe",
                amount_cents=int(amount),
                currency=obj.get("currency") or "USD",
                external_id=str(obj.get("id")),
                paid_at=paid_at,
                notify=True,
                **fields,
            )
        elif etype == "charge.refunded":
            amount = obj.get("amount_refunded")
            invoice_id = obj.get("invoice")
            if not amount or not invoice_id:
                return
            # Attribute the refund via the original invoice's ledger row.
            from db import SubscriptionPayment

            with db_session() as s:
                original = (
                    s.query(SubscriptionPayment)
                    .filter(SubscriptionPayment.external_id == str(invoice_id))
                    .first()
                )
                if original is None:
                    return
                fields = {
                    "scope": original.scope,
                    "group_id": original.group_id,
                    "user_id": original.user_id,
                    "subscription_id": original.subscription_id,
                    "tier_key": original.tier_key,
                }
            record_payment(
                provider="stripe",
                amount_cents=int(amount),
                currency=obj.get("currency") or "USD",
                external_id=f"refund:{obj.get('id')}",
                kind="refund",
                **fields,
            )

    def _apply():
        with db_session() as s:
            meta = obj.get("metadata") or {}

            # Race hardening: checkout.session.completed is the only event that
            # CREATES a leg, and Stripe may deliver customer.subscription.* /
            # invoice.paid *before* it (dropped as no-ops → NULL renewal date +
            # missing first-invoice ledger row). Pull authoritative state
            # straight from Stripe so a new row is complete regardless of
            # sibling-event ordering.
            checkout_state = (
                billing.fetch_subscription_state(obj.get("subscription"))
                if etype == "checkout.session.completed"
                else None
            )

            def _finalize(rec):
                if not checkout_state:
                    return
                if checkout_state.get("status"):
                    rec.status = checkout_state["status"]
                if checkout_state.get("current_period_end"):
                    rec.current_period_end = checkout_state["current_period_end"]
                rec.cancel_at_period_end = bool(checkout_state.get("cancel_at_period_end"))

            def _record_first_invoice(**fields):
                # Idempotent via the invoice's external_id — harmless if the
                # invoice.paid webhook already recorded (or later records) it.
                if not checkout_state:
                    return
                inv = checkout_state.get("invoice")
                if not inv:
                    return
                record_payment(
                    provider="stripe",
                    amount_cents=inv["amount_cents"],
                    currency=inv["currency"],
                    external_id=inv["external_id"],
                    paid_at=inv["paid_at"],
                    notify=True,
                    **fields,
                )

            # ---- User-scoped supporter subscription ----
            if meta.get("scope") == "user":
                user_id = None
                try:
                    user_id = int(meta.get("user_id"))
                except (ValueError, TypeError):
                    user_id = None
                sub = None
                if user_id is not None:
                    sub = s.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
                    if not sub:
                        sub = UserSubscription(user_id=user_id)
                        s.add(sub)
                if sub is not None:
                    _apply_fields(sub, meta)
                    amt = _extract_amount_cents()
                    if amt:
                        sub.amount_cents = amt
                    _finalize(sub)
                    s.commit()
                    _record_first_invoice(
                        scope="user",
                        user_id=sub.user_id,
                        subscription_id=sub.id,
                        tier_key=sub.tier_key,
                    )
                    _invalidate_user_entitlements(user_id)
                return

            # ---- Group contribution legs (subscription pool) ----
            # A leg is keyed by its provider subscription id — never "the
            # group's row"; a pool holds many legs.
            prov_id = _provider_subscription_id()

            leg = None
            if prov_id:
                leg = (
                    s.query(GroupSubscription)
                    .filter(GroupSubscription.provider_subscription_id == prov_id)
                    .first()
                )
                if leg is None:
                    # Pre-metadata user-sub events land here too — match the
                    # supporter table before assuming a group leg.
                    usub = (
                        s.query(UserSubscription)
                        .filter(UserSubscription.provider_subscription_id == prov_id)
                        .first()
                    )
                    if usub is not None:
                        _apply_fields(usub, meta)
                        amt = _extract_amount_cents()
                        if amt:
                            usub.amount_cents = amt
                        _finalize(usub)
                        s.commit()
                        _record_first_invoice(
                            scope="user",
                            user_id=usub.user_id,
                            subscription_id=usub.id,
                            tier_key=usub.tier_key,
                        )
                        _invalidate_user_entitlements(usub.user_id)
                        return

            if leg is None:
                # Only a completed checkout may CREATE a leg.
                if etype != "checkout.session.completed":
                    return
                group_id = None
                if meta.get("group_id"):
                    try:
                        group_id = int(meta["group_id"])
                    except (ValueError, TypeError):
                        group_id = None
                if group_id is None and obj.get("client_reference_id"):
                    try:
                        group_id = int(obj["client_reference_id"])
                    except (ValueError, TypeError):
                        group_id = None
                if group_id is None:
                    return
                leg = GroupSubscription(group_id=group_id)
                s.add(leg)

            _apply_fields(leg, meta)
            amt = _extract_amount_cents()
            if amt:
                leg.amount_cents = amt
            if leg.user_id is None and meta.get("payer_user_id"):
                try:
                    leg.user_id = int(meta["payer_user_id"])
                except (ValueError, TypeError):
                    pass
            _finalize(leg)
            s.commit()
            _record_first_invoice(
                scope="group",
                group_id=leg.group_id,
                user_id=leg.user_id,
                subscription_id=leg.id,
                tier_key=leg.tier_key,
            )

    await asyncio.to_thread(_record_ledger)
    await asyncio.to_thread(_apply)
    return jsonify({"received": True})
