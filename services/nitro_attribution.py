"""Discord Nitro boost → group subscription-pool credit.

When a member linked to a group places a Nitro boost on the **main** DropTracker
Discord server, that boost is turned into recurring-looking credit toward the
group's subscription pool (``db/entitlements.py`` pool model), helping the group
unlock premium features. It is NOT paid revenue (see ``NON_REVENUE_PROVIDERS``).

Design (confirmed rules):
  * Attribution basis is DropTracker group membership (the booster's linked
    Discord account → their group association), NOT Discord-guild membership.
  * A member may place **more than one** boost. Each boost *slot* contributes
    ``NITRO_BOOST_CENTS`` (default $5) of monthly pool credit, and all of a
    member's slots go to **exactly one** group (their designation, see below).
  * The per-group credit lives in a single ``provider="nitro"`` leg on
    ``group_subscriptions`` (``amount_cents = boost_slots × NITRO_BOOST_CENTS``).
    It flows through ``effective_group_subscription`` untouched, so it stacks
    with paid subs and is naturally capped at the priciest tier the pool covers
    — a group already at the top tier gets no extra unlock.

Counting boost slots (Discord gives no direct per-member count):
  * ``GuildMember.premium_since`` says *whether* someone boosts, never how many
    slots — a member list alone can only ever yield 1 per booster.
  * ``Guild.premium_subscription_count`` is the authoritative TOTAL number of
    live slots on the guild. It cannot attribute them, but it is the ceiling
    and the audit: attributed < total means slots are unaccounted for.
  * Boost system messages (types 8/9/10/11) carry the booster's cumulative slot
    count in ``content`` — Discord renders them as
    ``"{author} just boosted the server **{content}** times!"``, with ``content``
    empty for a single boost. This is the only per-member signal, but the log is
    append-only: someone who drops one of two slots leaves a stale ``2`` behind,
    so it is a *hint* the guild total must corroborate — never authority.
  * ``nitro_boost_count`` on ``UserConfiguration`` is a manual admin override and
    always wins. It is the only way to close slots the message log never saw
    (e.g. boosts placed before the current system channel existed).

Unattributable slots are deliberately NOT credited — ``resolve_boost_counts``
reports them as ``unattributed`` for an admin to assign, rather than inventing
credit for a group that may not have earned it.

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
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple

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
# UserConfiguration key: manual per-user boost-slot count set by an admin. Always
# beats the message-derived count; the only way to close slots Discord's message
# log never recorded.
NITRO_COUNT_OVERRIDE_KEY = "nitro_boost_count"
# UserConfiguration key: slot count last observed in a boost system message.
# Written by the bot (live listener + history scan), advisory only.
NITRO_COUNT_OBSERVED_KEY = "nitro_boost_observed"

# Discord system-message types announcing a boost. `content` on these carries the
# booster's cumulative slot count ("" or absent means 1). Types 9/10/11 are the
# same event that also pushed the guild to level 1/2/3.
BOOST_MESSAGE_TYPES = frozenset({8, 9, 10, 11})

# Nobody is credited more slots than this from an unverified signal — a guard
# against a malformed `content` turning into unbounded credit.
MAX_BOOST_SLOTS_PER_USER = 24

# system_channel_flags bit: when set, Discord posts no boost system messages and
# the message-derived attribution signal is unavailable.
_SUPPRESS_PREMIUM_SUBSCRIPTIONS = 1 << 1

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
# Per-user boost-slot counts (manual override + observed-from-message)
#
# Both live in ``user_configurations`` alongside the group designation above, so
# neither needs a migration. They are kept in SEPARATE keys on purpose: the
# message listener must never overwrite an admin's deliberate correction.
# --------------------------------------------------------------------------- #
def _get_user_config_int(session, user_id: int, key: str) -> Optional[int]:
    from db.models import UserConfiguration

    row = (
        session.query(UserConfiguration)
        .filter(
            UserConfiguration.user_id == user_id,
            UserConfiguration.config_key == key,
        )
        .first()
    )
    if row and row.config_value:
        try:
            return int(row.config_value)
        except (TypeError, ValueError):
            return None
    return None


def _set_user_config_int(session, user_id: int, key: str, value: Optional[int]) -> None:
    """Upsert (or delete, when value is None) an int-valued user config. Caller
    commits."""
    from db.models import UserConfiguration

    row = (
        session.query(UserConfiguration)
        .filter(
            UserConfiguration.user_id == user_id,
            UserConfiguration.config_key == key,
        )
        .first()
    )
    if value is None:
        if row is not None:
            session.delete(row)
        return
    if row is not None:
        row.config_value = str(int(value))
    else:
        session.add(
            UserConfiguration(user_id=user_id, config_key=key, config_value=str(int(value)))
        )


def get_boost_count_override(session, user_id: int) -> Optional[int]:
    """Admin-set slot count for this user, or None when unset."""
    return _get_user_config_int(session, user_id, NITRO_COUNT_OVERRIDE_KEY)


def set_boost_count_override(session, user_id: int, count: Optional[int]) -> None:
    """Set (or clear, when count is None) the admin override. Caller commits."""
    if count is not None:
        count = max(1, min(int(count), MAX_BOOST_SLOTS_PER_USER))
    _set_user_config_int(session, user_id, NITRO_COUNT_OVERRIDE_KEY, count)


def get_observed_boost_count(session, user_id: int) -> Optional[int]:
    """Slot count last seen in a boost system message, or None."""
    return _get_user_config_int(session, user_id, NITRO_COUNT_OBSERVED_KEY)


def record_observed_boost_count(session, discord_id, count: int) -> bool:
    """Persist a slot count seen in a boost system message. Caller commits.

    Returns False when the Discord account isn't linked to a DropTracker user
    (nothing to hang the value off) or the count isn't usable.
    """
    from db.models import User

    try:
        count = int(count)
    except (TypeError, ValueError):
        return False
    if count < 1:
        return False
    count = min(count, MAX_BOOST_SLOTS_PER_USER)
    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if user is None:
        return False
    _set_user_config_int(session, user.user_id, NITRO_COUNT_OBSERVED_KEY, count)
    return True


def _load_counts_by_discord_id(
    session, discord_ids: Iterable[str], key: str
) -> Dict[str, int]:
    """Bulk-load an int user config for a set of Discord ids → ``{discord_id: n}``."""
    from db.models import User, UserConfiguration

    ids = {str(x) for x in discord_ids if x is not None}
    if not ids:
        return {}
    rows = (
        session.query(User.discord_id, UserConfiguration.config_value)
        .join(UserConfiguration, UserConfiguration.user_id == User.user_id)
        .filter(User.discord_id.in_(ids), UserConfiguration.config_key == key)
        .all()
    )
    out: Dict[str, int] = {}
    for discord_id, value in rows:
        try:
            out[str(discord_id)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def load_boost_overrides(session, discord_ids: Iterable[str]) -> Dict[str, int]:
    """Admin overrides for these Discord ids."""
    return _load_counts_by_discord_id(session, discord_ids, NITRO_COUNT_OVERRIDE_KEY)


def load_observed_counts(session, discord_ids: Iterable[str]) -> Dict[str, int]:
    """Message-derived counts previously persisted for these Discord ids."""
    return _load_counts_by_discord_id(session, discord_ids, NITRO_COUNT_OBSERVED_KEY)


# --------------------------------------------------------------------------- #
# Slot resolution — pure. Turns the three signals into a per-booster slot count
# plus the diagnostics an admin needs to see what could not be attributed.
# --------------------------------------------------------------------------- #
def boost_count_from_message(message: Mapping) -> Optional[Tuple[str, int]]:
    """``(booster_discord_id, slot_count)`` from a boost system message, else None.

    ``content`` holds the cumulative count and is empty for a first/only boost,
    which Discord renders as plain "just boosted the server!".
    """
    if not message or message.get("type") not in BOOST_MESSAGE_TYPES:
        return None
    author_id = (message.get("author") or {}).get("id")
    if not author_id:
        return None
    raw = (message.get("content") or "").strip()
    if not raw:
        return str(author_id), 1
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return str(author_id), 1
    return str(author_id), max(1, min(count, MAX_BOOST_SLOTS_PER_USER))


def resolve_boost_counts(
    boosters: Iterable[str],
    observed: Optional[Mapping[str, int]] = None,
    overrides: Optional[Mapping[str, int]] = None,
    guild_total: Optional[int] = None,
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """``({discord_id: slots}, diagnostics)`` for the guild's current boosters.

    Precedence per booster: admin override → message-derived count → 1. Only
    members who are *currently* boosting are counted at all, so a stale override
    or message for someone who stopped is ignored rather than credited.

    ``guild_total`` (``Guild.premium_subscription_count``) is the ceiling. When
    the signals claim more slots than the guild actually has, the surplus is
    trimmed from the largest unverified counts first (never below 1 per booster,
    and admin overrides are trimmed last) — over-crediting a group is worse than
    leaving a slot unassigned. When they claim fewer, the shortfall is reported
    as ``unattributed`` and deliberately left uncredited.
    """
    observed = dict(observed or {})
    overrides = dict(overrides or {})
    live = sorted({str(b) for b in boosters if b is not None})

    counts: Dict[str, int] = {}
    manual: Set[str] = set()
    for discord_id in live:
        slots = 1
        seen = observed.get(discord_id)
        if seen and seen > 1:
            slots = min(int(seen), MAX_BOOST_SLOTS_PER_USER)
        override = overrides.get(discord_id)
        if override:
            slots = max(1, min(int(override), MAX_BOOST_SLOTS_PER_USER))
            manual.add(discord_id)
        counts[discord_id] = slots

    trimmed = 0
    if guild_total is not None and guild_total > 0:
        # Trim automatic claims before manual ones, largest first; ties broken on
        # id so the outcome is deterministic across runs.
        while sum(counts.values()) > guild_total:
            candidates = [d for d, n in counts.items() if n > 1 and d not in manual]
            if not candidates:
                candidates = [d for d, n in counts.items() if n > 1]
            if not candidates:
                break  # more boosters than slots — can't fix by trimming
            victim = max(candidates, key=lambda d: (counts[d], d))
            counts[victim] -= 1
            trimmed += 1

    attributed = sum(counts.values())
    diagnostics = {
        "guild_total": int(guild_total) if guild_total is not None else None,
        "boosters": len(live),
        "attributed": attributed,
        # Slots the guild says exist that no signal could pin on a member. Not
        # credited — surfaced for an admin to assign via the override.
        "unattributed": max(0, int(guild_total) - attributed) if guild_total else 0,
        # Slots claimed beyond what the guild reports (stale message log).
        "over_attributed": max(0, attributed - int(guild_total)) if guild_total else 0,
        "trimmed": trimmed,
        "manual_overrides": len(manual),
    }
    return counts, diagnostics


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


def attribute_boosters(session, boost_counts) -> Dict[int, int]:
    """Map boosters to ``{group_id: boost_slots}``.

    ``boost_counts`` is either ``{discord_id: slots}`` (from
    ``resolve_boost_counts``) or a bare iterable of Discord ids, which counts as
    one slot each. Boosters with no linked user or no real group are skipped; all
    of a booster's slots go to exactly one group.
    """
    from db.models import User

    if isinstance(boost_counts, Mapping):
        slots_by_id = {
            str(k): max(1, int(v)) for k, v in boost_counts.items() if k is not None
        }
    else:
        slots_by_id = {str(x): 1 for x in boost_counts if x is not None}
    if not slots_by_id:
        return {}
    users = session.query(User).filter(User.discord_id.in_(set(slots_by_id))).all()

    counts: Dict[int, int] = {}
    for user in users:
        gid = pick_group_for_user(session, user.user_id)
        if gid is None:
            continue
        counts[gid] = counts.get(gid, 0) + slots_by_id.get(str(user.discord_id), 1)
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
    boosts_credited = 0

    existing = {
        int(leg.group_id): leg
        for leg in session.query(GroupSubscription)
        .filter(GroupSubscription.provider == NITRO_PROVIDER)
        .all()
    }

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
        boosts_credited += count

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
        "boosts_credited": boosts_credited,
        "legs_expired": expired,
    }


def run_reconcile(
    session,
    booster_discord_ids: Set[str],
    observed_counts: Optional[Mapping[str, int]] = None,
    guild_total: Optional[int] = None,
) -> Dict[str, Any]:
    """Full pure-DB reconcile from the guild's current boosters.

    ``observed_counts`` are freshly-seen message-derived slot counts (from a
    history scan); previously persisted ones are loaded from the DB and merged,
    with the fresh values winning. ``guild_total`` is
    ``Guild.premium_subscription_count`` — the ceiling and the audit.
    """
    boosters = {str(b) for b in booster_discord_ids if b is not None}
    observed = load_observed_counts(session, boosters)
    for discord_id, count in (observed_counts or {}).items():
        observed[str(discord_id)] = int(count)
    overrides = load_boost_overrides(session, boosters)

    slots, diagnostics = resolve_boost_counts(boosters, observed, overrides, guild_total)
    group_counts = attribute_boosters(session, slots)
    stats: Dict[str, Any] = reconcile_nitro_legs(session, group_counts)
    stats["boosters_seen"] = len(boosters)
    stats.update(diagnostics)
    publish_boost_snapshot(
        build_boost_snapshot(session, slots, diagnostics, overrides, observed)
    )
    return stats


# --------------------------------------------------------------------------- #
# Admin read model
#
# The bot holds the Discord facts and the web API must never open a Discord
# connection, so the reconcile publishes a snapshot to Redis for
# /admin/nitro-boosts to render. Redis is imported lazily to keep this module
# importable (and unit-testable) without it.
# --------------------------------------------------------------------------- #
BOOST_SNAPSHOT_KEY = "nitro:boost_snapshot"
# Set by the web API when an admin changes an override; the bot's scheduler
# consumes it to run a reconcile promptly instead of waiting out the interval.
RECONCILE_REQUEST_KEY = "nitro:reconcile_requested"


def request_reconcile() -> bool:
    """Ask the bot to reconcile on its next poll. Best-effort; never raises."""
    try:
        from utils.redis import RedisClient

        RedisClient().set(RECONCILE_REQUEST_KEY, "1")
        return True
    except Exception:
        return False


def build_boost_snapshot(
    session,
    slots: Mapping[str, int],
    diagnostics: Mapping[str, Any],
    overrides: Optional[Mapping[str, int]] = None,
    observed: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    """Per-booster rows + totals for the admin dashboard."""
    from db.models import Group, User

    overrides = dict(overrides or {})
    observed = dict(observed or {})
    ids = {str(k) for k in slots}
    users = (
        session.query(User).filter(User.discord_id.in_(ids)).all() if ids else []
    )
    by_discord = {str(u.discord_id): u for u in users}

    entries = []
    group_ids = set()
    for discord_id, count in sorted(slots.items(), key=lambda kv: (-kv[1], kv[0])):
        user = by_discord.get(discord_id)
        group_id = pick_group_for_user(session, user.user_id) if user else None
        if group_id:
            group_ids.add(group_id)
        entries.append({
            "discord_id": discord_id,
            "user_id": user.user_id if user else None,
            "username": getattr(user, "username", None) if user else None,
            "slots": int(count),
            "source": (
                "manual" if discord_id in overrides
                else "message" if observed.get(discord_id, 1) > 1
                else "default"
            ),
            "override": int(overrides[discord_id]) if discord_id in overrides else None,
            "observed": int(observed[discord_id]) if discord_id in observed else None,
            "group_id": group_id,
            "monthly_cents": int(count) * NITRO_BOOST_CENTS,
        })

    names: Dict[int, str] = {}
    if group_ids:
        names = {
            int(gid): nm
            for gid, nm in session.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(group_ids))
            .all()
        }
    for entry in entries:
        gid = entry["group_id"]
        entry["group_name"] = names.get(gid) if gid else None

    return {
        "at": int(datetime.now().timestamp()),
        "per_boost_cents": NITRO_BOOST_CENTS,
        "guild_total": diagnostics.get("guild_total"),
        "boosters": diagnostics.get("boosters", len(entries)),
        "attributed": diagnostics.get("attributed", sum(slots.values())),
        "unattributed": diagnostics.get("unattributed", 0),
        "over_attributed": diagnostics.get("over_attributed", 0),
        "trimmed": diagnostics.get("trimmed", 0),
        "entries": entries,
    }


def publish_boost_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """Best-effort publish of the admin read model. Never raises."""
    import json

    try:
        from utils.redis import RedisClient

        RedisClient().set(BOOST_SNAPSHOT_KEY, json.dumps(snapshot))
        return True
    except Exception:
        return False


def load_boost_snapshot() -> Optional[Dict[str, Any]]:
    """Latest published snapshot, or None when the bot hasn't reconciled yet."""
    import json

    try:
        from utils.redis import RedisClient

        raw = RedisClient().get(BOOST_SNAPSHOT_KEY)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


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


async def fetch_guild_boost_state(http, guild_id: Optional[str] = None) -> Dict[str, Any]:
    """``{total, system_channel_id, boost_messages_enabled}`` for the main guild.

    ``premium_subscription_count`` is the authoritative number of live boost
    slots. ``system_channel_id`` is where boost system messages land, and they
    are only posted when the SUPPRESS_PREMIUM_SUBSCRIPTIONS flag (1 << 1) is
    clear. Returns ``total=None`` on any failure so the caller degrades to
    one-slot-per-booster rather than crashing the reconcile.
    """
    guild_id = guild_id or MAIN_GUILD_ID
    try:
        guild = await http.get_guild(guild_id)
    except Exception:
        return {"total": None, "system_channel_id": None, "boost_messages_enabled": False}
    raw_total = guild.get("premium_subscription_count")
    try:
        total = int(raw_total) if raw_total is not None else None
    except (TypeError, ValueError):
        total = None
    try:
        flags = int(guild.get("system_channel_flags") or 0)
    except (TypeError, ValueError):
        flags = 0
    channel_id = guild.get("system_channel_id")
    return {
        "total": total,
        "system_channel_id": str(channel_id) if channel_id else None,
        "boost_messages_enabled": bool(channel_id) and not (flags & _SUPPRESS_PREMIUM_SUBSCRIPTIONS),
    }


async def fetch_boost_message_counts(
    http,
    channel_id: str,
    max_pages: int = 60,
    page_limit: int = 100,
    page_pause: float = 0.3,
) -> Dict[str, int]:
    """Scan a channel's history for boost system messages → ``{discord_id: slots}``.

    Walks backwards from newest. A member's newest boost message wins, since
    ``content`` is cumulative at the time it was posted. Best-effort: a missing
    channel or a permissions error yields ``{}`` and the caller falls back to
    one slot per booster.
    """
    if not channel_id:
        return {}
    counts: Dict[str, int] = {}
    before: Optional[str] = None
    for _ in range(max_pages):
        try:
            page = await http.get_channel_messages(channel_id, limit=page_limit, before=before)
        except Exception:
            break
        if not page:
            break
        for message in page:
            parsed = boost_count_from_message(message)
            if parsed is None:
                continue
            discord_id, slots = parsed
            # Newest-first walk: the first value seen for a member is their latest.
            counts.setdefault(discord_id, slots)
        if len(page) < page_limit:
            break
        before = str(page[-1].get("id") or "")
        if not before:
            break
        if page_pause:
            await asyncio.sleep(page_pause)
    return counts


# --------------------------------------------------------------------------- #
# Booster messaging helpers (used by the webhook bot's boost-time DM + the
# contributors-channel announcement). DB lookups here; the bot owns Discord I/O.
# --------------------------------------------------------------------------- #
_SITE = "https://www.droptracker.io"


def format_cents(cents: Optional[int]) -> str:
    """`500 -> "$5"`, `250 -> "$2.50"`."""
    cents = int(cents or 0)
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def booster_context(session, discord_id) -> Dict[str, Any]:
    """Facts needed to message a booster: linkage, their real groups, the group
    their boost is (or would be) credited to, and how many slots they placed."""
    from db.models import Group, User

    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if user is None:
        return {
            "linked": False,
            "user_id": None,
            "groups": [],
            "picked_group_id": None,
            "picked_group_name": None,
            "boost_slots": 1,
        }
    gids = user_group_ids(session, user.user_id)
    names: Dict[int, str] = {}
    if gids:
        names = {
            int(gid): nm
            for gid, nm in session.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(gids))
            .all()
        }
    picked = pick_group_for_user(session, user.user_id)
    # Slots the member has placed, so the DM/announcement quotes what they
    # actually contributed rather than a flat one-boost figure.
    slots = get_boost_count_override(session, user.user_id)
    if not slots:
        slots = get_observed_boost_count(session, user.user_id) or 1
    return {
        "linked": True,
        "user_id": user.user_id,
        "groups": [{"id": gid, "name": names.get(gid) or f"Group {gid}"} for gid in gids],
        "picked_group_id": picked,
        "picked_group_name": (names.get(picked) or f"Group {picked}") if picked else None,
        "boost_slots": max(1, min(int(slots), MAX_BOOST_SLOTS_PER_USER)),
    }


def designate_group_for_discord_user(session, discord_id, group_id) -> Optional[str]:
    """Validate the group is one the (linked) booster belongs to, set the
    designation, and return the group name. None if not linked / not a member.
    Caller commits."""
    from db.models import Group, User

    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if user is None:
        return None
    if int(group_id) not in user_group_ids(session, user.user_id):
        return None
    set_designated_group(session, user.user_id, int(group_id))
    name = session.query(Group.group_name).filter(Group.group_id == int(group_id)).scalar()
    return name or f"Group {int(group_id)}"


def _credit_text(context: Dict[str, Any], per_boost_cents: Optional[int] = None) -> str:
    """The member's total monthly credit — slots × per-boost, so a double
    booster is told $10/mo rather than the flat single-boost figure."""
    per = NITRO_BOOST_CENTS if per_boost_cents is None else per_boost_cents
    slots = max(1, int(context.get("boost_slots") or 1))
    return format_cents(slots * per)


def nitro_boost_dm_text(context: Dict[str, Any], per_boost_cents: Optional[int] = None) -> str:
    """Confirmation DM body, varying by linkage and group count."""
    amt = _credit_text(context, per_boost_cents)
    if not context.get("linked"):
        return (
            f"🎉 Thanks for boosting **DropTracker**!\n"
            f"Link your Discord at {_SITE} and your {amt}/mo of boost credit will go toward your clan's premium."
        )
    groups = context.get("groups") or []
    if not groups:
        return (
            f"🎉 Thanks for boosting **DropTracker**!\n"
            f"Once you join a clan on DropTracker, your {amt}/mo of boost credit will support it automatically."
        )
    if len(groups) == 1:
        return (
            f"🎉 Thanks for boosting **DropTracker**!\n"
            f"Your {amt}/mo of premium credit now supports **{groups[0]['name']}**. Manage it at {_SITE}/settings."
        )
    picked = context.get("picked_group_name") or groups[0]["name"]
    return (
        f"🎉 Thanks for boosting **DropTracker**!\n"
        f"Your {amt}/mo of premium credit is set to support **{picked}**. "
        f"Pick a different clan below, or manage it any time at {_SITE}/settings."
    )


def nitro_boost_announcement_text(
    mention: str, context: Dict[str, Any], per_boost_cents: Optional[int] = None
) -> str:
    """One-liner for the contributors channel (single live boost)."""
    amt = _credit_text(context, per_boost_cents)
    slots = max(1, int(context.get("boost_slots") or 1))
    boosted = "just boosted the server" if slots == 1 else f"just boosted the server **{slots}×**"
    picked = context.get("picked_group_name") if context.get("linked") else None
    if picked:
        return f"🚀 {mention} {boosted} — {amt}/mo of premium credit now supports **{picked}**! Thank you 💜"
    return f"🚀 {mention} {boosted} — thank you for the support! 💜"


def nitro_boost_summary_blocks(
    entries: list, credited_cents: int, per_boost_cents: Optional[int] = None
) -> list:
    """Description block(s) for ONE consolidated thank-you (backfill), chunked to
    <=10 embeds' worth. Each entry is ``{"discord_id": str, "group": str|None}``.
    The caller wraps each block in an embed (mentions in embeds don't ping)."""
    lines = []
    for e in entries:
        did = e.get("discord_id")
        grp = e.get("group")
        lines.append(f"• <@{did}> → **{grp}**" if grp else f"• <@{did}>")
    blocks: list = []
    buf: list = []
    size = 0
    for ln in lines:
        if size + len(ln) + 1 > 3800 and buf:
            blocks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        blocks.append("\n".join(buf))
    blocks = blocks[:10]
    if blocks:
        intro = (
            f"These members boost the **DropTracker** Discord — together contributing "
            f"**{format_cents(credited_cents)}/mo** of premium credit to their clans! Thank you 💜\n\n"
        )
        blocks[0] = intro + blocks[0]
    return blocks
