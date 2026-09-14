"""Discord roles on the main server for subscription tiers and bug testers.

The database is the source of truth and the roles follow it:

* **Subscriber roles** (Patron / Sponsor / Supporter) go to a user who pays a
  live leg of a group whose pool reaches that tier. Comped (``manual``) and
  Nitro-credit legs pay nothing, so they make no one a subscriber.
* **Member roles** ("Patron (Member)" …) go to every member of such a group,
  using the same membership rule as Nitro attribution: direct user associations
  plus associations carried by the user's players.
* A person holds **one** tier role at most, the highest they qualify for:
  Patron > Sponsor > Supporter > Patron (Member) > Sponsor (Member) >
  Supporter (Member). That order is the order of :data:`ROLE_SPECS`.
* **Fanatic** follows a live personal (user-scope ``supporter``) subscription.
* **Bug Tester** follows an active ``bug_tester_helper`` badge on any of the
  user's players. That badge also grants the complimentary supporter perks
  (``db/entitlements.py``). The Fanatic role is not granted with it.

Role ids live in :data:`ROLE_MAP_PATH`, written by
``scripts/seed_discord_roles.py``. The seeder creates, renames, styles and
orders the roles. Nothing is synced until that map exists for the main guild,
so seeding is the deliberate step that switches the sync on.

Runtime: the webhook bot (``bots/webhook_bot.py``) runs the reconciler, because
listing guild members needs the privileged GUILD_MEMBERS intent and the core
bot's application does not have it. The core bot's ``/bug-tester`` command
(``commands/bug_tester.py``) writes the badge and then adds or removes the role
immediately. ``scripts/sync_discord_roles.py`` runs one pass by hand, as a dry
run by default.

Like ``services/nitro_attribution.py``, this module imports neither
``interactions`` nor ``web_api``: DB access is lazy and the Discord client is
duck-typed, so the planning logic is unit-testable in isolation.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

# Same default and override as services/nitro_attribution.MAIN_GUILD_ID.
MAIN_GUILD_ID = os.getenv("PRIMARY_GUILD_ID", "1172737525069135962").strip()

#: The badge that makes someone a Bug Tester (and grants comp supporter perks).
BUG_TESTER_BADGE_KEY = "bug_tester_helper"

#: role key -> Discord role id, plus the guild the ids belong to.
ROLE_MAP_PATH = Path(__file__).resolve().parents[1] / "data" / "discord_roles.json"

#: Set by anything that wants a pass sooner than the next scheduled one.
SYNC_REQUEST_KEY = "discord_roles:sync_requested"

#: Audit-log reason on every role change the sync makes.
SYNC_REASON = "DropTracker role sync"

#: Groups 1 and 2 are the template group and the every-player pseudo-group.
#: A subscription row on either must never hand its tier to the whole site.
_MIN_REAL_GROUP_ID = 2

GROUP_PAYER = "group_payer"
GROUP_MEMBER = "group_member"
USER_SUBSCRIBER = "user_subscriber"
BUG_TESTER = "bug_tester"
_TIER_GRANTS = (GROUP_PAYER, GROUP_MEMBER)


@dataclass(frozen=True)
class RoleSpec:
    key: str
    name: str
    grant: str
    #: The subscription tier this role stands for (tier roles and Fanatic).
    tier_key: Optional[str] = None
    primary_color: int = 0
    #: A second colour makes a gradient.
    secondary_color: Optional[int] = None
    hoist: bool = False
    unicode_emoji: Optional[str] = None
    #: Copy the image icon of another spec's role when this role has none.
    icon_from: Optional[str] = None
    #: A role that already exists on the server and becomes this one.
    adopt_role_id: Optional[str] = None


#: Top to bottom, which is both the order on the server and, among the tier
#: roles, the precedence when someone qualifies for several. Colours match the
#: website's tier flair (bronze / gold / amethyst, apps/web/lib/tier-flair.ts);
#: subscribers get a gradient, members the flat colour.
ROLE_SPECS: Tuple[RoleSpec, ...] = (
    RoleSpec("patron", "Patron", GROUP_PAYER, "t3", 0xD3A7F2, 0x9B59B6, hoist=True,
             adopt_role_id="1469455844679749737"),   # was "Supporter [T3]"
    RoleSpec("sponsor", "Sponsor", GROUP_PAYER, "t2", 0xFFD966, 0xE6A817, hoist=True,
             adopt_role_id="1469455599283470528"),   # was "Supporter [T2]"
    RoleSpec("supporter", "Supporter", GROUP_PAYER, "basic", 0xD99A5B, 0xA0522D, hoist=True,
             adopt_role_id="1469455370140385504"),   # was "Supporter [T1]"
    RoleSpec("fanatic", "Fanatic", USER_SUBSCRIBER, "supporter", 0xFFD966, 0xFFF1B8, hoist=True,
             adopt_role_id="1210765189625151592"),   # was the legacy "Supporter"
    RoleSpec("bug_tester", "Bug Tester", BUG_TESTER, None, 0x2ECC71, None, hoist=True,
             unicode_emoji="\U0001F41B", adopt_role_id="1424357463406149633"),
    RoleSpec("patron_member", "Patron (Member)", GROUP_MEMBER, "t3", 0xD3A7F2, icon_from="patron"),
    RoleSpec("sponsor_member", "Sponsor (Member)", GROUP_MEMBER, "t2", 0xFFD966, icon_from="sponsor"),
    RoleSpec("supporter_member", "Supporter (Member)", GROUP_MEMBER, "basic", 0xD99A5B,
             icon_from="supporter"),
)
SPECS_BY_KEY: Dict[str, RoleSpec] = {s.key: s for s in ROLE_SPECS}

#: The divider role the block sits directly beneath ("━━━━━━━━", above the
#: old Supporter role). Without it the seeder leaves the order alone.
ORDER_ANCHOR_ROLE_ID = "1210765188312338534"


def sync_mode() -> str:
    """``on`` (default), ``dry-run`` or ``off``, from ``DISCORD_ROLE_SYNC``.

    On by default because the role map is the real switch: without a seeded
    map there is nothing to sync.
    """
    raw = os.getenv("DISCORD_ROLE_SYNC", "").strip().lower()
    if raw in ("off", "false", "0", "no", "disabled"):
        return "off"
    if raw in ("dry-run", "dry_run", "dryrun", "dry"):
        return "dry-run"
    return "on"


def max_removals_per_pass() -> int:
    """Above this many removals, a pass adds but removes nothing.

    A failure that makes the database look empty would otherwise strip every
    managed role on the server in one pass. A real change that large (a big
    group losing its subscription) is applied by hand with
    ``scripts/sync_discord_roles.py --apply --allow-mass-removal``.
    """
    try:
        return max(0, int(os.getenv("DISCORD_ROLE_SYNC_MAX_REMOVALS", "40")))
    except (TypeError, ValueError):
        return 40


def normalize_discord_id(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text if text.isdigit() and text != "0" else None


# --------------------------------------------------------------------------- #
# Role map
# --------------------------------------------------------------------------- #
def load_role_map(path: Path = ROLE_MAP_PATH, guild_id: str = MAIN_GUILD_ID) -> Dict[str, str]:
    """``{role key: role id}`` for ``guild_id``, or ``{}`` when not seeded.

    A map written for another guild (a copied checkout, a dev server) reads
    as empty rather than pointing the sync at ids that are not there.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or str(data.get("guild_id")) != str(guild_id):
        return {}
    roles = data.get("roles")
    if not isinstance(roles, dict):
        return {}
    return {
        str(key): str(rid) for key, rid in roles.items()
        if key in SPECS_BY_KEY and normalize_discord_id(rid)
    }


def write_role_map(roles: Mapping[str, str], path: Path = ROLE_MAP_PATH,
                   guild_id: str = MAIN_GUILD_ID) -> None:
    payload = {
        "guild_id": str(guild_id),
        "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "roles": {s.key: str(roles[s.key]) for s in ROLE_SPECS if s.key in roles},
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Who should hold what (database side)
# --------------------------------------------------------------------------- #
@dataclass
class RoleInputs:
    #: group_id -> key of the tier its live pool reaches (paid tiers only)
    group_tier: Dict[int, str] = field(default_factory=dict)
    #: group_id -> users paying a live, revenue-bearing leg of it
    group_payers: Dict[int, Set[int]] = field(default_factory=dict)
    #: group_id -> member user ids
    group_members: Dict[int, Set[int]] = field(default_factory=dict)
    #: user_id -> tier key of their live personal subscription
    user_tier: Dict[int, str] = field(default_factory=dict)
    #: users holding an active Bug Tester badge
    bug_testers: Set[int] = field(default_factory=set)
    #: user_id -> Discord id
    discord_ids: Dict[int, str] = field(default_factory=dict)


def load_role_inputs(session) -> RoleInputs:
    """Read everything :func:`desired_role_keys` needs in a handful of queries."""
    from db.entitlements import (
        NON_REVENUE_PROVIDERS,
        effective_group_tiers,
        subscription_is_live,
    )
    from db.models import (
        Badge,
        GroupSubscription,
        Player,
        PlayerBadge,
        User,
        UserSubscription,
        user_group_association as uga,
    )

    live_statuses = ("active", "trialing")
    inputs = RoleInputs()

    legs = [
        leg for leg in session.query(GroupSubscription)
        .filter(GroupSubscription.status.in_(live_statuses)).all()
        if subscription_is_live(leg)
    ]
    group_ids = {int(leg.group_id) for leg in legs if int(leg.group_id) > _MIN_REAL_GROUP_ID}
    for gid, (tier, _total) in effective_group_tiers(session, group_ids).items():
        if int(gid) > _MIN_REAL_GROUP_ID:
            inputs.group_tier[int(gid)] = tier.key

    for leg in legs:
        gid = int(leg.group_id)
        if leg.user_id is None or gid not in inputs.group_tier:
            continue
        if (leg.provider or "").lower() in NON_REVENUE_PROVIDERS:
            continue
        inputs.group_payers.setdefault(gid, set()).add(int(leg.user_id))

    if inputs.group_tier:
        gids = sorted(inputs.group_tier)
        direct = session.query(uga.c.group_id, uga.c.user_id).filter(
            uga.c.group_id.in_(gids), uga.c.user_id.isnot(None))
        via_players = (
            session.query(uga.c.group_id, Player.user_id)
            .join(Player, Player.player_id == uga.c.player_id)
            .filter(uga.c.group_id.in_(gids), Player.user_id.isnot(None))
        )
        for gid, uid in direct.union(via_players).all():
            if gid is not None and uid is not None:
                inputs.group_members.setdefault(int(gid), set()).add(int(uid))

    for sub in (session.query(UserSubscription)
                .filter(UserSubscription.status.in_(live_statuses)).all()):
        if sub.tier_key and subscription_is_live(sub):
            inputs.user_tier[int(sub.user_id)] = sub.tier_key

    tester_rows = (
        session.query(Player.user_id)
        .join(PlayerBadge, PlayerBadge.player_id == Player.player_id)
        .join(Badge, Badge.badge_id == PlayerBadge.badge_id)
        .filter(
            Badge.key == BUG_TESTER_BADGE_KEY,
            Badge.active == True,  # noqa: E712
            PlayerBadge.status == "active",
            Player.user_id.isnot(None),
        )
        .distinct()
        .all()
    )
    inputs.bug_testers = {int(uid) for (uid,) in tester_rows}

    wanted: Set[int] = set(inputs.user_tier) | inputs.bug_testers
    for users in list(inputs.group_payers.values()) + list(inputs.group_members.values()):
        wanted |= users
    ordered = sorted(wanted)
    for start in range(0, len(ordered), 1000):
        chunk = ordered[start:start + 1000]
        for uid, discord_id in (session.query(User.user_id, User.discord_id)
                                .filter(User.user_id.in_(chunk)).all()):
            normalized = normalize_discord_id(discord_id)
            if normalized:
                inputs.discord_ids[int(uid)] = normalized
    return inputs


def desired_role_keys(inputs: RoleInputs,
                      specs: Sequence[RoleSpec] = ROLE_SPECS) -> Dict[str, Set[str]]:
    """``{discord_id: {role key}}`` — who should hold which managed role."""
    tier_specs = [s for s in specs if s.grant in _TIER_GRANTS]
    rank = {s.key: index for index, s in enumerate(tier_specs)}
    payer_role = {s.tier_key: s.key for s in tier_specs if s.grant == GROUP_PAYER}
    member_role = {s.tier_key: s.key for s in tier_specs if s.grant == GROUP_MEMBER}

    candidates: Dict[int, Set[str]] = defaultdict(set)
    for gid, tier_key in inputs.group_tier.items():
        if gid <= _MIN_REAL_GROUP_ID:
            continue
        if tier_key in payer_role:
            for uid in inputs.group_payers.get(gid, ()):
                candidates[uid].add(payer_role[tier_key])
        if tier_key in member_role:
            for uid in inputs.group_members.get(gid, ()):
                candidates[uid].add(member_role[tier_key])

    by_user: Dict[int, Set[str]] = defaultdict(set)
    for uid, keys in candidates.items():
        by_user[uid].add(min(keys, key=rank.__getitem__))

    for spec in specs:
        if spec.grant == USER_SUBSCRIBER:
            for uid, tier_key in inputs.user_tier.items():
                if tier_key == spec.tier_key:
                    by_user[uid].add(spec.key)
        elif spec.grant == BUG_TESTER:
            for uid in inputs.bug_testers:
                by_user[uid].add(spec.key)

    out: Dict[str, Set[str]] = {}
    for uid, keys in by_user.items():
        discord_id = inputs.discord_ids.get(uid)
        if discord_id:
            out.setdefault(discord_id, set()).update(keys)
    return out


# --------------------------------------------------------------------------- #
# What to change (pure)
# --------------------------------------------------------------------------- #
@dataclass
class RolePlan:
    adds: List[Tuple[str, str]] = field(default_factory=list)      # (discord_id, role key)
    removes: List[Tuple[str, str]] = field(default_factory=list)
    #: Removals withheld by the mass-removal guard.
    held_back: List[Tuple[str, str]] = field(default_factory=list)
    #: Users owed a role who are not on the server.
    not_in_guild: int = 0


def plan_role_changes(desired: Mapping[str, Set[str]],
                      members: Iterable[Mapping[str, Any]],
                      role_map: Mapping[str, str],
                      max_removals: Optional[int] = None) -> RolePlan:
    """Diff the members' managed roles against ``desired``.

    ``members`` are raw guild member objects (``{"user": {...}, "roles": [...]}``).
    Only roles in ``role_map`` are ever touched, bots are skipped, and a
    member's other roles are never read beyond that. With ``max_removals``,
    a plan with more removals than that keeps its additions and holds every
    removal back.
    """
    key_by_role_id = {str(rid): key for key, rid in role_map.items()}
    order = {s.key: index for index, s in enumerate(ROLE_SPECS)}
    plan = RolePlan()
    seen: Set[str] = set()
    for member in members:
        user = member.get("user") or {}
        discord_id = normalize_discord_id(user.get("id"))
        if not discord_id or user.get("bot"):
            continue
        seen.add(discord_id)
        held = {key_by_role_id[str(r)] for r in (member.get("roles") or ()) if str(r) in key_by_role_id}
        wanted = {key for key in desired.get(discord_id, ()) if key in role_map}
        plan.adds.extend((discord_id, key) for key in sorted(wanted - held, key=order.__getitem__))
        plan.removes.extend((discord_id, key) for key in sorted(held - wanted, key=order.__getitem__))
    plan.not_in_guild = sum(
        1 for discord_id, keys in desired.items()
        if discord_id not in seen and any(k in role_map for k in keys)
    )
    if max_removals is not None and len(plan.removes) > max_removals:
        plan.held_back, plan.removes = plan.removes, []
    return plan


# --------------------------------------------------------------------------- #
# Discord side (duck-typed interactions HTTPClient)
# --------------------------------------------------------------------------- #
async def fetch_guild_members(http, guild_id: str = MAIN_GUILD_ID, page_limit: int = 1000,
                              page_pause: float = 0.4) -> List[Dict[str, Any]]:
    """Every member of the guild as raw member objects (GUILD_MEMBERS intent)."""
    members: List[Dict[str, Any]] = []
    after: Optional[str] = None
    while True:
        page = await http.list_members(guild_id, limit=page_limit, after=after)
        if not page:
            break
        members.extend(page)
        if len(page) < page_limit:
            break
        last = (page[-1].get("user") or {}).get("id")
        if not last:
            break
        after = str(last)
        if page_pause:
            await asyncio.sleep(page_pause)
    return members


def _status_of(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


async def apply_role_plan(http, plan: RolePlan, role_map: Mapping[str, str],
                          guild_id: str = MAIN_GUILD_ID, op_limit: int = 250,
                          pause: float = 0.2, reason: str = SYNC_REASON) -> Dict[str, int]:
    """Carry out a plan. Returns counts; never raises for a single failed change.

    Additions go first so that a pass cut short by ``op_limit`` has granted
    what people are owed before it withdraws anything.
    """
    stats = {"added": 0, "removed": 0, "not_member": 0, "forbidden": 0, "errors": 0, "deferred": 0}
    operations = [("add", d, k) for d, k in plan.adds] + [("remove", d, k) for d, k in plan.removes]
    for index, (action, discord_id, key) in enumerate(operations):
        if index >= op_limit:
            stats["deferred"] = len(operations) - index
            break
        role_id = role_map[key]
        try:
            if action == "add":
                await http.add_guild_member_role(guild_id, discord_id, role_id, reason=reason)
                stats["added"] += 1
            else:
                await http.remove_guild_member_role(guild_id, discord_id, role_id, reason=reason)
                stats["removed"] += 1
        except Exception as exc:  # one bad member must not stop the pass
            status = _status_of(exc)
            if status == 404:
                stats["not_member"] += 1
            elif status == 403:
                stats["forbidden"] += 1
            else:
                stats["errors"] += 1
                print(f"[roles] {action} {key} for {discord_id} failed: {exc}")
        if pause:
            await asyncio.sleep(pause)
    return stats


def request_sync() -> bool:
    """Ask the webhook bot for a pass on its next poll. Best-effort; never raises."""
    try:
        from utils.redis import RedisClient

        RedisClient().set(SYNC_REQUEST_KEY, "1")
        return True
    except Exception:
        return False


def consume_sync_request() -> bool:
    """True (and clears the flag) when a pass was requested."""
    try:
        from utils.redis import RedisClient

        client = RedisClient().client
        return bool(client and client.getdel(SYNC_REQUEST_KEY))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Role setup (used by scripts/seed_discord_roles.py)
# --------------------------------------------------------------------------- #
def resolve_spec_roles(roles: Sequence[Mapping[str, Any]], role_map: Mapping[str, str],
                       specs: Sequence[RoleSpec] = ROLE_SPECS) -> Dict[str, Optional[Mapping[str, Any]]]:
    """Match each spec to an existing role: recorded id, then adopted id, then exact name.

    Each pass completes for every spec before the next begins, so a name can
    only match a role no id has claimed. The old "Supporter" role is Fanatic's
    by id before the new Supporter spec ever looks for the name. Roles managed
    by an integration can never be adopted by name.
    """
    by_id = {str(r["id"]): r for r in roles}
    claimed: Set[str] = set()
    out: Dict[str, Optional[Mapping[str, Any]]] = {s.key: None for s in specs}

    def _claim(spec: RoleSpec, role_id: Optional[str]) -> None:
        if out[spec.key] is None and role_id and role_id in by_id and role_id not in claimed:
            out[spec.key] = by_id[role_id]
            claimed.add(role_id)

    for spec in specs:
        _claim(spec, role_map.get(spec.key))
    for spec in specs:
        _claim(spec, spec.adopt_role_id)
    by_position = sorted(roles, key=lambda r: (-int(r["position"]), int(r["id"])))
    for spec in specs:
        if out[spec.key] is not None:
            continue
        match = next((r for r in by_position
                      if r.get("name") == spec.name and not r.get("managed")
                      and str(r["id"]) not in claimed), None)
        if match is not None:
            _claim(spec, str(match["id"]))
    return out


#: Gradient role colours are gated on this guild feature. Without it Discord
#: answers 403 "Missing guild feature", so the specs fall back to their flat
#: primary colour.
GRADIENT_FEATURE = "ENHANCED_ROLE_COLORS"


def spec_colors(spec: RoleSpec, allow_gradient: bool = True) -> Dict[str, Optional[int]]:
    secondary = spec.secondary_color if allow_gradient else None
    return {"primary_color": spec.primary_color, "secondary_color": secondary, "tertiary_color": None}


def role_patch(spec: RoleSpec, role: Mapping[str, Any], allow_gradient: bool = True) -> Dict[str, Any]:
    """The fields of ``role`` that differ from ``spec`` (icon images excluded).

    An existing image icon is kept. Only a role with no icon at all, or a spec
    with a Unicode emoji, gets a new one; :func:`icon_to_copy` covers copying
    another role's icon.
    """
    patch: Dict[str, Any] = {}
    if role.get("name") != spec.name:
        patch["name"] = spec.name
    colors = role.get("colors") or {}
    current = {
        "primary_color": colors.get("primary_color", role.get("color")),
        "secondary_color": colors.get("secondary_color"),
        "tertiary_color": colors.get("tertiary_color"),
    }
    if current != spec_colors(spec, allow_gradient):
        patch["colors"] = spec_colors(spec, allow_gradient)
    if bool(role.get("hoist")) != spec.hoist:
        patch["hoist"] = spec.hoist
    if role.get("mentionable"):
        patch["mentionable"] = False
    if spec.unicode_emoji and (role.get("unicode_emoji") != spec.unicode_emoji or role.get("icon")):
        patch["unicode_emoji"] = spec.unicode_emoji
        patch["icon"] = None
    return patch


def icon_to_copy(spec: RoleSpec, role: Optional[Mapping[str, Any]],
                 source: Optional[Mapping[str, Any]]) -> Optional[str]:
    """``"emoji"`` or ``"image"`` when ``role`` should take ``source``'s icon, else None.

    Only a role with no icon of its own takes one, so a later hand-picked icon
    survives a re-run.
    """
    if not spec.icon_from or source is None:
        return None
    if role is not None and (role.get("icon") or role.get("unicode_emoji")):
        return None
    if source.get("unicode_emoji"):
        return "emoji"
    if source.get("icon"):
        return "image"
    return None


def current_role_order(roles: Sequence[Mapping[str, Any]], guild_id: str = MAIN_GUILD_ID) -> List[str]:
    """Role ids top to bottom, ``@everyone`` excluded.

    Stored positions are neither dense nor unique: a deleted role leaves a gap,
    and Discord creates every new role at position 1, level with whatever is
    already there. Equal positions rank by id, lower id higher (the documented
    tie-break; discord.py's ``Role.__lt__`` and discord.js' ``discordSort``
    agree).
    """
    ordered = sorted((r for r in roles if str(r["id"]) != str(guild_id)),
                     key=lambda r: (-int(r["position"]), int(r["id"])))
    return [str(r["id"]) for r in ordered]


def plan_role_order(roles: Sequence[Mapping[str, Any]], block_ids: Sequence[str],
                    anchor_id: str, bot_top_position: int,
                    guild_id: str = MAIN_GUILD_ID) -> Optional[List[Dict[str, Any]]]:
    """The positions that put ``block_ids`` directly beneath ``anchor_id``, in order.

    Returns ``[]`` when the block is already in place, and ``None`` when it
    can't be planned (the anchor or a block role missing, or at or above the
    bot's highest role). Otherwise every role but ``@everyone`` is numbered
    1..n from the bottom in the new order. That is how discord.js'
    ``setPosition`` sends it, and it is the only form that says unambiguously
    where everything lands: with gaps and ties, a partial update or a
    renumbering of only the roles below the bot can need more numbers than
    exist under the bot's own role. Only block roles move, so every role at or
    above the bot keeps its relative order, which is what Discord checks
    against the bot's hierarchy.
    """
    ids = current_role_order(roles, guild_id)
    by_id = {str(r["id"]): r for r in roles}
    block = [str(i) for i in block_ids]
    for role_id in [str(anchor_id)] + block:
        if role_id not in by_id or role_id == str(guild_id) \
                or int(by_id[role_id]["position"]) >= int(bot_top_position):
            return None
    rest = [i for i in ids if i not in set(block)]
    anchor_index = rest.index(str(anchor_id))
    target = rest[:anchor_index + 1] + block + rest[anchor_index + 1:]
    if target == ids:
        return []
    return [{"id": role_id, "position": len(target) - index} for index, role_id in enumerate(target)]


# --------------------------------------------------------------------------- #
# Bug Tester grant / withdraw (shared by /bug-tester and the backfill script)
# --------------------------------------------------------------------------- #
@dataclass
class BugTesterChange:
    #: granted | already | revoked | not_held | no_user | no_players | no_badge
    status: str
    user_id: Optional[int] = None
    player_names: List[str] = field(default_factory=list)


def pick_badge_player(players: Sequence[Any]) -> Optional[Any]:
    """The account the badge is shown on: the highest total level, earliest on a tie.

    This matches every award made by hand so far, one award per person on
    their main.
    """
    if not players:
        return None
    return min(players, key=lambda p: (-(p.total_level or 0), int(p.player_id)))


def _active_tester_awards(session, user_id: int) -> List[Tuple[Any, Any]]:
    from db.models import Badge, Player, PlayerBadge

    return (
        session.query(PlayerBadge, Player)
        .join(Player, Player.player_id == PlayerBadge.player_id)
        .join(Badge, Badge.badge_id == PlayerBadge.badge_id)
        .filter(
            Badge.key == BUG_TESTER_BADGE_KEY,
            PlayerBadge.status == "active",
            Player.user_id == user_id,
        )
        .all()
    )


def grant_bug_tester(session, discord_id: str, actor_user_id: int,
                     note: Optional[str] = None, dry_run: bool = False) -> BugTesterChange:
    """Award the badge on the user's main account. The caller commits.

    ``actor_user_id`` is required: ``award_badge`` treats an award with no
    actor as automatic, and a revoked automatic award blocks it forever.
    ``dry_run`` makes every read and reports the outcome without writing.
    """
    if actor_user_id is None:
        raise ValueError("actor_user_id is required")
    from db.models import AuditLog, Badge, Player, User

    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if user is None:
        return BugTesterChange("no_user")
    held = _active_tester_awards(session, user.user_id)
    if held:
        return BugTesterChange("already", user.user_id, [p.player_name for _, p in held])
    badge = (session.query(Badge)
             .filter(Badge.key == BUG_TESTER_BADGE_KEY, Badge.active == True)  # noqa: E712
             .first())
    if badge is None:
        return BugTesterChange("no_badge", user.user_id)
    player = pick_badge_player(session.query(Player).filter(Player.user_id == user.user_id).all())
    if player is None:
        return BugTesterChange("no_players", user.user_id)
    if dry_run:
        return BugTesterChange("granted", user.user_id, [player.player_name])

    from services.badges import award_badge

    context = {"note": note} if note else None
    award = award_badge(session, badge, player.player_id, slot_key=f"p:{player.player_id}",
                        context=context, awarded_by=actor_user_id)
    if award is None:  # lost a race with another grant
        return BugTesterChange("already", user.user_id, [player.player_name])
    session.add(AuditLog(
        actor_user_id=actor_user_id, group_id=None, action="badge.award",
        target=f"player:{player.player_id}",
        after=json.dumps({"award_id": int(award.id), "badge_key": BUG_TESTER_BADGE_KEY,
                          "player_id": int(player.player_id), "player_name": player.player_name,
                          "note": note, "via": "discord"}),
    ))
    return BugTesterChange("granted", user.user_id, [player.player_name])


def revoke_bug_tester(session, discord_id: str, actor_user_id: Optional[int]) -> BugTesterChange:
    """Revoke every active Bug Tester award on the user's players. The caller commits."""
    from db.models import AuditLog, User
    from services.badges import revoke_badge

    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if user is None:
        return BugTesterChange("no_user")
    held = _active_tester_awards(session, user.user_id)
    if not held:
        return BugTesterChange("not_held", user.user_id)
    for award, player in held:
        before = {"award_id": int(award.id), "badge_key": BUG_TESTER_BADGE_KEY,
                  "player_id": int(player.player_id), "status": award.status, "via": "discord"}
        revoke_badge(session, award)
        session.add(AuditLog(actor_user_id=actor_user_id, group_id=None, action="badge.revoke",
                             target=f"player:{player.player_id}", before=json.dumps(before)))
    return BugTesterChange("revoked", user.user_id, [p.player_name for _, p in held])


def invalidate_user_perks(user_id: Optional[int]) -> None:
    """Drop this process's cached supporter perks for the user (the badge grants some)."""
    if user_id is None:
        return
    try:
        from db.entitlements import invalidate_user_entitlement_cache

        invalidate_user_entitlement_cache(int(user_id))
    except Exception:
        pass
