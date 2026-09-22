"""Audience rules for targeted pop-up notices (web118a).

A notice's audience is a list of rules. A signed-in visitor sees the notice when
they match ANY rule (a union), so "clan leaders of Tier 3 groups, plus these two
people" is two rules. Rules are stored as JSON on ``popup_notices`` and matched
per visitor when the site asks ``GET /me/notices`` — nothing is fanned out per
recipient, so a notice to everyone costs the same as a notice to one user, and
somebody who becomes a clan leader while a notice is live starts seeing it.

Rule shapes (after ``normalize_audience``):

    {"type": "everyone"}                              every signed-in user
    {"type": "staff"}                                 developers + superadmins
    {"type": "users", "user_ids": [..]}               specific accounts
    {"type": "group_leaders", "roles": ["owner"] | ["owner", "admin"],
     "group_ids": [..], "group_tiers": [..]}          clan leaders
    {"type": "group_members", "group_ids": [..], "group_tiers": [..]}
    {"type": "supporters", "tier_keys": [..]}         people paying for a tier

Empty ``group_ids`` / ``group_tiers`` / ``tier_keys`` mean "any". A group's tier
is its effective pool tier (``db.entitlements.effective_group_tiers``), or
``"free"`` when its live pool covers no paid tier.

"Supporters" are people who pay: a live personal (user-scope) subscription, or
a live leg they pay toward a group's pool. Comped (``manual``) and Discord
Nitro credits are not payments and don't count (``NON_REVENUE_PROVIDERS``). A
group-scope tier key matches a payer when the group they pay toward currently
sits on that tier.

The module has two halves. The pure half (``normalize_audience``,
``ViewerFacts``, ``audience_matches``) holds every rule decision and is unit
tested without a database. The DB half loads one visitor's facts
(``load_viewer_facts``) or enumerates everyone a rule set reaches for the admin
recipient count (``resolve_audience_user_ids``). Keep the two answering the
same question: a rule change belongs in both.

Known gap, on purpose: a Discord server manager with no DropTracker grant row
is a group ``admin`` (``deps.resolve_group_role``), and the visitor-side match
honours that. But MANAGE_GUILD holders can't be listed server-side (the guild
list is cached per user at login), so the admin count cannot include them and
says so in its notes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

RULE_TYPES = ("everyone", "staff", "users", "group_leaders", "group_members", "supporters")
LEADER_ROLES = ("owner", "admin")
FREE_TIER = "free"

MAX_RULES = 10
MAX_USER_IDS = 500
MAX_GROUP_IDS = 200
MAX_TIER_KEYS = 20

_TIER_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class AudienceError(ValueError):
    """An audience payload the admin must fix; the message is shown to them."""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _int_list(value: Any, what: str, limit: int) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AudienceError(f"'{what}' must be a list of ids.")
    out: list[int] = []
    for v in value:
        # bool is an int subclass; True must not become user 1.
        if isinstance(v, bool):
            raise AudienceError(f"'{what}' holds a value that is not an id.")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise AudienceError(f"'{what}' holds a value that is not an id.") from None
        if n not in out:
            out.append(n)
    if len(out) > limit:
        raise AudienceError(f"At most {limit} entries in '{what}'.")
    return out


def _tier_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AudienceError(f"'{what}' must be a list of tier keys.")
    out: list[str] = []
    for v in value:
        key = str(v).strip()
        if not _TIER_KEY_RE.match(key):
            raise AudienceError(f"'{key}' is not a tier key.")
        if key not in out:
            out.append(key)
    if len(out) > MAX_TIER_KEYS:
        raise AudienceError(f"At most {MAX_TIER_KEYS} entries in '{what}'.")
    return out


def _normalize_rule(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise AudienceError("Each audience rule must be an object.")
    kind = raw.get("type")
    if kind not in RULE_TYPES:
        raise AudienceError(f"Unknown audience type '{kind}'.")

    if kind in ("everyone", "staff"):
        return {"type": kind}

    if kind == "users":
        ids = _int_list(raw.get("user_ids"), "user_ids", MAX_USER_IDS)
        if not ids:
            raise AudienceError("Pick at least one person for 'Specific people'.")
        return {"type": kind, "user_ids": ids}

    if kind == "supporters":
        return {"type": kind, "tier_keys": _tier_list(raw.get("tier_keys"), "tier_keys")}

    rule: dict = {
        "type": kind,
        "group_ids": _int_list(raw.get("group_ids"), "group_ids", MAX_GROUP_IDS),
        "group_tiers": _tier_list(raw.get("group_tiers"), "group_tiers"),
    }
    if kind == "group_leaders":
        roles = raw.get("roles")
        if roles is None:
            roles = ["owner"]
        if not isinstance(roles, list) or not roles:
            raise AudienceError("Choose owners, admins, or both for 'Clan leaders'.")
        clean = [r for r in LEADER_ROLES if r in roles]
        if len(clean) != len(set(roles)):
            raise AudienceError("Clan leader roles must be 'owner' and/or 'admin'.")
        rule["roles"] = clean
    return rule


def normalize_audience(raw: Any) -> list[dict]:
    """Validate an audience payload (a list of rules, or its JSON text) into
    the canonical stored form. Raises ``AudienceError`` with a message meant
    for the admin. An ``everyone`` rule swallows the rest: the union is
    everybody either way, and storing the redundant rules would only make the
    list page describe the audience wrongly."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raise AudienceError("Audience is not valid JSON.") from None
    if not isinstance(raw, list) or not raw:
        raise AudienceError("Choose who should see this notice.")
    if len(raw) > MAX_RULES:
        raise AudienceError(f"At most {MAX_RULES} audience rules.")
    rules = [_normalize_rule(r) for r in raw]
    if any(r["type"] == "everyone" for r in rules):
        return [{"type": "everyone"}]
    return rules


def parse_stored_audience(raw: Optional[str]) -> list[dict]:
    """Read ``audience_json`` back. A row that no longer validates (hand edit,
    older shape) matches nobody rather than everybody: failing closed is the
    only safe reading of a targeting rule."""
    try:
        return normalize_audience(raw or "")
    except AudienceError:
        return []


# --------------------------------------------------------------------------- #
# Matching (pure)
# --------------------------------------------------------------------------- #
@dataclass
class ViewerFacts:
    """What the matcher needs to know about one visitor. Loaders fill only the
    parts the live notices' rules ask about (see ``facts_needed``)."""

    user_id: int
    is_staff: bool = False
    #: group_id -> "owner" | "admin"
    leader_roles: dict = field(default_factory=dict)
    member_group_ids: set = field(default_factory=set)
    #: group_id -> effective tier key; groups absent here are FREE_TIER
    group_tiers: dict = field(default_factory=dict)
    #: True when the visitor pays for any live subscription at all
    is_supporter: bool = False
    #: Tier keys the visitor pays for (user-scope key, or the tier of a group
    #: whose pool they pay into)
    supporter_tiers: set = field(default_factory=set)


def _tier_of(facts: ViewerFacts, group_id: int) -> str:
    return facts.group_tiers.get(group_id, FREE_TIER)


def _group_passes(rule: dict, facts: ViewerFacts, group_id: int) -> bool:
    ids = rule.get("group_ids") or []
    if ids and group_id not in ids:
        return False
    tiers = rule.get("group_tiers") or []
    if tiers and _tier_of(facts, group_id) not in tiers:
        return False
    return True


def rule_matches(rule: dict, facts: ViewerFacts) -> bool:
    kind = rule.get("type")
    if kind == "everyone":
        return True
    if kind == "staff":
        return facts.is_staff
    if kind == "users":
        return facts.user_id in (rule.get("user_ids") or [])
    if kind == "group_leaders":
        roles = set(rule.get("roles") or ())
        return any(
            role in roles and _group_passes(rule, facts, gid)
            for gid, role in facts.leader_roles.items()
        )
    if kind == "group_members":
        return any(_group_passes(rule, facts, gid) for gid in facts.member_group_ids)
    if kind == "supporters":
        keys = rule.get("tier_keys") or []
        if not keys:
            return facts.is_supporter
        return bool(facts.supporter_tiers.intersection(keys))
    return False


def audience_matches(rules: Iterable[dict], facts: ViewerFacts) -> bool:
    return any(rule_matches(r, facts) for r in rules)


def facts_needed(rule_sets: Iterable[Iterable[dict]]) -> set:
    """Which fact groups a set of audiences needs, so a visitor's request only
    runs the queries the live notices can actually use."""
    needs: set = set()
    for rules in rule_sets:
        for r in rules:
            kind = r.get("type")
            if kind == "staff":
                needs.add("staff")
            elif kind == "group_leaders":
                needs.add("leaders")
                if r.get("group_tiers"):
                    needs.add("group_tiers")
            elif kind == "group_members":
                needs.add("members")
                if r.get("group_tiers"):
                    needs.add("group_tiers")
            elif kind == "supporters":
                needs.add("supporters")
    return needs


# --------------------------------------------------------------------------- #
# DB half
# --------------------------------------------------------------------------- #
def _paid_group_tiers(s, group_ids: Iterable[int]) -> dict:
    """{group_id: tier_key} for groups whose live pool covers a paid tier."""
    from db.entitlements import effective_group_tiers

    return {int(gid): tier.key for gid, (tier, _total) in effective_group_tiers(s, group_ids).items()}


def _member_group_ids(s, user_id: int) -> set:
    """Groups the user belongs to, through a user row on the association table
    or through any player they own."""
    from db.models import Player, user_group_association as uga

    rows = (
        s.query(uga.c.group_id)
        .filter(uga.c.user_id == user_id)
        .union(
            s.query(uga.c.group_id)
            .join(Player, Player.player_id == uga.c.player_id)
            .filter(Player.user_id == user_id)
        )
        .all()
    )
    return {int(r[0]) for r in rows if r[0] is not None}


def _leader_roles(s, user_id: int) -> dict:
    """{group_id: role} from explicit grants, plus Discord server managers of a
    linked guild as ``admin`` (matching ``deps.resolve_group_role``, minus its
    superadmin bypass: staff are not every clan's leader)."""
    from db.models import Group, GroupAdmin
    from web_api.deps import discord_perms_grant_admin, manageable_guild_ids

    roles: dict = {}
    for gid, role in s.query(GroupAdmin.group_id, GroupAdmin.role).filter(GroupAdmin.user_id == user_id):
        roles[int(gid)] = "owner" if role == "owner" else "admin"

    guild_ids = [g for g in manageable_guild_ids(user_id) if g]
    if guild_ids:
        for (gid,) in s.query(Group.group_id).filter(Group.guild_id.in_(guild_ids)):
            gid = int(gid)
            if gid not in roles and discord_perms_grant_admin(s, gid):
                roles[gid] = "admin"
    return roles


def _is_paid(sub) -> bool:
    from db.entitlements import NON_REVENUE_PROVIDERS, subscription_is_live

    return subscription_is_live(sub) and (sub.provider or "") not in NON_REVENUE_PROVIDERS


def _supporter_facts(s, user_id: int) -> tuple[bool, set]:
    from db.models import GroupSubscription, UserSubscription

    paying = False
    tiers: set = set()
    sub = s.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
    if sub is not None and _is_paid(sub):
        paying = True
        if sub.tier_key:
            tiers.add(sub.tier_key)

    legs = s.query(GroupSubscription).filter(GroupSubscription.user_id == user_id).all()
    paid_groups = {int(leg.group_id) for leg in legs if _is_paid(leg)}
    if paid_groups:
        paying = True
        tiers.update(_paid_group_tiers(s, paid_groups).values())
    return paying, tiers


def load_viewer_facts(s, user_id: int, needs: set) -> ViewerFacts:
    facts = ViewerFacts(user_id=user_id)
    if "staff" in needs:
        from web_api.deps import is_developer, load_user

        facts.is_staff = is_developer(load_user(s, user_id))
    if "leaders" in needs:
        facts.leader_roles = _leader_roles(s, user_id)
    if "members" in needs:
        facts.member_group_ids = _member_group_ids(s, user_id)
    if "group_tiers" in needs:
        gids = set(facts.leader_roles) | facts.member_group_ids
        facts.group_tiers = _paid_group_tiers(s, gids) if gids else {}
    if "supporters" in needs:
        facts.is_supporter, facts.supporter_tiers = _supporter_facts(s, user_id)
    return facts


def _groups_for_rule(s, rule: dict) -> Optional[set]:
    """Group ids a leader/member rule is limited to, or None for "any group".
    Applies the tier filter by computing effective tiers once."""
    from db.models import Group

    ids = rule.get("group_ids") or []
    tiers = rule.get("group_tiers") or []
    if not tiers:
        return set(ids) if ids else None
    candidates = set(ids) if ids else {int(g) for (g,) in s.query(Group.group_id)}
    paid = _paid_group_tiers(s, candidates)
    return {gid for gid in candidates if paid.get(gid, FREE_TIER) in tiers}


def resolve_audience_user_ids(s, rules: list[dict]) -> tuple[Optional[set], list[str]]:
    """Everyone the rules reach right now, for the admin recipient count.

    Returns ``(user_ids, notes)``; ``user_ids`` is None for an ``everyone``
    audience (the caller counts the users table instead of materialising it).
    ``notes`` are caveats to show next to the number."""
    from db.models import (
        GroupAdmin,
        GroupSubscription,
        Player,
        User,
        UserSubscription,
        user_group_association as uga,
    )
    from sqlalchemy import or_

    notes: list[str] = []
    out: set = set()
    for rule in rules:
        kind = rule["type"]
        if kind == "everyone":
            return None, notes

        if kind == "staff":
            rows = s.query(User.user_id).filter(
                or_(User.is_superadmin.is_(True), User.is_developer.is_(True))
            )
            out.update(int(u) for (u,) in rows)

        elif kind == "users":
            ids = rule["user_ids"]
            rows = s.query(User.user_id).filter(User.user_id.in_(ids))
            found = {int(u) for (u,) in rows}
            missing = len(set(ids) - found)
            if missing:
                notes.append(f"{missing} picked account(s) no longer exist.")
            out.update(found)

        elif kind == "group_leaders":
            groups = _groups_for_rule(s, rule)
            if groups is not None and not groups:
                continue
            q = s.query(GroupAdmin.user_id, GroupAdmin.role)
            if groups is not None:
                q = q.filter(GroupAdmin.group_id.in_(groups))
            roles = set(rule["roles"])
            for uid, role in q:
                if ("owner" if role == "owner" else "admin") in roles:
                    out.add(int(uid))
            if "admin" in roles:
                notes.append(
                    "Discord server managers without a DropTracker admin role also see it, "
                    "but can't be counted ahead of time."
                )

        elif kind == "group_members":
            groups = _groups_for_rule(s, rule)
            if groups is not None and not groups:
                continue
            direct = s.query(uga.c.user_id).filter(uga.c.user_id.isnot(None))
            via_players = (
                s.query(Player.user_id)
                .join(uga, uga.c.player_id == Player.player_id)
                .filter(Player.user_id.isnot(None))
            )
            if groups is not None:
                direct = direct.filter(uga.c.group_id.in_(groups))
                via_players = via_players.filter(uga.c.group_id.in_(groups))
            out.update(int(u) for (u,) in direct.distinct())
            out.update(int(u) for (u,) in via_players.distinct())

        elif kind == "supporters":
            keys = set(rule.get("tier_keys") or [])
            for sub in s.query(UserSubscription).all():
                if _is_paid(sub) and (not keys or sub.tier_key in keys):
                    out.add(int(sub.user_id))
            legs = [
                leg
                for leg in s.query(GroupSubscription).filter(GroupSubscription.user_id.isnot(None))
                if _is_paid(leg)
            ]
            if legs:
                tier_by_group = _paid_group_tiers(s, {int(leg.group_id) for leg in legs})
                for leg in legs:
                    tier = tier_by_group.get(int(leg.group_id))
                    if not keys or tier in keys:
                        out.add(int(leg.user_id))

    return out, notes


def describe_audience(rules: list[dict], names: Optional[dict] = None) -> str:
    """One-line English summary for the admin list ("Clan owners of Tier 3
    groups + 2 people"). ``names`` maps ``("group", id)`` / ``("tier", key)``
    to display names when the caller has them."""
    names = names or {}

    def _groups(rule: dict) -> str:
        parts = []
        tiers = rule.get("group_tiers") or []
        ids = rule.get("group_ids") or []
        if tiers:
            parts.append(" or ".join(names.get(("tier", t), t) for t in tiers) + " groups")
        if ids:
            if len(ids) <= 3:
                parts.append(", ".join(names.get(("group", g), f"group {g}") for g in ids))
            else:
                parts.append(f"{len(ids)} groups")
        return " in ".join(parts) if parts else "any group"

    out = []
    for r in rules:
        kind = r["type"]
        if kind == "everyone":
            out.append("Everyone signed in")
        elif kind == "staff":
            out.append("Staff")
        elif kind == "users":
            n = len(r["user_ids"])
            out.append(f"{n} {'person' if n == 1 else 'people'}")
        elif kind == "group_leaders":
            who = "Clan owners" if r["roles"] == ["owner"] else (
                "Clan admins" if r["roles"] == ["admin"] else "Clan owners and admins"
            )
            out.append(f"{who} of {_groups(r)}")
        elif kind == "group_members":
            out.append(f"Members of {_groups(r)}")
        elif kind == "supporters":
            keys = r.get("tier_keys") or []
            out.append(
                "Supporters on " + " or ".join(names.get(("tier", k), k) for k in keys)
                if keys
                else "All supporters"
            )
    return " + ".join(out)
