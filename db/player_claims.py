"""Shared RSN claim/unclaim service.

Centralizes the logic behind the Discord ``/claim-rsn`` and ``/unclaim-rsn``
commands so the same behavior can be invoked from the website / Discord
Activity (``web_api``) and the bot itself, without either side hand-copying
the rules.

Behavioral contract (mirrors ``UserCommands.claim_rsn_command``):
    * A ``Player`` row must already exist (created by plugin submissions,
      WOM-authoritative) — claiming NEVER creates players.
    * Lookup is case/trim-insensitive with OSRS space<->underscore
      equivalence (``utils.format.get_player_by_claim_rsn``).
    * A player claimed by another Discord account is refused; disputes go
      through support tickets.
    * A successful claim links ``players.user_id`` and attaches the player to
      the guild's group when a guild context is provided, falling back to the
      global group (id 2) — identical to running the command in DMs.
    * Re-claiming an account you already own re-runs that guild-group attach,
      but only where the hourly WOM sync would not undo it — see
      ``_reclaim_attach_group``. Membership of a WOM-backed group belongs to
      its roster, so the result reports ``wom_managed`` instead of writing a
      row that would be evicted (and publicly announced) within the hour.

The functions are context free: they accept plain primitives (Discord ids as
strings) so they can run inside the Quart web_api process without importing
the Discord bot stack. All mutations run on the global scoped ``session`` and
end in commit-or-rollback.

The web_api's per-request teardown is NOT the safety net it looks like: these
run inside ``asyncio.to_thread``, so the scoped session they touch belongs to
the *worker thread*, while ``teardown_appcontext`` calls ``remove()`` on the
event-loop thread — a different thread-local slot entirely. Every early return
that only read (``not_found``, ``already_yours``, ``claimed_by_other``) would
therefore strand an autobegun read transaction, holding its connection open
for the life of the process. The public entry points are wrapped in
``_release_scoped_session`` so cleanup happens on the thread that did the work,
the same shape as ``services.points._autoclean_scoped_session``.
"""
from __future__ import annotations

import functools
from datetime import datetime
from typing import Optional, TypedDict

from sqlalchemy.exc import IntegrityError

from db.models import (
    Group,
    Player,
    User,
    UserConfiguration,
    session,
)
from utils.format import get_player_by_claim_rsn, normalize_claim_rsn_input

def _release_scoped_session(fn):
    """``session.remove()`` in a finally, on the calling thread.

    See the module docstring: these entry points run under
    ``asyncio.to_thread``, so nothing else will ever clean up the thread-local
    scoped session they used.

    ONLY for outermost entry points. ``remove()`` discards the whole session,
    so decorating a helper that runs inside another one's transaction (e.g.
    ``ensure_user_provisioned``, called by ``claim_player``) would drop the
    caller's work halfway through.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            try:
                session.remove()
            except Exception:
                pass
    return wrapper


# Template user whose configuration rows are cloned when a user account has no
# config rows yet (mirrors ``try_create_user`` / ``group_creation``).
TEMPLATE_USER_ID = 1

# The global/default group every claim falls back to when no guild context (or
# an unregistered guild) is supplied — same as the Discord command.
FALLBACK_GROUP_ID = 2


class ClaimResult(TypedDict, total=False):
    """Outcome of :func:`preview_claim` / :func:`claim_player`.

    ``status`` is one of:
        ``not_found``        - no Player row matches the RSN (install plugin first).
        ``claimable``        - preview only: the player exists and is unclaimed.
        ``claimed``          - claim only: the player is now linked to the caller.
        ``already_yours``    - the caller already owns this player.
        ``claimed_by_other`` - another Discord account owns this player.
        ``error``            - unexpected database error.
    """

    status: str
    message: str
    player_id: Optional[int]
    player_name: Optional[str]
    claimed_at: Optional[str]
    # Only populated on claimed_by_other, for the Discord command's embed.
    # Web surfaces must NOT expose this.
    owner_discord_id: Optional[str]
    # Group that was (claim) or would be (preview) attached, including the
    # fallback group 2 — callers decide whether group 2 is worth surfacing.
    group_id: Optional[int]
    group_name: Optional[str]
    # ``already_yours`` only: the guild group's Wise Old Man id, so callers can
    # link straight to the roster a member has to be added to.
    group_wom_id: Optional[int]
    # ``already_yours`` only: what happened to the guild-group attach.
    #   ``in_group``    - the player is already in the guild's group.
    #   ``attached``    - this re-claim attached it, durably.
    #   ``wom_managed`` - deliberately NOT attached; the group's membership is
    #                     owned by its WOM roster (see _reclaim_attach_group).
    #   ``error``       - the attach failed; treat it as a no-op.
    # Absent when no guild group resolved at all.
    group_status: Optional[str]


class UnclaimResult(TypedDict, total=False):
    """Outcome of :func:`unclaim_player`.

    ``status`` is one of ``unclaimed`` | ``not_found`` | ``not_yours`` | ``error``.
    """

    status: str
    message: str
    player_id: Optional[int]
    player_name: Optional[str]


def _resolve_guild_group(guild_id) -> Optional[Group]:
    """Resolve the group a claim should attach to (mirrors the command).

    Guild context first (``Group.guild_id`` match, case-insensitive), then the
    global fallback group 2. Returns ``None`` only if neither exists.
    """
    group = None
    if guild_id:
        group = (
            session.query(Group).filter(Group.guild_id.ilike(str(guild_id))).first()
        )
    if not group:
        group = session.query(Group).filter_by(group_id=FALLBACK_GROUP_ID).first()
    return group


def _reclaim_attach_group(
    result: ClaimResult, player: Player, group: Optional[Group]
) -> None:
    """Re-run the guild-group attach for an ``already_yours`` re-claim.

    Only the *first* claim ever attached the guild's group, so a player who was
    linked before they joined the clan's Discord (or before the group existed)
    stayed out of it no matter how often they re-ran ``/claim-rsn`` — the
    support-ticket-413 shape: tracked on the site, never posted in the clan's
    channel, and the command reporting nothing either way.

    Attaching unconditionally would be worse than that no-op. For a WOM-backed
    group ``_sync_group_from_wom`` (``db/ops.py``) evicts any member whose
    ``wom_id`` is not on the roster, and that eviction calls
    ``notify_group(bot, "player_removed", ...)`` — so the clan's log channel
    would announce a departure, within the hour, for a member who never left.

    So attach only where the hourly sync cannot undo it:

    * **the group has no usable ``wom_id``** — ``update_group_members`` iterates
      ``Group.wom_id`` and does ``int(wom_id)``, skipping the group when that
      raises, so nothing ever syncs it (and a 0 never resolves a roster); or
    * **the player has no ``wom_id``** — the removal pass is guarded by
      ``if member.wom_id and member.wom_id not in group_wom_id_set``.

    Otherwise leave the membership alone and report ``wom_managed``, which is
    the honest answer: joining the clan's WOM group is what starts the posting.
    """
    if not group:
        return

    result["group_id"] = group.group_id
    result["group_name"] = group.group_name
    result["group_wom_id"] = group.wom_id

    if group in player.groups:
        result["group_status"] = "in_group"
        return

    if group.wom_id and player.wom_id:
        result["group_status"] = "wom_managed"
        return

    try:
        # add_group is idempotent, links the owning User too, and commits.
        player.add_group(group)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(
            f"[player_claims] Failed to attach group {group.group_id} to "
            f"player {player.player_id} on re-claim: {exc}"
        )
        result["group_status"] = "error"
        return

    result["group_status"] = "attached"


def _seed_user_config_if_missing(user: User) -> None:
    """Clone template config rows for a user that has none.

    Users minted by the web OAuth login (``auth._find_or_create_user``) skip
    the template clone that ``try_create_user`` performs — backfill here so a
    web-first user ends up provisioned identically to a bot-first user.
    """
    try:
        has_config = (
            session.query(UserConfiguration.id)
            .filter(UserConfiguration.user_id == user.user_id)
            .first()
        )
        if has_config:
            return
        template_rows = (
            session.query(UserConfiguration)
            .filter(UserConfiguration.user_id == TEMPLATE_USER_ID)
            .all()
        )
        new_rows = [
            UserConfiguration(
                user_id=user.user_id,
                config_key=row.config_key,
                config_value=row.config_value,
                updated_at=datetime.now(),
            )
            for row in template_rows
        ]
        if new_rows:
            session.add_all(new_rows)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - config seeding is best-effort
        session.rollback()
        print(f"[player_claims] Failed to seed user config for {user.user_id}: {exc}")


def ensure_user_provisioned(
    discord_id: str, username: Optional[str] = None
) -> Optional[User]:
    """Return the :class:`User` for ``discord_id``, creating it if needed.

    DB-only mirror of ``try_create_user`` (Discord role/DM side effects are
    bot-only and stay in the command). Also backfills template config rows for
    pre-existing users that have none.
    """
    discord_id = str(discord_id)
    user = session.query(User).filter(User.discord_id == discord_id).first()
    if user:
        _seed_user_config_if_missing(user)
        return user

    if username and len(username) > 20:
        username = username[:20]

    try:
        user = User(
            auth_token="",
            discord_id=discord_id,
            username=str(username) if username else None,
        )
        session.add(user)
        session.commit()
    except IntegrityError:
        # Lost an insert race — users.discord_id is unique, so another path
        # (bot command, web OAuth login) created the row between our check and
        # commit. Roll back and return the winner.
        session.rollback()
        user = session.query(User).filter(User.discord_id == discord_id).first()
        if user:
            _seed_user_config_if_missing(user)
        return user
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[player_claims] Failed to create user {discord_id}: {exc}")
        return None

    _seed_user_config_if_missing(user)
    return user


def _player_claim_facts(player: Player) -> dict:
    claimed_at = None
    if player.date_added is not None:
        try:
            claimed_at = player.date_added.isoformat()
        except Exception:  # noqa: BLE001
            claimed_at = None
    return {
        "player_id": player.player_id,
        "player_name": player.player_name,
        "claimed_at": claimed_at,
    }


@_release_scoped_session
def preview_claim(
    rsn: str, *, discord_id: Optional[str] = None, guild_id=None
) -> ClaimResult:
    """Read-only claim check for as-you-type feedback. Writes nothing."""
    norm = normalize_claim_rsn_input(rsn)
    if not norm:
        return ClaimResult(status="not_found", message="No RSN provided.")

    player = get_player_by_claim_rsn(session, Player, norm)
    if not player:
        return ClaimResult(
            status="not_found",
            message="This player has not been tracked yet.",
        )

    facts = _player_claim_facts(player)
    if player.user:
        if discord_id is not None and str(player.user.discord_id) == str(discord_id):
            return ClaimResult(status="already_yours", **facts)
        return ClaimResult(
            status="claimed_by_other",
            owner_discord_id=str(player.user.discord_id),
            **facts,
        )

    result = ClaimResult(status="claimable", **facts)
    group = _resolve_guild_group(guild_id)
    if group:
        result["group_id"] = group.group_id
        result["group_name"] = group.group_name
    return result


@_release_scoped_session
def claim_player(
    rsn: str,
    *,
    discord_id: str,
    username: Optional[str] = None,
    guild_id=None,
) -> ClaimResult:
    """Claim ``rsn`` for ``discord_id`` (mirrors the /claim-rsn mutation)."""
    user = ensure_user_provisioned(discord_id, username)
    if not user:
        return ClaimResult(
            status="error",
            message="Unable to resolve your DropTracker account. Please try again later.",
        )

    player = get_player_by_claim_rsn(session, Player, rsn)
    if not player:
        return ClaimResult(
            status="not_found",
            message="This player has not been tracked yet.",
        )

    facts = _player_claim_facts(player)
    if player.user:
        if str(player.user.discord_id) == str(discord_id):
            result = ClaimResult(status="already_yours", **facts)
            # Re-claiming is how users try to fix "my drops aren't posting in
            # our clan channel" — make it self-healing where that is safe.
            _reclaim_attach_group(result, player, _resolve_guild_group(guild_id))
            return result
        return ClaimResult(
            status="claimed_by_other",
            owner_discord_id=str(player.user.discord_id),
            **facts,
        )

    group = _resolve_guild_group(guild_id)
    try:
        player.user = user
        if group and group not in player.groups:
            # add_group also links the owning User to the group and commits.
            player.add_group(group)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[player_claims] Failed to claim {rsn!r} for {discord_id}: {exc}")
        return ClaimResult(
            status="error",
            message="A database error occurred while claiming this account.",
        )

    result = ClaimResult(status="claimed", **facts)
    if group:
        result["group_id"] = group.group_id
        result["group_name"] = group.group_name
    return result


@_release_scoped_session
def unclaim_player(
    *,
    discord_id: str,
    player_id: Optional[int] = None,
    rsn: Optional[str] = None,
) -> UnclaimResult:
    """Unclaim a player owned by ``discord_id`` (mirrors /unclaim-rsn).

    Accepts either a ``player_id`` (web: the UI lists owned accounts by id) or
    an ``rsn`` (bot: the command takes a name).
    """
    player = None
    if player_id is not None:
        player = session.query(Player).filter(Player.player_id == player_id).first()
    elif rsn:
        player = get_player_by_claim_rsn(session, Player, rsn)

    if not player:
        return UnclaimResult(status="not_found", message="Player not found.")

    if not player.user or str(player.user.discord_id) != str(discord_id):
        return UnclaimResult(
            status="not_yours",
            message="This account is not claimed by you.",
            player_id=player.player_id,
            player_name=player.player_name,
        )

    try:
        for group in list(player.groups):
            player.remove_group(group)
        player.user = None
        player.user_id = None
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[player_claims] Failed to unclaim player {player.player_id}: {exc}")
        return UnclaimResult(
            status="error",
            message="A database error occurred while unclaiming this account.",
        )

    return UnclaimResult(
        status="unclaimed",
        player_id=player.player_id,
        player_name=player.player_name,
    )
