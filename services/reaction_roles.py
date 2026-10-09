"""Roles handed out for reacting to a message on the main server.

Each :class:`ReactionRole` names one message, one emoji and the role a reaction
earns. The webhook bot (``bots/webhook_bot.py``) applies them two ways:

* **Live:** a ``MessageReactionAdd`` on the message with that emoji grants the
  role at once.
* **Backfill:** :func:`reconcile` reads everyone who has reacted, from the
  message itself, and grants the role to each reactor still on the server who
  lacks it. It runs at startup and then on a timer, so reactions made before
  this existed, or while the bot was down, are covered too.

Grants only. Removing the reaction does not take the role away, and a role
someone already holds is left alone. Staff can hand the same role out by hand
without the bot ever undoing it.

The webhook bot runs this because listing guild members needs the privileged
GUILD_MEMBERS intent, which only that bot's application has. The bot needs
Manage Roles, a top role above every role listed here, and read access to each
channel listed here.

Like ``services/discord_roles.py``, this module does not import
``interactions``: the HTTP client is duck-typed, so it is unit-testable alone.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from services.discord_roles import MAIN_GUILD_ID, fetch_guild_members


@dataclass(frozen=True)
class ReactionRole:
    key: str
    guild_id: str
    channel_id: str
    message_id: str
    #: ``name:id`` for a custom emoji, as the reactions endpoint wants it. The
    #: ``a:`` prefix of an animated emoji is left off.
    emoji: str
    role_id: str

    @property
    def emoji_id(self) -> str:
        return self.emoji.rsplit(":", 1)[-1]

    @property
    def reason(self) -> str:
        return f"Reacted to the {self.key} message"


REACTION_ROLES: Sequence[ReactionRole] = (
    # The owner's post in #updates (services/leader_updates.UPDATES_CHANNEL_ID):
    # reacting with <a:droptracker:…> gives the Event Viewer role.
    ReactionRole(
        key="event_viewer",
        guild_id=MAIN_GUILD_ID,
        channel_id="1557440610062045295",
        message_id="1558095248679375013",
        emoji="droptracker:1346787143778963497",
        role_id="1347273738575679600",
    ),
)

#: How often the backfill re-reads the reactions while the bot is up.
RECONCILE_SECONDS = 1800


def match(message_id: Any, emoji_id: Any,
          specs: Iterable[ReactionRole] = REACTION_ROLES) -> Optional[ReactionRole]:
    """The reaction role a reaction earns, if any. Ids may be ints or strings."""
    if message_id is None or emoji_id is None:
        return None
    for spec in specs:
        if str(message_id) == spec.message_id and str(emoji_id) == spec.emoji_id:
            return spec
    return None


async def fetch_reactor_ids(http, spec: ReactionRole, page_pause: float = 0.3) -> List[str]:
    """Every non-bot user who has reacted to the message with the emoji."""
    reactors: List[str] = []
    after: Optional[str] = None
    while True:
        kwargs = {"limit": 100}
        if after:
            kwargs["after"] = after
        page = await http.get_reactions(spec.channel_id, spec.message_id, spec.emoji, **kwargs)
        if not page:
            break
        for user in page:
            if user.get("id") and not user.get("bot"):
                reactors.append(str(user["id"]))
        if len(page) < 100:
            break
        after = str(page[-1]["id"])
        if page_pause:
            await asyncio.sleep(page_pause)
    return reactors


def plan_grants(reactor_ids: Iterable[str], members: Iterable[Mapping[str, Any]],
                role_id: str) -> List[str]:
    """Reactors still on the server who lack the role, in reaction order.

    Someone who reacted and then left is skipped; they get the role from the
    live listener if they rejoin and react again, or from the next pass if
    their reaction is still there when they rejoin.
    """
    holders: Dict[str, bool] = {}
    for member in members:
        user_id = str((member.get("user") or {}).get("id") or "")
        if user_id:
            holders[user_id] = str(role_id) in {str(r) for r in member.get("roles") or ()}
    seen: Set[str] = set()
    grants: List[str] = []
    for user_id in reactor_ids:
        if user_id in seen:
            continue
        seen.add(user_id)
        if holders.get(user_id) is False:
            grants.append(user_id)
    return grants


def _status_of(exc: BaseException) -> Optional[int]:
    try:
        return int(getattr(exc, "status", None))
    except (TypeError, ValueError):
        return None


async def grant(http, spec: ReactionRole, user_id: Any) -> str:
    """Add the role to one member. Returns ``added``, ``not_member``,
    ``forbidden`` or ``error``; never raises."""
    try:
        await http.add_guild_member_role(spec.guild_id, str(user_id), spec.role_id,
                                         reason=spec.reason)
        return "added"
    except Exception as exc:
        status = _status_of(exc)
        if status == 404:
            return "not_member"
        if status == 403:
            return "forbidden"
        print(f"[reaction-roles] {spec.key}: adding the role to {user_id} failed: {exc}")
        return "error"


async def reconcile(http, spec: ReactionRole, dry_run: bool = False,
                    pause: float = 0.2) -> Dict[str, int]:
    """Grant the role to every current reactor who lacks it."""
    reactors = await fetch_reactor_ids(http, spec)
    members = await fetch_guild_members(http, spec.guild_id)
    grants = plan_grants(reactors, members, spec.role_id)
    stats = {"reactors": len(reactors), "to_grant": len(grants),
             "added": 0, "not_member": 0, "forbidden": 0, "error": 0}
    if dry_run:
        return stats
    for user_id in grants:
        stats[await grant(http, spec, user_id)] += 1
        if stats["forbidden"]:
            # Missing Manage Roles or the role sits above the bot's: every
            # other grant would fail the same way.
            break
        if pause:
            await asyncio.sleep(pause)
    return stats


async def reconcile_all(http, specs: Iterable[ReactionRole] = REACTION_ROLES,
                        dry_run: bool = False) -> Dict[str, Dict[str, int]]:
    results: Dict[str, Dict[str, int]] = {}
    for spec in specs:
        try:
            results[spec.key] = await reconcile(http, spec, dry_run=dry_run)
        except Exception as exc:  # one bad message must not stop the others
            print(f"[reaction-roles] {spec.key}: backfill failed: {exc}")
            results[spec.key] = {"failed": 1}
    return results
