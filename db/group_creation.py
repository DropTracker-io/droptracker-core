"""
Shared group-creation service.

This module centralizes the logic required to register a brand new DropTracker
group. It is the SINGLE implementation behind every creation path: the Discord
``/create-group`` slash command (``ClanCommands.create_group_cmd``), the
website wizard (``web_api`` ``POST /groups``), and the legacy XenForo intake
(``api/routes/group_create.py``). Change the creation flow here and every
surface picks it up.

The function is deliberately *context free* - it accepts plain primitives
(Discord IDs as strings/ints) instead of an ``interactions`` ``SlashContext`` so
that it can run inside the Quart API process without importing the Discord bot
stack. Presentation (embeds, HTTP codes) stays with the callers.

Author: DropTracker
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Optional, TypedDict

from sqlalchemy.exc import IntegrityError

from db.clan_sync import insert_xf_group
from db.models import (
    Group,
    GroupAdmin,
    GroupConfiguration,
    Guild,
    User,
    UserConfiguration,
    session,
)

# Hard cap enforced by the ``groups.group_name`` column (String(30)).
MAX_GROUP_NAME_LEN = 30

# Template group whose configuration rows are cloned onto every new group.
# This mirrors the Discord bot, which copies all rows from ``group_id = 1``.
TEMPLATE_GROUP_ID = 1
# Template user whose configuration rows are cloned when we have to create the
# owner's DropTracker user account on the fly (mirrors ``try_create_user``).
TEMPLATE_USER_ID = 1

# Config key holding the per-group API key used for WOM sync / CSV export auth.
EXPORT_API_KEY = "export_api_key"
# Pepper used when minting export API keys. Kept in sync with
# ``scripts/group_export_keygen.py``; override via the EXPORT_API_KEY_PEPPER env.
_EXPORT_API_KEY_PEPPER = os.getenv(
    "EXPORT_API_KEY_PEPPER", "nJ8Rih8TU4MdkMyAMBSDhr2i"
)


def generate_export_api_key(group_id: int) -> str:
    """Mint a unique, unguessable export API key for a group.

    Mirrors the algorithm in ``scripts/group_export_keygen.py`` (a random salt
    plus a timestamp and secret pepper, hashed with SHA-256) so the two code
    paths produce keys of the same shape.
    """
    salt = secrets.token_hex(24)
    timestamp = datetime.now(timezone.utc).isoformat()
    raw = f"{group_id}:{salt}:{timestamp}:{_EXPORT_API_KEY_PEPPER}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GroupCreationResult(TypedDict, total=False):
    """Structured outcome returned by :func:`create_web_group`.

    ``status`` is one of:
        ``created``             - the group was created successfully.
        ``already_registered``  - the guild already owns a group with this WOM id.
        ``guild_conflict``      - the guild already owns a *different* group.
        ``wom_conflict``        - the WOM id is already registered to another group.
        ``invalid_wom``         - the supplied WOM id was not a valid integer.
        ``invalid_name``        - the group name is empty or longer than 30 chars.
        ``db_error``            - an unexpected database error occurred.
    """

    success: bool
    status: str
    message: str
    group_id: Optional[int]
    group_name: Optional[str]
    wom_id: Optional[int]
    guild_id: Optional[str]


def _ensure_user(discord_user_id: str, username: Optional[str]) -> Optional[User]:
    """Return the :class:`User` for ``discord_user_id``, creating it if needed.

    This replicates the database-only portion of ``try_create_user`` (the Discord
    role assignment / DM steps are bot-only and intentionally skipped here).
    """
    discord_user_id = str(discord_user_id)
    user = session.query(User).filter(User.discord_id == discord_user_id).first()
    if user:
        return user

    if username and len(username) > 20:
        # Match the column limit enforced by the bot.
        username = username[:20]

    try:
        new_user = User(
            auth_token="",
            discord_id=discord_user_id,
            username=str(username) if username else None,
        )
        session.add(new_user)
        session.commit()
    except IntegrityError:
        # Lost an insert race — users.discord_id is unique, so another path
        # (bot command, web OAuth login) created the row between our check and
        # commit. Roll back and return the winner.
        session.rollback()
        return session.query(User).filter(User.discord_id == discord_user_id).first()
    except Exception as exc:  # noqa: BLE001 - mirror bot's defensive handling
        session.rollback()
        print(f"[create_web_group] Failed to create user {discord_user_id}: {exc}")
        return None

    # Clone the default user configuration rows from the template user.
    try:
        default_config = (
            session.query(UserConfiguration)
            .filter(UserConfiguration.user_id == TEMPLATE_USER_ID)
            .all()
        )
        new_config = [
            UserConfiguration(
                user_id=new_user.user_id,
                config_key=option.config_key,
                config_value=option.config_value,
                updated_at=datetime.now(),
            )
            for option in default_config
        ]
        if new_config:
            session.add_all(new_config)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(
            f"[create_web_group] Failed to seed user config for {discord_user_id}: {exc}"
        )

    return new_user


def _clone_group_configurations(group: Group, group_name: str, owner_discord_id: str):
    """Clone the template group's configuration onto ``group``.

    Mirrors the Discord bot exactly: every row from ``group_id = 1`` is copied,
    with ``clan_name`` overridden to the new group's name and ``authed_users``
    set to a JSON array containing the owner's Discord id.
    """
    default_config = (
        session.query(GroupConfiguration)
        .filter(GroupConfiguration.group_id == TEMPLATE_GROUP_ID)
        .all()
    )

    new_config = []
    saw_export_key = False
    for option in default_config:
        option_value = option.config_value
        if option.config_key == "clan_name":
            option_value = group_name
        if option.config_key == "authed_users":
            option_value = json.dumps([str(owner_discord_id)])
        if option.config_key == EXPORT_API_KEY:
            # Never reuse the template group's key; mint a unique one so each
            # group's WOM-sync / export API access is isolated.
            saw_export_key = True
            option_value = generate_export_api_key(group.group_id)
        new_config.append(
            GroupConfiguration(
                group_id=group.group_id,
                config_key=option.config_key,
                config_value=option_value,
                updated_at=datetime.now(),
                group=group,
            )
        )

    # Guarantee the group always has an export API key even if the template
    # group is missing the row.
    if not saw_export_key:
        new_config.append(
            GroupConfiguration(
                group_id=group.group_id,
                config_key=EXPORT_API_KEY,
                config_value=generate_export_api_key(group.group_id),
                updated_at=datetime.now(),
                group=group,
            )
        )

    session.add_all(new_config)
    session.commit()
    return len(new_config)


def _seed_group_admin_owner(group: Group, user: User) -> None:
    """Seed the creator as the group's ``group_admins`` owner (idempotent).

    Runs on every creation path (bot command, web wizard, legacy XF intake) so
    the creator can always administer their group on the website. Best-effort:
    the ``authed_users`` config remains the bot-side auth source regardless.
    """
    try:
        existing = (
            session.query(GroupAdmin)
            .filter(
                GroupAdmin.group_id == group.group_id,
                GroupAdmin.user_id == user.user_id,
            )
            .first()
        )
        if not existing:
            session.add(
                GroupAdmin(group_id=group.group_id, user_id=user.user_id, role="owner")
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[create_web_group] Failed to seed group_admins owner: {exc}")


async def _run_initial_wom_sync(wom_id: int) -> None:
    """Background task: perform the first WOM membership sync for a new group.

    Runs after the create result has been returned so callers stay snappy; by
    the time the owner views their lootboard it will be populated. Best-effort
    only — failures are logged and swallowed, and the sync helper's own
    cooldown guard makes double-fires safe.
    """
    try:
        from db.ops import sync_group_from_wom_with_stats

        result = await sync_group_from_wom_with_stats(wom_id=int(wom_id))
        if result.get("on_cooldown"):
            print(f"[create_web_group] Initial WOM sync skipped (cooldown) for wom_id={wom_id}")
        else:
            print(
                f"[create_web_group] Initial WOM sync done for wom_id={wom_id}: "
                f"+{len(result.get('added', []))} members"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[create_web_group] Initial WOM sync failed for wom_id={wom_id}: {exc}")


async def create_web_group(
    *,
    group_name: str,
    wom_id,
    guild_id,
    owner_discord_id,
    owner_username: Optional[str] = None,
    initial_sync: bool = True,
) -> GroupCreationResult:
    """Create a new DropTracker group on behalf of a website user.

    This is a 1:1 functional replica of the Discord ``/create-group`` command,
    minus the Discord-only presentation (embeds, ephemeral messages, sleeps).

    Args:
        group_name: Desired display name for the group.
        wom_id: WiseOldMan group id (accepts ``int`` or numeric ``str``).
        guild_id: Discord server (guild) id the group is being created for.
        owner_discord_id: Discord user id of the person creating the group; this
            becomes the sole entry in the group's ``authed_users``.
        owner_username: Optional Discord username, used only if we have to create
            the owner's DropTracker user account on the fly.
        initial_sync: Schedule a background WOM membership sync after creation
            (default True) so the group's lootboard populates without waiting
            for the scheduled sync.

    Returns:
        A :class:`GroupCreationResult` describing the outcome.
    """
    # --- Validate the group name against the column limit ---
    group_name = (group_name or "").strip()
    if not (1 <= len(group_name) <= MAX_GROUP_NAME_LEN):
        return GroupCreationResult(
            success=False,
            status="invalid_name",
            message=f"Group name must be 1–{MAX_GROUP_NAME_LEN} characters.",
            group_id=None,
            group_name=None,
            wom_id=None,
            guild_id=str(guild_id) if guild_id is not None else None,
        )

    # --- Validate WOM id (the bot coerces to int) ---
    try:
        wom_id = int(wom_id)
    except (TypeError, ValueError):
        return GroupCreationResult(
            success=False,
            status="invalid_wom",
            message="The WiseOldMan group ID must be a number.",
            group_id=None,
            wom_id=None,
            guild_id=str(guild_id) if guild_id is not None else None,
        )

    if not guild_id:
        return GroupCreationResult(
            success=False,
            status="guild_conflict",
            message="A Discord server must be selected to create a group.",
            group_id=None,
            wom_id=wom_id,
            guild_id=None,
        )

    guild_id = str(guild_id)
    owner_discord_id = str(owner_discord_id)

    # --- Ensure the owner has a DropTracker user account ---
    user = _ensure_user(owner_discord_id, owner_username)
    if not user:
        return GroupCreationResult(
            success=False,
            status="db_error",
            message="Unable to resolve your DropTracker account. Please try again later.",
            group_id=None,
            wom_id=wom_id,
            guild_id=guild_id,
        )

    # --- Ensure the guild row exists ---
    guild = session.query(Guild).filter(Guild.guild_id == guild_id).first()
    if not guild:
        guild = Guild(guild_id=guild_id, date_added=datetime.now())
        session.add(guild)
        session.commit()
    else:
        if guild.group_id is not None:
            existing = (
                session.query(Group).filter(Group.group_id == guild.group_id).first()
            )
            if existing and existing.wom_id == wom_id:
                return GroupCreationResult(
                    success=False,
                    status="already_registered",
                    message="This Discord server already has a group registered with the DropTracker.",
                    group_id=existing.group_id,
                    wom_id=existing.wom_id,
                    guild_id=guild_id,
                )
            return GroupCreationResult(
                success=False,
                status="guild_conflict",
                message=(
                    "This Discord server is already associated with a different "
                    f"DropTracker group (WOM id {existing.wom_id if existing else 'unknown'})."
                ),
                group_id=existing.group_id if existing else None,
                wom_id=existing.wom_id if existing else None,
                guild_id=guild_id,
            )

    # --- Enforce global WOM id uniqueness ---
    existing_wom_group = session.query(Group).filter(Group.wom_id == wom_id).first()
    if existing_wom_group:
        return GroupCreationResult(
            success=False,
            status="wom_conflict",
            message=f"This WiseOldMan group ({wom_id}) is already registered with the DropTracker.",
            group_id=existing_wom_group.group_id,
            wom_id=wom_id,
            guild_id=guild_id,
        )

    # --- Create the group ---
    group = Group(group_name=group_name, wom_id=wom_id, guild_id=guild.guild_id)
    session.add(group)
    user.add_group(group)
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[create_web_group] Failed to commit group: {exc}")
        return GroupCreationResult(
            success=False,
            status="db_error",
            message="Unable to create your group due to a database error. Please try again later.",
            group_id=None,
            wom_id=wom_id,
            guild_id=guild_id,
        )

    # --- Link the guild to the new group ---
    guild.group_id = group.group_id
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[create_web_group] Failed to link guild to group: {exc}")

    # --- Seed the creator as the web-side owner (all creation paths) ---
    _seed_group_admin_owner(group, user)

    # Site-wide ticker (rt:feed): announce the new group. Best-effort.
    try:
        from services.realtime import publish_feed_group_created

        publish_feed_group_created(group.group_id, group_name)
    except Exception as exc:  # noqa: BLE001
        print(f"[create_web_group] Ticker publish failed: {exc}")

    # --- Mirror into the XenForo database (best effort, matches bot) ---
    try:
        await insert_xf_group(group)
    except Exception as exc:  # noqa: BLE001
        print(f"[create_web_group] Error inserting group into XenForo: {exc}")

    # --- Clone the default configuration from the template group ---
    config_warning = None
    try:
        _clone_group_configurations(group, group_name, owner_discord_id)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"[create_web_group] Error creating default configs: {exc}")
        # The group itself exists; surface a soft warning rather than failing.
        config_warning = (
            "Your group was created, but default settings could not be applied "
            "automatically. You can configure them on the website."
        )

    # --- Kick off a non-blocking initial WOM membership sync ---
    if initial_sync:
        try:
            import asyncio

            asyncio.get_running_loop().create_task(_run_initial_wom_sync(wom_id))
        except Exception as exc:  # noqa: BLE001
            print(f"[create_web_group] Could not schedule initial WOM sync: {exc}")

    return GroupCreationResult(
        success=True,
        status="created",
        message=config_warning or "Your group has been created successfully.",
        group_id=group.group_id,
        group_name=group_name,
        wom_id=wom_id,
        guild_id=guild_id,
    )
