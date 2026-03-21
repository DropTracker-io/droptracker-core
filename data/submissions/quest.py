"""Quest completion submissions processor."""

import asyncio

from db import QuestCompletionEntry
from db.models import Group

from .common import (
    SubmissionResponse,
    ensure_player_by_name_then_auth,
    get_player_groups_with_global,
    is_user_dm_enabled,
    create_notification,
    is_truthy_config,
    screenshot_required,
    select_session_and_flag,
    ensure_can_create,
    debug_print,
    GroupConfiguration,
)


def _safe_int(value):
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


async def quest_processor(quest_data, external_session=None):
    """
    Process quest completion submissions.

    Expected quest-specific keys (sent by the client/plugin):
      - quest_name
      - quests_completed, total_quests, completion_percentage
      - quest_points, total_quest_points, qp_percentage
      - timestamp (unix seconds)

    Standard submission keys:
      - player_name, acc_hash, auth_key, guid
      - image_url / image_path (optional)
      - video_key / video_url (optional; supported for consistency)
    """
    print("=== QUEST PROCESSOR START ===")
    print(f"[QUEST] Raw quest data: {quest_data}")

    session, use_external_session = select_session_and_flag(external_session)

    player_name = quest_data.get("player_name") or quest_data.get("player")
    account_hash = quest_data.get("acc_hash") or quest_data.get("account_hash")
    auth_key = quest_data.get("auth_key", "")
    unique_id = quest_data.get("guid")

    image_url = quest_data.get("image_url") or quest_data.get("image_path") or ""
    video_key = quest_data.get("video_key")
    video_url = quest_data.get("video_url")

    quest_name = quest_data.get("quest_name") or quest_data.get("quest") or ""
    quests_completed = quest_data.get("quests_completed")
    total_quests = quest_data.get("total_quests")
    completion_percentage = quest_data.get("completion_percentage")
    quest_points = quest_data.get("quest_points")
    total_quest_points = quest_data.get("total_quest_points")
    qp_percentage = quest_data.get("qp_percentage")
    timestamp = quest_data.get("timestamp")
    print(f"[QUEST] timestamp: {timestamp}")
    print(f"[QUEST] completion_percentage: {completion_percentage}")
    print(f"[QUEST] quest_points: {quest_points}")
    print(f"[QUEST] total_quest_points: {total_quest_points}")
    print(f"[QUEST] qp_percentage: {qp_percentage}")
    print(f"[QUEST] quests_completed: {quests_completed}")
    print(f"[QUEST] total_quests: {total_quests}")
    print(f"[QUEST] quest_name: {quest_name}")
    print(f"[QUEST] unique_id: {unique_id}")

    notice = ""

    if not player_name or not account_hash:
        return SubmissionResponse(success=False, message="Missing player_name or acc_hash.")
    if not quest_name:
        return SubmissionResponse(success=False, message="Missing quest_name.")
    if unique_id and not await ensure_can_create(session, unique_id, "quest"):
        return SubmissionResponse(success=True, message="Quest already processed (duplicate guid).")

    # Authenticate player
    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        return SubmissionResponse(success=False, message="Player not found or could not be created.")
    if not user_exists or not authed:
        return SubmissionResponse(success=False, message="Player authentication failed.")

    player_id = player.player_id
    quest_entry = QuestCompletionEntry(
        player_id=player_id,
        quest_name=quest_name,
        quests_completed=_safe_int(quests_completed),
        total_quests=_safe_int(total_quests),
        completion_percentage=str(completion_percentage) if completion_percentage is not None else None,
        quest_points=_safe_int(quest_points),
        total_quest_points=_safe_int(total_quest_points),
        qp_percentage=str(qp_percentage) if qp_percentage is not None else None,
        timestamp=_safe_int(timestamp),
        image_url=image_url or None,
        video_url=video_url,
        used_api=bool(quest_data.get("used_api", False)),
        unique_id=unique_id,
    )
    session.add(quest_entry)
    if use_external_session:
        session.flush()
    else:
        session.commit()
        session.refresh(quest_entry)

    # Group notifications
    player_groups = get_player_groups_with_global(session, player)
    if 2 not in [group.group_id for group in player_groups]:
        global_group = session.query(Group).filter(Group.group_id == 2).first()
        if global_group:
            player_groups.append(global_group)
    for group in player_groups:
        print(f"Checking group for quest notifications: {group.group_name}")
        await asyncio.sleep(0)
        group_id = group.group_id

        quest_notify_config = (
            session.query(GroupConfiguration)
            .filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == "notify_quests",
            )
            .first()
        )

        if not quest_notify_config or not is_truthy_config(getattr(quest_notify_config, "config_value", None)):
            print(f"Quest notifications are disabled for group {group.group_name}")
            continue

        # Screenshot requirement (treat video submissions as satisfying it too))
        if await screenshot_required(session, group_id):
            if not image_url and not video_key and not video_url:
                notice = (
                    f"Your quest submission did not include a screenshot "
                    f"(required for {group.group_name}). Please enable screenshots "
                    f"in the DropTracker plugin configuration."
                )
                print(f"Quest submission did not include a screenshot (required for {group.group_name}).")
                continue

        notification_data = {
            "group_id": group_id,
            "player_name": player_name,
            "player_id": player_id,
            "quest_id": quest_entry.id,
            "guid": unique_id,
            "quest_name": quest_name,
            "quests_completed": quests_completed,
            "total_quests": total_quests,
            "completion_percentage": completion_percentage,
            "quest_points": quest_points,
            "total_quest_points": total_quest_points,
            "qp_percentage": qp_percentage,
            "timestamp": timestamp,
            "image_url": image_url or "",
            "video_key": video_key,
            "video_url": video_url,
        }
        print(f"Notification data: {notification_data}")

        # DM notifications (inferred key: dm_quests)
        if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_quests"):
            await create_notification(
                "dm_quest",
                player_id,
                notification_data,
                group_id,
                existing_session=session if use_external_session else None,
            )

        await create_notification(
            "quest",
            player_id,
            notification_data,
            group_id,
            existing_session=session if use_external_session else None,
        )

    print(f"[QUEST] === QUEST PROCESSOR END ===")
    return SubmissionResponse(success=True, message=f"Quest recorded: {quest_name}", notice=notice or None)


