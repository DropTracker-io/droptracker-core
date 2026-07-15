"""Discord Nitro boost → group subscription-pool credit.

When a member linked to a group places a Nitro boost on the **main** DropTracker
Discord server, that boost is turned into recurring-looking credit toward the
group's subscription pool (``db/entitlements.py`` pool model), helping the group
unlock premium features. It is NOT paid revenue (see ``NON_REVENUE_PROVIDERS``).

Design (confirmed rules):
  * Attribution basis is DropTracker group membership (the booster's linked
    Discord account → their group association), NOT Discord-guild membership.
  * Each boosting member contributes ``NITRO_BOOST_CENTS`` (default $5) of
    monthly pool credit to **exactly one** group, once. The Discord API only
    exposes *whether* a user boosts (``premium_since``), never how many boost
    slots they placed, so a booster always counts as 1.
  * The per-group credit lives in a single ``provider="nitro"`` leg on
    ``group_subscriptions`` (``amount_cents = boosters × NITRO_BOOST_CENTS``).
    It flows through ``effective_group_subscription`` untouched, so it stacks
    with paid subs and is naturally capped at the priciest tier the pool covers
    — a group already at the top tier gets no extra unlock.

This module is pure DB + a duck-typed HTTP fetch: it imports neither
``interactions`` nor ``web_api``, so it is unit-testable in isolation and safe
to import from any bot process. The webhook bot (``bots/webhook_bot.py``) owns
the Discord side: it has the privileged GUILD_MEMBERS intent and drives the
reconciler.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

from db.entitlements import NITRO_PROVIDER

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# Main DropTracker Discord server (boosts here count). Same default used across
# the codebase; PRIMARY_GUILD_ID overrides.
MAIN_GUILD_ID = os.getenv("PRIMARY_GUILD_ID", "1172737525069135962").strip()

# Monthly pool credit granted per boosting member, in minor units (cents).
try:
    NITRO_BOOST_CENTS = int(os.getenv("NITRO_BOOST_CENTS", "500"))
except (TypeError, ValueError):
    NITRO_BOOST_CENTS = 500

# How long a reconciled nitro leg stays "live" without a refresh. Must exceed
# the reconcile interval so a single missed run (deploy/restart) doesn't drop a
# group's benefits; the pool applies a further 72h grace on top of this.
try:
    NITRO_LEG_TTL_HOURS = int(os.getenv("NITRO_LEG_TTL_HOURS", "36"))
except (TypeError, ValueError):
    NITRO_LEG_TTL_HOURS = 36

# UserConfiguration key: a multi-group booster's chosen group for their boost.
NITRO_DESIGNATION_KEY = "nitro_boost_group"

# System/template groups never receive boost credit.
_MIN_REAL_GROUP_ID = 2


def process_nitro_boosts_enabled() -> bool:
    """Master on/off flag (case-insensitive; .env ships it as ``True``)."""
    return os.getenv("PROCESS_NITRO_BOOSTS", "").strip().lower() == "true"


# --------------------------------------------------------------------------- #
# Per-booster group designation (which group a multi-group booster supports)
# --------------------------------------------------------------------------- #
def get_designated_group(session, user_id: int) -> Optional[int]:
    """The group_id this user chose their boost to support, or None."""
    from db.models import UserConfiguration

    row = (
        session.query(UserConfiguration)
        .filter(
            UserConfiguration.user_id == user_id,
            UserConfiguration.config_key == NITRO_DESIGNATION_KEY,
        )
        .first()
    )
    if row and row.config_value:
        try:
            return int(row.config_value)
        except (TypeError, ValueError):
            return None
    return None


def set_designated_group(session, user_id: int, group_id: Optional[int]) -> None:
    """Set (or clear, when group_id is None) the user's boost-target group.

    Caller is responsible for validating group membership and committing.
    """
    from db.models import UserConfiguration

    row = (
        session.query(UserConfiguration)
        .filter(
            UserConfiguration.user_id == user_id,
            UserConfiguration.config_key == NITRO_DESIGNATION_KEY,
        )
        .first()
    )
    if group_id is None:
        if row is not None:
            session.delete(row)
        return
    if row is not None:
        row.config_value = str(int(group_id))
    else:
        session.add(
            UserConfiguration(
                user_id=user_id,
                config_key=NITRO_DESIGNATION_KEY,
                config_value=str(int(group_id)),
            )
        )


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #
def user_group_ids(session, user_id: int) -> list[int]:
    """Real (non-system) groups this user belongs to.

    Robust to how membership was recorded: unions direct user associations with
    associations carried by the user's players (WOM sync writes both, but a row
    may exist on only one side). Returned sorted for a stable deterministic
    fallback pick.
    """
    from db.models import Player, user_group_association

    direct = session.query(user_group_association.c.group_id).filter(
        user_group_association.c.user_id == user_id
    )
    via_players = (
        session.query(user_group_association.c.group_id)
        .join(Player, Player.player_id == user_group_association.c.player_id)
        .filter(Player.user_id == user_id)
    )
    ids = {
        int(gid)
        for (gid,) in direct.union(via_players).all()
        if gid is not None and int(gid) > _MIN_REAL_GROUP_ID
    }
    return sorted(ids)


def pick_group_for_user(session, user_id: int) -> Optional[int]:
    """Choose the single group a booster's credit goes to.

    Order: explicit designation → a group they own/admin → deterministic
    (lowest group_id). Returns None when the user is in no real group.
    """
    from db.models import GroupAdmin

    group_ids = user_group_ids(session, user_id)
    if not group_ids:
        return None
    if len(group_ids) == 1:
        return group_ids[0]

    designated = get_designated_group(session, user_id)
    if designated in group_ids:
        return designated

    # Prefer a group they administer; 'owner' sorts after 'admin', so DESC on
    # role puts owner first.
    admin_row = (
        session.query(GroupAdmin.group_id)
        .filter(
            GroupAdmin.user_id == user_id,
            GroupAdmin.group_id.in_(group_ids),
        )
        .order_by(GroupAdmin.role.desc())
        .first()
    )
    if admin_row is not None:
        return int(admin_row[0])

    return group_ids[0]


def attribute_boosters(session, booster_discord_ids: Set[str]) -> Dict[int, int]:
    """Map a set of booster Discord snowflakes to ``{group_id: booster_count}``.

    Boosters with no linked user or no real group are skipped. Each booster is
    counted toward exactly one group.
    """
    from db.models import User

    ids = {str(x) for x in booster_discord_ids if x is not None}
    if not ids:
        return {}
    users = session.query(User).filter(User.discord_id.in_(ids)).all()

    counts: Dict[int, int] = {}
    for user in users:
        gid = pick_group_for_user(session, user.user_id)
        if gid is None:
            continue
        counts[gid] = counts.get(gid, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Reconciliation — the source of truth
# --------------------------------------------------------------------------- #
def reconcile_nitro_legs(
    session, group_counts: Dict[int, int], now: Optional[datetime] = None
) -> Dict[str, int]:
    """Upsert one ``provider="nitro"`` leg per credited group and expire the
    rest. Idempotent: re-running with the same counts just refreshes periods.

    Returns ``{groups_credited, boosters_credited, legs_expired}``.
    """
    from db.models import GroupSubscription

    now = now or datetime.now()
    period_end = now + timedelta(hours=NITRO_LEG_TTL_HOURS)

    existing = {
        int(leg.group_id): leg
        for leg in session.query(GroupSubscription)
        .filter(GroupSubscription.provider == NITRO_PROVIDER)
        .all()
    }

    boosters_credited = 0
    for gid, count in group_counts.items():
        if count <= 0:
            continue
        leg = existing.get(int(gid))
        if leg is None:
            leg = GroupSubscription(group_id=int(gid))
            session.add(leg)
        leg.provider = NITRO_PROVIDER
        leg.status = "active"
        # tier_key=None → leg_monthly_cents uses amount_cents at monthly interval.
        leg.tier_key = None
        leg.amount_cents = count * NITRO_BOOST_CENTS
        leg.current_period_end = period_end
        leg.cancel_at_period_end = False
        leg.user_id = None
        boosters_credited += count

    expired = 0
    for gid, leg in existing.items():
        if gid not in group_counts or group_counts.get(gid, 0) <= 0:
            if leg.status != "expired":
                leg.status = "expired"
                leg.amount_cents = 0
                expired += 1

    session.commit()
    return {
        "groups_credited": len([c for c in group_counts.values() if c > 0]),
        "boosters_credited": boosters_credited,
        "legs_expired": expired,
    }


def run_reconcile(session, booster_discord_ids: Set[str]) -> Dict[str, int]:
    """Full pure-DB reconcile from a set of current booster Discord ids."""
    counts = attribute_boosters(session, booster_discord_ids)
    stats = reconcile_nitro_legs(session, counts)
    stats["boosters_seen"] = len(booster_discord_ids)
    return stats


# --------------------------------------------------------------------------- #
# Discord side — enumerate current boosters of the main guild.
# Requires the GUILD_MEMBERS privileged intent (webhook bot has Intents.ALL).
# `http` is duck-typed (interactions HTTPClient): only `.list_members` is used.
# --------------------------------------------------------------------------- #
async def fetch_booster_discord_ids(
    http, guild_id: Optional[str] = None, page_limit: int = 1000, page_pause: float = 0.4
) -> Set[str]:
    """Page through the main guild's members and return the set of Discord ids
    whose member object carries a non-null ``premium_since`` (i.e. boosting)."""
    guild_id = guild_id or MAIN_GUILD_ID
    boosters: Set[str] = set()
    after: Optional[str] = None
    while True:
        members = await http.list_members(guild_id, limit=page_limit, after=after)
        if not members:
            break
        for m in members:
            if m.get("premium_since"):
                uid = (m.get("user") or {}).get("id")
                if uid:
                    boosters.add(str(uid))
        if len(members) < page_limit:
            break
        last = (members[-1].get("user") or {}).get("id")
        if not last:
            break
        after = str(last)
        if page_pause:
            await asyncio.sleep(page_pause)
    return boosters
