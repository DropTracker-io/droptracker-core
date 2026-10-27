"""Clan rank → Discord role sync.

A group maps the in-game clan ranks its members hold to roles in its Discord
server, and the core bot keeps those roles matched to the ranks. v1 reads the
ranks from **WiseOldMan**: the hourly membership sync already caches every
member's WOM group role (``utils/clan_ranks.py``, ``clanrank:{wom_id}``), and
WOM's roles are the OSRS clan rank list. Only members who have claimed an RSN
(``players.user_id`` → ``users.discord_id``) can be given a role; there is no
other way to know which Discord account is which clanmate.

Configuration is one JSON document per group in ``group_configurations``
(key :data:`CONFIG_KEY`, always in ``long_value``). It is not a registry
field: the website edits it through its own panel and route
(``web_api/routes/rank_roles.py``) because it is a list, not a value::

    {"enabled": true, "mode": "single", "create_roles": true,
     "exempt_role_id": null,
     "ranks": [{"rank": "owner", "role_id": "…", "name": "Owner",
                "create": false, "created": false, "delete_when_empty": false},
               …]}

``ranks`` is in priority order, highest first. The leader sets that order,
because WOM reports a rank's name but not where it sits in the clan.

Rules the owner settled on (dev tracker #38, t383):

* A member with several accounts in the clan gets the role of their highest
  rank.
* ``single`` mode gives one role; ``cumulative`` gives the rank's role plus
  every mapped rank below it.
* A rank with no role falls back to the next mapped rank below it.
* Strict: a mapped role a member shouldn't have is taken off, including one
  staff gave by hand, unless the member holds the exempt role. Leaving the
  clan or being demoted removes the old role. Roles not in the map are never
  touched.
* With ``create_roles`` on, a rank set to "create" gets a new role (no
  permissions, below the bot). A role with the rank's name is reused rather
  than duplicated.
* ``delete_when_empty`` only ever deletes a role DropTracker created, and
  only after it has had no members for :data:`DELETE_GRACE_SECONDS`.
* One group per Discord server runs the sync.

Safety: a missing rank cache (WOM down, the 6-hour TTL lapsed) skips the
group instead of reading as "nobody has a rank", and a pass that would take
roles off an unusual number of members without the map having changed holds
those removals back until a leader presses "Sync now".

Runtime: the core bot, from ``bots/main.py``'s ``rank_role_sync`` task. The
core app has no GUILD_MEMBERS intent, so it never lists a server; it starts
from our roster and fetches members one at a time, as
``services/auto_nickname.py`` does. Everything above the Discord section is
pure and unit-tested.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

CONFIG_KEY = "rank_role_sync"

MODES = ("single", "cumulative")

#: WOM's role for a clan member with no rank. ``utils.clan_ranks`` never stores
#: it, so it stands for "on the roster, absent from the rank cache".
MEMBER_RANK = "member"

#: Upper bounds on what a leader can save. OSRS has ~270 rank icons and
#: Discord caps role names at 100 characters.
MAX_RANKS = 300
MAX_ROLE_NAME = 100

#: A role DropTracker created is deleted only after it has been empty this long.
DELETE_GRACE_SECONDS = 3 * 24 * 60 * 60

#: Changed-members pass for every enabled group; a full pass (every linked
#: member fetched, which is what catches hand-granted roles) once a day.
ROUTINE_INTERVAL_SECONDS = 30 * 60
FULL_INTERVAL_SECONDS = 24 * 60 * 60

#: Removal guard: hold back a pass's removals when more members than this
#: would lose a role and the map has not changed since the last pass.
GUARD_MIN_MEMBERS = 10
GUARD_FRACTION = 0.25

#: Discord writes per group per pass. What is left over runs next pass.
OP_LIMIT = 400
OP_PAUSE_SECONDS = 0.25
FETCH_PAUSE_SECONDS = 0.1

REASON = "DropTracker: clan rank sync"

#: Permissions a synced role may not carry. WOM ranks are not an
#: authentication system: a rank must never hand out control of the server.
ADMINISTRATOR = 1 << 3
MANAGE_ROLES = 1 << 28
FORBIDDEN_PERMISSIONS = (
    (1 << 1, "Kick Members"),
    (1 << 2, "Ban Members"),
    (ADMINISTRATOR, "Administrator"),
    (1 << 4, "Manage Channels"),
    (1 << 5, "Manage Server"),
    (MANAGE_ROLES, "Manage Roles"),
    (1 << 29, "Manage Webhooks"),
)

# Redis keys.
REQUESTS_KEY = "rankroles:requests"  # hash group_id -> "changed" | "full" | "force"


def status_key(group_id: int) -> str:
    return f"rankroles:status:{int(group_id)}"


def applied_key(group_id: int) -> str:
    """discord_id -> comma-joined role ids we last confirmed they hold."""
    return f"rankroles:applied:{int(group_id)}"


def empty_key(group_id: int) -> str:
    """rank -> epoch seconds its created role was first seen empty."""
    return f"rankroles:empty:{int(group_id)}"


STATUS_TTL_SECONDS = 14 * 24 * 60 * 60

_SNOWFLAKE_RE = re.compile(r"^\d{15,21}$")
_NON_KEY_RE = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class RankRoleConfigError(ValueError):
    """A config a leader tried to save is not valid. ``errors`` lists why."""

    def __init__(self, errors: Sequence[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def normalize_rank(rank) -> str:
    """Same key as ``utils.rank_emojis.normalize_rank``: WOM's ``deputy_owner``
    and the game's ``Deputy Owner`` agree."""
    return _NON_KEY_RE.sub("_", str(rank or "").strip().lower()).strip("_")


def rank_label(rank) -> str:
    """``deputy_owner`` → ``Deputy Owner``; the no-rank member → ``Clan Member``."""
    key = normalize_rank(rank)
    if key == MEMBER_RANK:
        return "Clan Member"
    return " ".join(part.capitalize() for part in key.split("_") if part) or str(rank)


def _snowflake(value) -> Optional[str]:
    text = str(value or "").strip()
    return text if _SNOWFLAKE_RE.match(text) else None


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def default_config() -> dict:
    return {"enabled": False, "mode": "single", "create_roles": False,
            "exempt_role_id": None, "ranks": []}


def parse_config(raw) -> dict:
    """Stored JSON (or a dict) → a normalized config. Never raises: anything
    unreadable reads as the default, which is disabled."""
    data = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    out = default_config()
    out["enabled"] = _bool(data.get("enabled"))
    mode = str(data.get("mode") or "single").strip().lower()
    out["mode"] = mode if mode in MODES else "single"
    out["create_roles"] = _bool(data.get("create_roles"))
    out["exempt_role_id"] = _snowflake(data.get("exempt_role_id"))
    seen: Set[str] = set()
    for entry in data.get("ranks") or []:
        if not isinstance(entry, dict):
            continue
        rank = normalize_rank(entry.get("rank"))
        if not rank or rank in seen:
            continue
        seen.add(rank)
        name = str(entry.get("name") or "").strip()[:MAX_ROLE_NAME] or rank_label(rank)
        role_id = _snowflake(entry.get("role_id"))
        out["ranks"].append({
            "rank": rank,
            "role_id": role_id,
            "name": name,
            "create": _bool(entry.get("create")),
            "created": _bool(entry.get("created")) and role_id is not None,
            "delete_when_empty": _bool(entry.get("delete_when_empty")),
        })
        if len(out["ranks"]) >= MAX_RANKS:
            break
    return out


def dump_config(config: Mapping) -> str:
    return json.dumps(config, separators=(",", ":"), sort_keys=True)


def config_fingerprint(config: Mapping) -> str:
    """Changes when anything that decides who gets which role changes."""
    relevant = {
        "mode": config.get("mode"),
        "exempt": config.get("exempt_role_id"),
        "ranks": [(e["rank"], e.get("role_id")) for e in config.get("ranks") or ()],
    }
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


def role_permission_problem(permissions) -> Optional[str]:
    """Why a role with these permission bits may not be synced, or None."""
    try:
        bits = int(permissions or 0)
    except (TypeError, ValueError):
        return None
    held = [label for flag, label in FORBIDDEN_PERMISSIONS if bits & flag]
    if held:
        return f"it has the {', '.join(held)} permission{'s' if len(held) > 1 else ''}"
    return None


def validate_config(data: Mapping, *, guild_id=None, stored: Optional[Mapping] = None,
                    roles: Optional[Sequence[Mapping]] = None) -> dict:
    """A leader's save → a config to store. Raises :class:`RankRoleConfigError`.

    ``stored`` is the current config: ``created`` is bot-owned and only
    carries over for an unchanged role id, so a client can never mark a role
    it picked as one we created (and so deletable). ``roles`` is the guild's
    role list when the cache has it (``[{id, name, position, permissions?}]``);
    without it, role existence is left to the bot, which reports problems."""
    if not isinstance(data, Mapping):
        raise RankRoleConfigError(["The rank roles settings are malformed."])
    errors: List[str] = []
    stored = parse_config(stored or {})
    created_before = {e["rank"]: e["role_id"] for e in stored["ranks"] if e.get("created")}

    mode = str(data.get("mode") or "single").strip().lower()
    if mode not in MODES:
        errors.append("Choose one role per member or a ladder of roles.")
    exempt_raw = data.get("exempt_role_id")
    exempt = _snowflake(exempt_raw)
    if exempt_raw not in (None, "") and exempt is None:
        errors.append("The exempt role isn't a valid Discord role.")
    create_roles = _bool(data.get("create_roles"))

    raw_ranks = data.get("ranks")
    if raw_ranks is None:
        raw_ranks = []
    if not isinstance(raw_ranks, list):
        errors.append("The rank list is malformed.")
        raw_ranks = []
    if len(raw_ranks) > MAX_RANKS:
        errors.append(f"At most {MAX_RANKS} ranks can be listed.")

    role_by_id = {str(r.get("id")): r for r in roles or () if r.get("id") is not None}
    guild = str(guild_id) if guild_id is not None else None
    ranks: List[dict] = []
    seen_ranks: Set[str] = set()
    role_owner: Dict[str, str] = {}
    for entry in raw_ranks[:MAX_RANKS]:
        if not isinstance(entry, Mapping):
            errors.append("A rank entry is malformed.")
            continue
        rank = normalize_rank(entry.get("rank"))
        if not rank:
            errors.append("A rank entry has no rank.")
            continue
        label = rank_label(rank)
        if rank in seen_ranks:
            errors.append(f"{label} is listed twice.")
            continue
        seen_ranks.add(rank)
        name = str(entry.get("name") or "").strip()
        if len(name) > MAX_ROLE_NAME:
            errors.append(f"The role name for {label} is longer than {MAX_ROLE_NAME} characters.")
        name = name[:MAX_ROLE_NAME] or label
        role_raw = entry.get("role_id")
        role_id = _snowflake(role_raw)
        if role_raw not in (None, "") and role_id is None:
            errors.append(f"The role for {label} isn't a valid Discord role.")
        create = _bool(entry.get("create")) and role_id is None
        created = role_id is not None and created_before.get(rank) == role_id
        delete_when_empty = _bool(entry.get("delete_when_empty"))
        if role_id is not None:
            if guild and role_id == guild:
                errors.append(f"{label} can't use @everyone.")
            elif role_id in role_owner:
                errors.append(f"{rank_label(role_owner[role_id])} and {label} use the same role. "
                              "Give each rank its own role.")
            else:
                role_owner[role_id] = rank
            if exempt and role_id == exempt:
                errors.append(f"The exempt role can't also be the role for {label}.")
            if role_by_id:
                role = role_by_id.get(role_id)
                if role is None:
                    errors.append(f"The role for {label} no longer exists in your server, "
                                  "or the bot manages it.")
                else:
                    problem = role_permission_problem(role.get("permissions"))
                    if problem:
                        errors.append(f"The role for {label} can't be synced: {problem}.")
        if create and not create_roles:
            errors.append(f"{label} is set to create a new role. Turn on "
                          "\"Create missing roles\" first.")
        if delete_when_empty and not (create or created):
            # Only roles we made can be deleted; ignore the tick quietly for a
            # role the leader picked rather than refusing the whole save.
            delete_when_empty = False
        ranks.append({"rank": rank, "role_id": role_id, "name": name, "create": create,
                      "created": created, "delete_when_empty": delete_when_empty})

    if errors:
        raise RankRoleConfigError(errors)
    return {"enabled": _bool(data.get("enabled")), "mode": mode,
            "create_roles": create_roles, "exempt_role_id": exempt, "ranks": ranks}


# --------------------------------------------------------------------------- #
# Planning (pure)
# --------------------------------------------------------------------------- #
def _norm_name(name) -> str:
    from utils.format import normalize_player_display_equivalence

    return normalize_player_display_equivalence(name)


def rank_of(player_name, rank_map: Mapping[str, str]) -> str:
    """A roster member's rank from the cache. Absent = on the roster with no
    rank (WOM's ``member``)."""
    rank = rank_map.get(_norm_name(player_name))
    return normalize_rank(rank) if rank else MEMBER_RANK


def _pending_id(rank: str) -> str:
    """Stand-in role id for a rank whose role is still to be created."""
    return f"pending:{rank}"


def effective_roles(config: Mapping, *, include_pending: bool = False) -> List[Optional[str]]:
    """Role id per rank entry, in priority order. With ``include_pending``,
    a rank set to create (and allowed to) holds a placeholder id."""
    out: List[Optional[str]] = []
    for entry in config.get("ranks") or ():
        role_id = entry.get("role_id")
        if role_id is None and include_pending and entry.get("create") and config.get("create_roles"):
            role_id = _pending_id(entry["rank"])
        out.append(role_id)
    return out


def roles_for_rank_index(best: Optional[int], role_ids: Sequence[Optional[str]],
                         mode: str) -> Set[str]:
    """The roles for a member whose highest listed rank is at ``best``."""
    if best is None:
        return set()
    below = [r for r in role_ids[best:] if r]
    if not below:
        return set()
    return set(below) if mode == "cumulative" else {below[0]}


def best_rank_index(ranks: Iterable[str], order: Mapping[str, int]) -> Optional[int]:
    """Highest (lowest index) of a member's ranks that the leader listed."""
    found = [order[r] for r in ranks if r in order]
    return min(found) if found else None


def desired_roles(config: Mapping, roster: Iterable[Tuple[Optional[str], str]],
                  rank_map: Mapping[str, str], *,
                  include_pending: bool = False) -> Dict[str, Set[str]]:
    """``(discord_id, player_name)`` roster rows → ``{discord_id: role ids}``.

    Rows with no Discord link are skipped. Every linked member is in the
    result, with an empty set when no role applies, so a demotion to an
    unmapped rank still removes the old role."""
    order = {e["rank"]: i for i, e in enumerate(config.get("ranks") or ())}
    role_ids = effective_roles(config, include_pending=include_pending)
    mode = config.get("mode") or "single"
    ranks_by_member: Dict[str, Set[str]] = {}
    for discord_id, player_name in roster:
        did = _snowflake(discord_id)
        if did is None:
            continue
        ranks_by_member.setdefault(did, set()).add(rank_of(player_name, rank_map))
    return {did: roles_for_rank_index(best_rank_index(ranks, order), role_ids, mode)
            for did, ranks in ranks_by_member.items()}


def roles_in_use(config: Mapping, roster_names: Iterable[str],
                 rank_map: Mapping[str, str], *, include_pending: bool = False) -> Set[str]:
    """Every role some clanmate's rank resolves to, linked to Discord or not.
    This is what "no members remain" is measured against: the whole clan,
    from WOM's ranks plus roster members WOM reports without one."""
    order = {e["rank"]: i for i, e in enumerate(config.get("ranks") or ())}
    role_ids = effective_roles(config, include_pending=include_pending)
    mode = config.get("mode") or "single"
    ranks: Set[str] = {normalize_rank(r) for r in rank_map.values() if r}
    ranked_names = set(rank_map)
    if any(_norm_name(n) not in ranked_names for n in roster_names):
        ranks.add(MEMBER_RANK)
    used: Set[str] = set()
    for rank in ranks:
        used |= roles_for_rank_index(best_rank_index([rank], order), role_ids, mode)
    return used


def rank_counts(roster_names: Iterable[str], rank_map: Mapping[str, str]) -> Dict[str, int]:
    """Members per rank, for the leader's list. Roster members WOM reports
    without a rank count as ``member``."""
    counts: Dict[str, int] = {}
    for rank in rank_map.values():
        if rank:
            key = normalize_rank(rank)
            counts[key] = counts.get(key, 0) + 1
    ranked = set(rank_map)
    for name in roster_names:
        if _norm_name(name) not in ranked:
            counts[MEMBER_RANK] = counts.get(MEMBER_RANK, 0) + 1
    return counts


def default_rank_order(counts: Mapping[str, int], known: Sequence[str] = ()) -> List[str]:
    """Ranks for the leader's list: ``known`` (their saved order) first, then
    new ranks with Owner and Deputy Owner on top, smaller ranks above larger
    ones (a clan's top ranks are usually its smallest), the no-rank member
    last. Only a starting point; the leader reorders it."""
    seen = list(dict.fromkeys(normalize_rank(r) for r in known if normalize_rank(r)))
    rest = [r for r in counts if r not in seen and r != MEMBER_RANK]
    pinned = {"owner": 0, "deputy_owner": 1}
    rest.sort(key=lambda r: (pinned.get(r, 2), counts.get(r, 0), r))
    out = seen + rest
    if MEMBER_RANK not in out:
        out.append(MEMBER_RANK)
    return out


@dataclass
class MemberChange:
    discord_id: str
    adds: List[str] = field(default_factory=list)
    removes: List[str] = field(default_factory=list)


def plan_member(current: Iterable[str], desired: Set[str], managed: Set[str],
                exempt_role_id: Optional[str] = None) -> Optional[MemberChange]:
    """One member's role changes, or None when they hold the exempt role.
    Only roles in ``managed`` (the map) are ever added or removed."""
    held = {str(r) for r in current}
    if exempt_role_id and exempt_role_id in held:
        return None
    change = MemberChange(discord_id="")
    change.adds = sorted((desired & managed) - held)
    change.removes = sorted((held & managed) - desired)
    return change


def removals_held_back(members_losing: int, members_linked: int, *,
                       config_changed: bool, forced: bool) -> bool:
    """Whether a pass's removals look like bad rank data rather than real
    demotions: lots of members losing roles while the map stayed the same."""
    if forced or config_changed:
        return False
    limit = max(GUARD_MIN_MEMBERS, int(members_linked * GUARD_FRACTION))
    return members_losing > limit


def applied_signature(roles: Iterable[str]) -> str:
    return ",".join(sorted(str(r) for r in roles))


def members_to_visit(desired: Mapping[str, Set[str]], applied: Mapping[str, str], *,
                     full: bool) -> List[str]:
    """Members a pass fetches. A full pass visits every linked member and
    everyone we have given a role before; a routine pass only those whose
    roles should have changed since we last confirmed them. A member known to
    be outside the server counts as confirmed until the next full pass."""
    ids = set(desired) | set(applied)
    if not full:
        ids = {d for d in ids
               if applied.get(d) not in _settled(applied_signature(desired.get(d, ())))}
    return sorted(ids)


#: Prefix on an applied signature for a linked member who is not in the
#: server. A routine pass leaves them be; the daily full pass looks again.
NOT_IN_SERVER = "~"


def _settled(signature: str) -> Tuple[str, str]:
    return (signature, NOT_IN_SERVER + signature)


def bot_capabilities(guild_id, roles: Sequence[Mapping], bot_role_ids: Iterable[str]) -> Tuple[int, int]:
    """``(permission bits, top role position)`` for the bot from the guild's
    roles and the bot member's role ids."""
    by_id = {str(r.get("id")): r for r in roles}
    everyone = by_id.get(str(guild_id)) or {}
    perms = int(everyone.get("permissions") or 0)
    top = 0
    for rid in bot_role_ids:
        role = by_id.get(str(rid))
        if not role:
            continue
        perms |= int(role.get("permissions") or 0)
        top = max(top, int(role.get("position") or 0))
    return perms, top


def role_problems(config: Mapping, guild_id, roles: Sequence[Mapping],
                  bot_top_position: int) -> Dict[str, str]:
    """``{role_id: reason}`` for mapped roles the bot must not or cannot sync."""
    by_id = {str(r.get("id")): r for r in roles}
    out: Dict[str, str] = {}
    mapped = [e.get("role_id") for e in config.get("ranks") or ()]
    for role_id in filter(None, mapped):
        role = by_id.get(str(role_id))
        if role is None:
            out[role_id] = "the role no longer exists"
        elif str(role_id) == str(guild_id):
            out[role_id] = "@everyone can't be synced"
        elif role.get("managed"):
            out[role_id] = "an integration manages this role"
        elif int(role.get("position") or 0) >= bot_top_position:
            out[role_id] = "the role is above the DropTracker bot's highest role"
        else:
            problem = role_permission_problem(role.get("permissions"))
            if problem:
                out[role_id] = problem
    return out


# --------------------------------------------------------------------------- #
# Requests (any process)
# --------------------------------------------------------------------------- #
_MODE_RANK = {"changed": 0, "full": 1, "force": 2}


def request_sync(group_id: int, mode: str = "changed") -> bool:
    """Ask the core bot for a pass on its next tick. ``full`` re-checks every
    member; ``force`` also lets held-back removals through. A pending request
    is only ever upgraded. Best-effort; never raises."""
    if mode not in _MODE_RANK:
        mode = "changed"
    try:
        from utils.redis import redis_client

        client = redis_client.client
        current = client.hget(REQUESTS_KEY, str(int(group_id)))
        if isinstance(current, (bytes, bytearray)):
            current = current.decode()
        if current in _MODE_RANK and _MODE_RANK[current] >= _MODE_RANK[mode]:
            return True
        client.hset(REQUESTS_KEY, str(int(group_id)), mode)
        return True
    except Exception:
        return False


def request_for_player(session, player_id: int) -> int:
    """After a claim: a changed-members pass for every group the account is
    in. Disabled groups are skipped by the bot. Returns groups requested."""
    try:
        from sqlalchemy import text as sql_text

        rows = session.execute(
            sql_text("SELECT group_id FROM user_group_association WHERE player_id = :pid"),
            {"pid": int(player_id)},
        ).fetchall()
    except Exception:
        return 0
    n = 0
    for (gid,) in rows:
        if gid and request_sync(int(gid), "changed"):
            n += 1
    return n


def _pop_requests() -> Dict[int, str]:
    from utils.redis import redis_client

    client = redis_client.client
    pipe = client.pipeline()
    pipe.hgetall(REQUESTS_KEY)
    pipe.delete(REQUESTS_KEY)
    raw, _ = pipe.execute()
    out: Dict[int, str] = {}
    for k, v in (raw or {}).items():
        k = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        v = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        if k.isdigit():
            out[int(k)] = v if v in _MODE_RANK else "changed"
    return out


def read_status(group_id: int) -> Optional[dict]:
    try:
        from utils.redis import redis_client

        raw = redis_client.client.get(status_key(group_id))
        if not raw:
            return None
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


def _write_status(group_id: int, status: dict) -> None:
    try:
        from utils.redis import redis_client

        redis_client.client.setex(status_key(group_id), STATUS_TTL_SECONDS, json.dumps(status))
    except Exception as e:
        print(f"[rank_roles] status write failed for group {group_id}: {e}")


def read_rank_map(wom_group_id) -> Optional[Dict[str, str]]:
    """The cached WOM ranks, or None when there is no cache (never synced,
    WOM down, expired). None means "unknown", never "nobody has a rank"."""
    if not wom_group_id:
        return None
    try:
        from utils.clan_ranks import _key
        from utils.redis import redis_client

        raw = redis_client.client.hgetall(_key(wom_group_id))
    except Exception:
        return None
    if not raw:
        return None
    out = {}
    for k, v in raw.items():
        k = k.decode("utf-8", "replace") if isinstance(k, (bytes, bytearray)) else str(k)
        v = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
        out[k] = v
    return out


# --------------------------------------------------------------------------- #
# DB (sync; the bot calls these through asyncio.to_thread)
# --------------------------------------------------------------------------- #
def load_config_row(session, group_id: int) -> dict:
    from db.models import GroupConfiguration

    row = (session.query(GroupConfiguration)
           .filter(GroupConfiguration.group_id == group_id,
                   GroupConfiguration.config_key == CONFIG_KEY)
           .order_by(GroupConfiguration.id).first())
    if row is None:
        return default_config()
    return parse_config(row.long_value or row.config_value or "")


def save_config(session, group_id: int, config: Mapping) -> None:
    """Upsert the config row (JSON in ``long_value``). Does not commit."""
    from db.models import GroupConfiguration

    row = (session.query(GroupConfiguration)
           .filter(GroupConfiguration.group_id == group_id,
                   GroupConfiguration.config_key == CONFIG_KEY)
           .order_by(GroupConfiguration.id).first())
    payload = dump_config(config)
    if row is None:
        row = GroupConfiguration(group_id=group_id, config_key=CONFIG_KEY, config_value="")
        session.add(row)
    row.config_value = ""
    row.long_value = payload
    try:
        from datetime import datetime

        if hasattr(row, "updated_at"):
            row.updated_at = datetime.now()
    except Exception:
        pass


def load_roster(session, group_id: int) -> List[Tuple[Optional[str], str]]:
    """``(discord_id or None, player_name)`` for every account in the group."""
    from sqlalchemy import text as sql_text

    rows = session.execute(sql_text(
        "SELECT u.discord_id, p.player_name "
        "FROM user_group_association uga "
        "JOIN players p ON p.player_id = uga.player_id "
        "LEFT JOIN users u ON u.user_id = p.user_id "
        "WHERE uga.group_id = :gid AND uga.player_id IS NOT NULL"
    ), {"gid": int(group_id)}).fetchall()
    return [(r[0], r[1]) for r in rows if r[1]]


def sync_owner_group(session, guild_id, group_id: int) -> Optional[int]:
    """The group that runs the sync in this server: the lowest group id with
    it enabled. ``None`` when no group has it on."""
    from db.models import Group, GroupConfiguration

    rows = (session.query(Group.group_id, GroupConfiguration.long_value)
            .join(GroupConfiguration, GroupConfiguration.group_id == Group.group_id)
            .filter(Group.guild_id == str(guild_id),
                    GroupConfiguration.config_key == CONFIG_KEY)
            .order_by(Group.group_id).all())
    for gid, value in rows:
        if parse_config(value or "")["enabled"]:
            return int(gid)
    return None


@dataclass
class GroupContext:
    group_id: int
    guild_id: Optional[str]
    wom_id: Optional[int]
    config: dict
    roster: List[Tuple[Optional[str], str]]
    owner_group: Optional[int]


def load_group_context(group_id: int) -> Optional[GroupContext]:
    from db.models import Group
    from db.models.base import Session

    session = Session()
    try:
        group = session.query(Group.guild_id, Group.wom_id).filter(Group.group_id == group_id).first()
        if group is None:
            return None
        config = load_config_row(session, group_id)
        guild_id = _snowflake(group[0])
        owner = sync_owner_group(session, guild_id, group_id) if guild_id else None
        roster = load_roster(session, group_id) if config["enabled"] else []
        return GroupContext(group_id=group_id, guild_id=guild_id,
                            wom_id=int(group[1]) if group[1] else None,
                            config=config, roster=roster, owner_group=owner)
    finally:
        session.rollback()
        session.close()


def enabled_group_ids() -> List[int]:
    from db.models import GroupConfiguration
    from db.models.base import Session

    session = Session()
    try:
        rows = (session.query(GroupConfiguration.group_id, GroupConfiguration.long_value)
                .filter(GroupConfiguration.config_key == CONFIG_KEY).all())
    finally:
        session.rollback()
        session.close()
    return sorted({int(gid) for gid, value in rows if parse_config(value or "")["enabled"]})


def patch_rank_roles(group_id: int, updates: Mapping[str, Mapping[str, Any]]) -> None:
    """Write bot-side results (a created role's id, a deleted role) back into
    the stored config, entry by entry, re-reading inside the transaction so a
    leader's save in between is not overwritten."""
    if not updates:
        return
    from db.models.base import Session
    from utils import group_config

    session = Session()
    try:
        config = load_config_row(session, group_id)
        changed = False
        for entry in config["ranks"]:
            patch = updates.get(entry["rank"])
            if not patch:
                continue
            expect = patch.get("expect_role_id", entry["role_id"])
            if entry["role_id"] != expect:
                continue  # the leader changed this rank meanwhile; theirs wins
            for key in ("role_id", "created"):
                if key in patch:
                    entry[key] = patch[key]
            changed = True
        if changed:
            save_config(session, group_id, config)
            session.commit()
            group_config.invalidate(group_id, CONFIG_KEY)
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Discord side (duck-typed interactions HTTPClient)
# --------------------------------------------------------------------------- #
def _status_of(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _redis():
    from utils.redis import redis_client

    return redis_client.client


def _hash(raw) -> Dict[str, str]:
    out = {}
    for k, v in (raw or {}).items():
        k = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        v = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        out[k] = v
    return out


async def _ensure_roles(http, ctx: GroupContext, roles: List[dict], in_use: Set[str],
                        status: dict) -> Dict[str, Dict[str, Any]]:
    """Create the roles that should exist, reusing a same-named role, and
    forget created roles someone deleted by hand. Mutates ``ctx.config`` and
    ``roles``; returns the patch to store."""
    config = ctx.config
    by_id = {str(r.get("id")) for r in roles}
    patches: Dict[str, Dict[str, Any]] = {}
    for entry in config["ranks"]:
        if entry["role_id"] and entry["role_id"] not in by_id and entry.get("create") \
                and config.get("create_roles"):
            # Someone deleted a role we made: forget it, and make it again below.
            patches[entry["rank"]] = {"expect_role_id": entry["role_id"], "role_id": None,
                                      "created": False}
            entry["role_id"], entry["created"] = None, False
    if not config.get("create_roles"):
        return patches
    by_name = {}
    for r in roles:
        if not r.get("managed") and str(r.get("id")) != str(ctx.guild_id):
            by_name.setdefault(str(r.get("name") or "").strip().lower(), r)
    # Highest rank first: each new role is created at the bottom of the list,
    # so the last one created sits lowest and the ranks read top to bottom.
    for entry in config["ranks"]:
        if entry["role_id"] or not entry.get("create"):
            continue
        if _pending_id(entry["rank"]) not in in_use:
            continue
        name = entry.get("name") or rank_label(entry["rank"])
        existing = by_name.get(name.strip().lower())
        try:
            if existing is not None:
                role_id, created = str(existing["id"]), False
            else:
                made = await http.create_guild_role(
                    int(ctx.guild_id),
                    {"name": name, "permissions": "0", "mentionable": False, "hoist": False},
                    reason=REASON,
                )
                role_id, created = str(made["id"]), True
                roles.append({"id": role_id, "name": name, "position": made.get("position", 1),
                              "permissions": made.get("permissions", "0"), "managed": False})
                status["created"].append(name)
        except Exception as exc:
            status["problems"].append({"rank": entry["rank"], "role_id": None,
                                       "reason": f"couldn't create the role ({_status_of(exc) or exc})"})
            continue
        # A rank forgotten above still holds the old id in the database.
        expect = patches.get(entry["rank"], {}).get("expect_role_id")
        patches[entry["rank"]] = {"expect_role_id": expect, "role_id": role_id, "created": created}
        entry["role_id"], entry["created"] = role_id, created
        if OP_PAUSE_SECONDS:
            await asyncio.sleep(OP_PAUSE_SECONDS)
    return patches


async def _delete_empty_roles(http, ctx: GroupContext, in_use: Set[str], problems: Dict[str, str],
                              status: dict, now: float) -> Dict[str, Dict[str, Any]]:
    """Delete roles we created that have been empty past the grace period."""
    patches: Dict[str, Dict[str, Any]] = {}
    client = _redis()
    key = empty_key(ctx.group_id)
    since = _hash(client.hgetall(key))
    keep: Dict[str, str] = {}
    for entry in ctx.config["ranks"]:
        role_id = entry.get("role_id")
        if not (role_id and entry.get("created") and entry.get("delete_when_empty")):
            continue
        if role_id in in_use or role_id in problems:
            continue
        first = since.get(entry["rank"])
        try:
            first_ts = float(first.split(":", 1)[1]) if first and first.startswith(role_id + ":") else None
        except ValueError:
            first_ts = None
        if first_ts is None:
            keep[entry["rank"]] = f"{role_id}:{now}"
            continue
        if now - first_ts < DELETE_GRACE_SECONDS:
            keep[entry["rank"]] = f"{role_id}:{first_ts}"
            continue
        try:
            await http.delete_guild_role(int(ctx.guild_id), int(role_id), reason=REASON)
        except Exception as exc:
            if _status_of(exc) != 404:
                status["problems"].append({"rank": entry["rank"], "role_id": role_id,
                                           "reason": f"couldn't delete the empty role ({_status_of(exc) or exc})"})
                keep[entry["rank"]] = f"{role_id}:{first_ts}"
                continue
        status["deleted"].append(entry.get("name") or rank_label(entry["rank"]))
        patches[entry["rank"]] = {"expect_role_id": role_id, "role_id": None, "created": False}
        entry["role_id"], entry["created"] = None, False
    pipe = client.pipeline()
    pipe.delete(key)
    if keep:
        pipe.hset(key, mapping=keep)
    pipe.execute()
    return patches


def _new_status(group_id: int, mode: str, previous: Optional[dict]) -> dict:
    previous = previous or {}
    return {
        "group_id": group_id, "state": "ok", "at": int(time.time()), "pass": mode,
        "last_full_at": previous.get("last_full_at"), "cfg": previous.get("cfg"),
        "linked": 0, "unlinked": 0, "checked": 0, "added": 0, "removed": 0,
        "not_in_server": 0, "exempt": 0, "held_back": 0, "deferred": 0, "errors": 0,
        "created": [], "deleted": [], "problems": [], "message": None,
    }


async def sync_group(bot, group_id: int, mode: str = "changed") -> dict:
    """One pass for one group. Never raises; the result is also stored as the
    group's status for the website."""
    previous = read_status(group_id)
    status = _new_status(group_id, mode, previous)
    try:
        await _sync_group(bot, group_id, mode, status, previous or {})
    except Exception as exc:
        status["state"] = "error"
        status["message"] = str(exc)[:300]
        print(f"[rank_roles] group {group_id} pass failed: {exc}")
    _write_status(group_id, status)
    return status


async def _sync_group(bot, group_id: int, mode: str, status: dict, previous: dict) -> None:
    http = bot.http
    ctx = await asyncio.to_thread(load_group_context, group_id)
    if ctx is None or not ctx.config["enabled"]:
        status["state"] = "disabled"
        return
    if not ctx.guild_id:
        status["state"], status["message"] = "no_guild", "The group has no Discord server linked."
        return
    if ctx.owner_group is not None and ctx.owner_group != group_id:
        status["state"] = "conflict"
        status["message"] = f"Group {ctx.owner_group} already syncs rank roles in this server."
        return
    rank_map = read_rank_map(ctx.wom_id)
    if rank_map is None:
        status["state"] = "waiting_for_ranks"
        status["message"] = ("No clan ranks from WiseOldMan yet. They arrive with the hourly "
                             "member sync once your WiseOldMan group has been synced in game.")
        return

    roles = [dict(r) for r in await http.get_roles(int(ctx.guild_id))]
    bot_member = await http.get_member(int(ctx.guild_id), int(bot.user.id))
    perms, bot_top = bot_capabilities(ctx.guild_id, roles, (bot_member or {}).get("roles") or ())
    if not (perms & ADMINISTRATOR or perms & MANAGE_ROLES):
        status["state"] = "missing_permission"
        status["message"] = "The DropTracker bot needs the Manage Roles permission."
        return

    roster_names = [name for _, name in ctx.roster]
    in_use_pending = roles_in_use(ctx.config, roster_names, rank_map, include_pending=True)
    patches = await _ensure_roles(http, ctx, roles, in_use_pending, status)

    problems = role_problems(ctx.config, ctx.guild_id, roles, bot_top)
    for entry in ctx.config["ranks"]:
        if entry.get("role_id") in problems:
            status["problems"].append({"rank": entry["rank"], "role_id": entry["role_id"],
                                       "reason": problems[entry["role_id"]]})
    managed = {e["role_id"] for e in ctx.config["ranks"] if e.get("role_id")} - set(problems)

    desired = desired_roles(ctx.config, ctx.roster, rank_map)
    desired = {d: roles & managed for d, roles in desired.items()}
    status["linked"] = len(desired)
    status["unlinked"] = sum(1 for did, _ in ctx.roster if _snowflake(did) is None)

    fingerprint = config_fingerprint(ctx.config)
    config_changed = previous.get("cfg") != fingerprint
    last_full = previous.get("last_full_at") or 0
    full = mode in ("full", "force") or config_changed or time.time() - last_full >= FULL_INTERVAL_SECONDS

    client = _redis()
    akey = applied_key(group_id)
    applied = _hash(client.hgetall(akey))
    visit = members_to_visit(desired, applied, full=full)

    # Look first, write second: the removal guard needs the whole pass's count.
    changes: List[MemberChange] = []
    confirmed: Dict[str, str] = {}
    gone: List[str] = []
    for discord_id in visit:
        want = desired.get(discord_id, set())
        try:
            member = await http.get_member(int(ctx.guild_id), int(discord_id))
        except Exception as exc:
            if _status_of(exc) == 404:
                status["not_in_server"] += 1
                gone.append(discord_id)
                if discord_id in desired:
                    confirmed[discord_id] = NOT_IN_SERVER + applied_signature(want)
            else:
                status["errors"] += 1
            continue
        status["checked"] += 1
        change = plan_member((member or {}).get("roles") or (), want, managed,
                             ctx.config.get("exempt_role_id"))
        if change is None:
            status["exempt"] += 1
            confirmed[discord_id] = applied_signature(want)
            continue
        change.discord_id = discord_id
        if change.adds or change.removes:
            changes.append(change)
        else:
            confirmed[discord_id] = applied_signature(want)
        if FETCH_PAUSE_SECONDS:
            await asyncio.sleep(FETCH_PAUSE_SECONDS)

    losing = sum(1 for c in changes if c.removes)
    hold = removals_held_back(losing, max(status["linked"], 1), config_changed=config_changed,
                              forced=mode == "force")
    ops = 0
    for change in changes:
        removes = [] if hold else change.removes
        if hold and change.removes:
            status["held_back"] += 1
        failed = False
        for action, role_id in [("add", r) for r in change.adds] + [("remove", r) for r in removes]:
            if ops >= OP_LIMIT:
                status["deferred"] += 1
                failed = True
                break
            ops += 1
            try:
                if action == "add":
                    await http.add_guild_member_role(int(ctx.guild_id), int(change.discord_id),
                                                     int(role_id), reason=REASON)
                    status["added"] += 1
                else:
                    await http.remove_guild_member_role(int(ctx.guild_id), int(change.discord_id),
                                                        int(role_id), reason=REASON)
                    status["removed"] += 1
            except Exception as exc:
                failed = True
                code = _status_of(exc)
                if code == 404:
                    status["not_in_server"] += 1
                    gone.append(change.discord_id)
                    if change.discord_id in desired:
                        confirmed[change.discord_id] = NOT_IN_SERVER + applied_signature(
                            desired[change.discord_id])
                    break
                status["errors"] += 1
                if code == 403 and not any(p.get("reason", "").startswith("Discord refused")
                                           for p in status["problems"]):
                    status["problems"].append({"rank": None, "role_id": role_id,
                                               "reason": "Discord refused a role change (check the "
                                                         "bot's role is above the rank roles)"})
            if OP_PAUSE_SECONDS:
                await asyncio.sleep(OP_PAUSE_SECONDS)
        if change.discord_id in gone:
            continue  # left the server mid-pass
        if not failed and not (hold and change.removes):
            confirmed[change.discord_id] = applied_signature(desired.get(change.discord_id, ()))

    pipe = client.pipeline()
    if confirmed:
        pipe.hset(akey, mapping=confirmed)
    # Off the roster with nothing left to hold, or gone from the server: no
    # longer ours to track. Linked members outside the server stay, marked.
    stale = [d for d in gone if d not in desired]
    stale += [d for d, sig in confirmed.items() if not sig and d not in desired]
    if stale:
        pipe.hdel(akey, *stale)
    pipe.execute()

    in_use = roles_in_use(ctx.config, roster_names, rank_map)
    patches.update(await _delete_empty_roles(http, ctx, in_use, problems, status, time.time()))
    if patches:
        await asyncio.to_thread(patch_rank_roles, group_id, patches)

    if hold:
        status["state"] = "held_back"
        status["message"] = (f"{losing} members would lose a rank role although your rank map "
                             "didn't change, so removals were held back. If that's right, press "
                             "Sync now.")
    elif status["problems"]:
        status["state"] = "problems"
    # The fingerprint only advances on a pass that let removals through, so a
    # held-back pass is guarded again next time.
    status["cfg"] = fingerprint if not hold else previous.get("cfg")
    if full and not hold:
        status["last_full_at"] = int(time.time())


# --------------------------------------------------------------------------- #
# Scheduler (core bot)
# --------------------------------------------------------------------------- #
_lock = asyncio.Lock()
_pending: set = set()


def _due(group_id: int, now: float) -> Optional[str]:
    status = read_status(group_id) or {}
    at = status.get("at") or 0
    if now - at < ROUTINE_INTERVAL_SECONDS:
        return None
    return "changed"


async def run_tick(bot) -> None:
    async with _lock:
        requests = await asyncio.to_thread(_pop_requests)
        now = time.time()
        enabled = await asyncio.to_thread(enabled_group_ids)
        todo: Dict[int, str] = {}
        for gid in enabled:
            mode = _due(gid, now)
            if mode:
                todo[gid] = mode
        for gid, mode in requests.items():
            if _MODE_RANK.get(mode, 0) >= _MODE_RANK.get(todo.get(gid, "changed"), 0):
                todo[gid] = mode
        for gid, mode in sorted(todo.items()):
            # A request for a group that turned the sync off still writes a
            # "disabled" status, so the website stops showing it as pending.
            result = await sync_group(bot, gid, mode)
            if result.get("added") or result.get("removed") or result.get("created") or result.get("deleted"):
                print(f"[rank_roles] group {gid} ({mode}): +{result['added']} -{result['removed']} "
                      f"created={result['created']} deleted={result['deleted']} state={result['state']}")


def schedule_tick(bot) -> bool:
    """Start a tick in the background unless one is running. The task loop
    calls this every minute; a long pass never stacks up behind itself."""
    if _lock.locked():
        return False
    task = asyncio.get_running_loop().create_task(run_tick(bot))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return True
