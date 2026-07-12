"""Per-group manual-submission policy (suggestion #45).

Groups configure ``manual_submission_policy`` (registry-validated select):

- ``allow`` (default)   — manual submissions count immediately (legacy).
- ``confirm``           — an unauthorized member's manual drop is held
                          ``pending`` a group admin's review; it counts for the
                          group only once approved. Authorized members bypass.
- ``authorized_only``   — only the group's admins / authorized users' manual
                          submissions count; everyone else's are permanently
                          excluded from this group's boards & notifications.
- ``block``             — no manual submission ever counts for this group.

A policy only ever affects the configuring group: the drop row is always
written, always counts globally and for the player's other groups. Withheld
drops are made durable as ``drop_group_moderation`` rows so board rebuilds
(reconcile scripts, force-update) and the lootboard generator can re-apply
them — write-time Redis filtering alone would leak them back in on the next
rebuild. ``pending`` rows can later be ``approved`` (retro-applied, see
``services/drop_moderation.py``) or ``rejected``; ``excluded`` rows are
permanent.

``resolve_manual_moderation`` is pure (unit-tested in isolation); the session-
bound gatherers live alongside it.
"""
from __future__ import annotations

import json

MANUAL_POLICY_KEY = "manual_submission_policy"

# Policy values (mirror the registry select). 'allow' is the implicit default.
MANUAL_POLICIES = ("allow", "confirm", "authorized_only", "block")

# Group ids the policy never applies to (1 = template group, 2 = global).
_SYSTEM_GROUP_MAX = 2


def resolve_manual_moderation(policies: dict, authorized_group_ids: set) -> dict:
    """Group id -> withhold status for a MANUAL submission (pure; no I/O).

    Returns ``{group_id: 'excluded' | 'pending'}`` for every group the
    submission must be withheld from. Groups that count normally are absent.

    - ``block``                       -> 'excluded' (permanent)
    - ``authorized_only`` + unauth    -> 'excluded' (permanent)
    - ``confirm`` + unauth            -> 'pending'  (reviewable)
    - authorized submitter            -> absent (bypasses confirm/auth_only)
    - ``allow`` / missing / unknown   -> absent (a bad config value must never
                                         silently withhold data)
    """
    out = {}
    for group_id, policy in policies.items():
        if group_id <= _SYSTEM_GROUP_MAX:
            continue
        authorized = group_id in authorized_group_ids
        if policy == "block":
            out[group_id] = "excluded"
        elif policy == "authorized_only" and not authorized:
            out[group_id] = "excluded"
        elif policy == "confirm" and not authorized:
            out[group_id] = "pending"
    return out


def _authorized_group_ids(session, user_id, group_ids) -> set:
    """Groups (of ``group_ids``) where ``user_id`` is an admin or authorized
    user: a ``group_admins`` grant, or the user's Discord id in the group's
    ``authed_users`` config (JSON list; spills into long_value — read both)."""
    if not user_id or not group_ids:
        return set()
    from db.models import GroupAdmin, GroupConfiguration, User

    out = {
        gid for (gid,) in
        session.query(GroupAdmin.group_id)
        .filter(GroupAdmin.user_id == user_id, GroupAdmin.group_id.in_(group_ids))
        .all()
    }
    discord_id = (
        session.query(User.discord_id).filter(User.user_id == user_id).scalar()
    )
    if discord_id:
        rows = (
            session.query(GroupConfiguration)
            .filter(
                GroupConfiguration.group_id.in_(group_ids),
                GroupConfiguration.config_key == "authed_users",
            )
            .all()
        )
        for row in rows:
            raw = row.config_value or getattr(row, "long_value", None)
            if not raw:
                continue
            try:
                authed = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(authed, list) and str(discord_id) in {str(a) for a in authed}:
                out.add(row.group_id)
    return out


def manual_moderation_for_player(session, player, group_ids) -> dict:
    """Which of ``group_ids`` this player's MANUAL submission must be withheld
    from, as ``{group_id: (status, policy)}`` (status ∈ excluded|pending;
    policy feeds the moderation row's ``reason``). Empty dict = nothing held.

    The submitter is the player's owning user — the web API only lets a user
    manual-submit for players they own (web_api/routes/submissions.py), so no
    separate submitter identity travels with the payload.
    """
    real_gids = [g for g in group_ids if g > _SYSTEM_GROUP_MAX]
    if not real_gids:
        return {}
    from utils import group_config as gc

    values = gc.get_bulk(session, real_gids, [MANUAL_POLICY_KEY])
    policies = {gid: values.get((gid, MANUAL_POLICY_KEY)) for gid in real_gids}
    if not any(p in ("block", "authorized_only", "confirm") for p in policies.values()):
        return {}
    authorized = _authorized_group_ids(
        session, getattr(player, "user_id", None), real_gids
    )
    statuses = resolve_manual_moderation(policies, authorized)
    return {gid: (status, policies.get(gid)) for gid, status in statuses.items()}


def record_moderation(session, drop_id, moderation: dict) -> None:
    """Durably record withheld (drop, group) rows (one per group)."""
    if not moderation or not drop_id:
        return
    from db.models import DropGroupModeration

    for gid in sorted(moderation):
        status, policy = moderation[gid]
        session.add(DropGroupModeration(
            drop_id=drop_id,
            group_id=gid,
            status=status,
            reason=f"policy:{policy}" if policy else "policy",
        ))


def manual_notification_suppressed_groups(session, player, group_ids) -> set:
    """Groups whose Discord notification must be suppressed for a MANUAL
    non-drop submission (clog / pb / ca / pet) under a restrictive policy.

    These types have no group leaderboard to withhold from and their DB record
    is player-global, so the policy's only group-scoped effect is the
    notification — and there is nothing to retro-credit, so ``confirm`` here
    simply suppresses too (no review queue for non-drop types; only drops get
    the pending/approve flow). Reuses the exact drop resolution, so the
    authorized-member bypass and system-group carve-outs apply identically.
    """
    return set(manual_moderation_for_player(session, player, group_ids))


REVIEW_CHANNEL_KEY = "channel_id_to_post_manual_review"


async def notify_pending_review(session, drop, player, item_name, npc_name, drop_value,
                                pending_group_ids, use_external_session: bool = False) -> None:
    """Ping each pending group's configured review channel about a manual drop
    now awaiting approval. Enqueues to ``discord_outbox`` (the core bot drains
    it); groups without ``channel_id_to_post_manual_review`` set are review-on-
    the-website only. Best-effort — never raises into the submission path."""
    pending_group_ids = [g for g in (pending_group_ids or []) if g]
    if not pending_group_ids:
        return
    from utils import group_config as gc
    from utils.format import format_number
    from services.discord_outbox import enqueue

    channels = gc.get_bulk(session, pending_group_ids, [REVIEW_CHANNEL_KEY])
    player_name = getattr(player, "player_name", "A player")
    enqueued = False
    for gid in pending_group_ids:
        channel_id = channels.get((gid, REVIEW_CHANNEL_KEY))
        if not channel_id:
            continue
        content = (
            f"\U0001F50D **Manual submission awaiting review** — "
            f"`{player_name}` submitted **{item_name}** from **{npc_name}** "
            f"(~{format_number(int(drop_value))} gp). "
            f"Approve or reject it: https://www.droptracker.io/groups/{gid}/submissions"
        )
        enqueue(
            session, channel_id=str(channel_id), content=content,
            kind="message", ref_type="manual_review",
            ref_id=getattr(drop, "drop_id", None), commit=False,
        )
        enqueued = True
    if enqueued:
        if use_external_session:
            session.flush()
        else:
            session.commit()
