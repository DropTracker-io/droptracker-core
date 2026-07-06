"""Achievement diary completion submissions processor."""

import asyncio

from db import DiaryCompletionEntry
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


async def diary_processor(diary_data, external_session=None, world_type="main"):
    """
    Process achievement diary completion submissions.

    Expected diary-specific keys (sent by the client/plugin):
      - diary_name (area, e.g. "Ardougne"; aliases: diary, area)
      - diary_tier (Easy / Medium / Hard / Elite; alias: tier)
      - timestamp (unix seconds)

    Standard submission keys:
      - player_name, acc_hash, auth_key, guid
      - image_url / image_path (optional)
      - video_key / video_url (optional; supported for consistency)
    """
    debug_print(f"=== DIARY PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"[DIARY] Raw diary data: {diary_data}")

    config_prefix = get_config_prefix(world_type)
    is_seasonal = world_type == SEASONAL_WORLD_TYPE
    session, use_external_session = select_session_and_flag(external_session)

    player_name = diary_data.get("player_name") or diary_data.get("player")
    account_hash = diary_data.get("acc_hash") or diary_data.get("account_hash")
    auth_key = diary_data.get("auth_key", "")
    unique_id = diary_data.get("guid")

    image_url = diary_data.get("image_url") or diary_data.get("image_path") or ""
    video_key = diary_data.get("video_key")
    video_url = diary_data.get("video_url")

    diary_name = diary_data.get("diary_name") or diary_data.get("diary") or diary_data.get("area") or ""
    diary_tier = diary_data.get("diary_tier") or diary_data.get("tier") or ""
    timestamp = diary_data.get("timestamp")
    debug_print(f"[DIARY] diary_name={diary_name} diary_tier={diary_tier} unique_id={unique_id} timestamp={timestamp}")

    notice = ""

    if not player_name or not account_hash:
        return SubmissionResponse(success=False, message="Missing player_name or acc_hash.")
    if not diary_name:
        return SubmissionResponse(success=False, message="Missing diary_name.")
    dedup_type = "seasonal_diary" if is_seasonal else "diary"
    if unique_id and not await ensure_can_create(session, unique_id, dedup_type):
        return SubmissionResponse(success=True, message="Diary already processed (duplicate guid).")

    # Authenticate player
    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        return SubmissionResponse(success=False, message="Player not found or could not be created.")
    if not user_exists or not authed:
        return SubmissionResponse(success=False, message="Player authentication failed.")

    player_id = player.player_id
    diary_entry = DiaryCompletionEntry(
        player_id=player_id,
        diary_name=diary_name,
        diary_tier=diary_tier or None,
        world_type=world_type,
        timestamp=_safe_int(timestamp),
        image_url=image_url or None,
        video_url=video_url,
        used_api=bool(diary_data.get("used_api", False)),
        unique_id=unique_id,
    )
    session.add(diary_entry)
    if use_external_session:
        session.flush()
    else:
        session.commit()
        session.refresh(diary_entry)

    # Group notifications
    player_groups = get_player_groups_with_global(session, player)
    if 2 not in [group.group_id for group in player_groups]:
        global_group = session.query(Group).filter(Group.group_id == 2).first()
        if global_group:
            player_groups.append(global_group)
    for group in player_groups:
        print(f"Checking group for diary notifications: {group.group_name}")
        await asyncio.sleep(0)
        group_id = group.group_id

        from utils import group_config as gc
        if not gc.is_truthy(gc.get(session, group_id, f"{config_prefix}notify_diaries")):
            print(f"Diary notifications are disabled for group {group.group_name}")
            continue

        # Screenshot requirement (treat video submissions as satisfying it too)
        if await screenshot_required(session, group_id):
            if not image_url and not video_key and not video_url:
                notice = (
                    f"Your diary submission did not include a screenshot "
                    f"(required for {group.group_name}). Please enable screenshots "
                    f"in the DropTracker plugin configuration."
                )
                print(f"Diary submission did not include a screenshot (required for {group.group_name}).")
                continue

        notification_data = {
            "group_id": group_id,
            "player_name": player_name,
            "player_id": player_id,
            "diary_id": diary_entry.id,
            "guid": unique_id,
            "diary_name": diary_name,
            "diary_tier": diary_tier,
            "timestamp": timestamp,
            "image_url": image_url or "",
            "video_key": video_key,
            "video_url": video_url,
            "world_type": world_type,
        }
        print(f"Notification data: {notification_data}")

        # DM notifications (inferred key: dm_diaries)
        if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_diaries"):
            await create_notification(
                "dm_diary",
                player_id,
                notification_data,
                group_id,
                existing_session=session if use_external_session else None,
            )

        await create_notification(
            "diary",
            player_id,
            notification_data,
            group_id,
            existing_session=session if use_external_session else None,
        )

    debug_print(f"[DIARY] === DIARY PROCESSOR END ===")
    return SubmissionResponse(success=True, message=f"Diary recorded: {diary_name}", notice=notice or None)
