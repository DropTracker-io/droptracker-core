"""Player death submissions processor."""

import asyncio

from db import PlayerDeath
from db.models import Group

from .common import (
    SubmissionResponse,
    ensure_player_by_name_then_auth,
    get_player_groups_with_global,
    is_user_dm_enabled,
    create_notification,
    screenshot_required,
    select_session_and_flag,
    ensure_can_create,
    debug_print,
    get_config_prefix,
    SEASONAL_WORLD_TYPE,
)


def _safe_int(value):
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


async def death_processor(death_data, external_session=None, world_type="main"):
    """
    Process player death submissions.

    Expected death-specific keys (sent by the client/plugin):
      - source (killer NPC/player name; aliases: killer, npc_name)
      - region_id
      - location
      - timestamp (unix seconds)

    Standard submission keys:
      - player_name, acc_hash, auth_key, guid
      - image_url / image_path (optional)
      - video_key / video_url (optional; supported for consistency)
    """
    debug_print(f"=== DEATH PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"[DEATH] Raw death data: {death_data}")

    config_prefix = get_config_prefix(world_type)
    is_seasonal = world_type == SEASONAL_WORLD_TYPE
    session, use_external_session = select_session_and_flag(external_session)

    player_name = death_data.get("player_name") or death_data.get("player")
    account_hash = death_data.get("acc_hash") or death_data.get("account_hash")
    auth_key = death_data.get("auth_key", "")
    unique_id = death_data.get("guid")

    image_url = death_data.get("image_url") or death_data.get("image_path") or ""
    video_key = death_data.get("video_key")
    video_url = death_data.get("video_url")

    source = death_data.get("source") or death_data.get("killer") or death_data.get("npc_name") or ""
    region_id = _safe_int(death_data.get("region_id"))
    location = death_data.get("location") or ""
    timestamp = death_data.get("timestamp")
    debug_print(f"[DEATH] source={source} unique_id={unique_id} timestamp={timestamp}")

    notice = ""

    if not player_name or not account_hash:
        return SubmissionResponse(success=False, message="Missing player_name or acc_hash.")
    dedup_type = "seasonal_death" if is_seasonal else "death"
    if unique_id and not await ensure_can_create(session, unique_id, dedup_type):
        return SubmissionResponse(success=True, message="Death already processed (duplicate guid).")

    # Authenticate player
    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        return SubmissionResponse(success=False, message="Player not found or could not be created.")
    if not user_exists or not authed:
        return SubmissionResponse(success=False, message="Player authentication failed.")

    player_id = player.player_id
    death_entry = PlayerDeath(
        player_id=player_id,
        source=source or None,
        region_id=region_id,
        location=location or None,
        world_type=world_type,
        image_url=image_url or None,
        video_url=video_url,
        used_api=bool(death_data.get("used_api", False)),
        unique_id=unique_id,
    )
    session.add(death_entry)
    if use_external_session:
        session.flush()
    else:
        session.commit()
        session.refresh(death_entry)

    # Group notifications
    player_groups = get_player_groups_with_global(session, player)
    if 2 not in [group.group_id for group in player_groups]:
        global_group = session.query(Group).filter(Group.group_id == 2).first()
        if global_group:
            player_groups.append(global_group)
    for group in player_groups:
        print(f"Checking group for death notifications: {group.group_name}")
        await asyncio.sleep(0)
        group_id = group.group_id

        from utils import group_config as gc
        if not gc.is_truthy(gc.get(session, group_id, f"{config_prefix}notify_deaths")):
            print(f"Death notifications are disabled for group {group.group_name}")
            continue

        # Screenshot requirement (treat video submissions as satisfying it too)
        if await screenshot_required(session, group_id):
            if not image_url and not video_key and not video_url:
                notice = (
                    f"Your death submission did not include a screenshot "
                    f"(required for {group.group_name}). Please enable screenshots "
                    f"in the DropTracker plugin configuration."
                )
                print(f"Death submission did not include a screenshot (required for {group.group_name}).")
                continue

        notification_data = {
            "group_id": group_id,
            "player_name": player_name,
            "player_id": player_id,
            "death_id": death_entry.id,
            "guid": unique_id,
            "source": source,
            "region_id": region_id,
            "location": location,
            "timestamp": timestamp,
            "image_url": image_url or "",
            "video_key": video_key,
            "video_url": video_url,
            "world_type": world_type,
        }
        print(f"Notification data: {notification_data}")

        await create_notification(
            "death",
            player_id,
            notification_data,
            group_id,
            existing_session=session if use_external_session else None,
        )

    # Personal submission DM (supporter perk): queued once per death,
    # OUTSIDE the group loop — group notify/screenshot criteria must not
    # gate or duplicate a personal DM (same fix as drops, c258115).
    # Entitlement + opt-in are re-checked at send time.
    try:
        if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_deaths"):
            await create_notification(
                "dm_death",
                player_id,
                {
                    "player_name": player_name,
                    "player_id": player_id,
                    "death_id": death_entry.id,
                    "guid": unique_id,
                    "source": source,
                    "location": location,
                    "image_url": image_url or "",
                    "video_key": video_key,
                    "world_type": world_type,
                },
                existing_session=session if use_external_session else None,
            )
    except Exception as e:
        print(f"Couldn't queue personal death DM notification: {e}")

    debug_print(f"[DEATH] === DEATH PROCESSOR END ===")
    return SubmissionResponse(success=True, message="Death recorded.", notice=notice or None)
