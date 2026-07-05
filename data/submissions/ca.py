"""Combat Achievements submissions processor."""

from datetime import datetime

from .common import (
    SubmissionResponse,
    ensure_player_by_name_then_auth,
    ensure_can_create,
    screenshot_required,
    select_session_and_flag,
    create_notification,
    get_player_groups_with_global,
    award_points_to_player,
    debug_print,
    get_config_prefix,
    SEASONAL_WORLD_TYPE,
    SeasonalCombatAchievementEntry,
)


async def ca_processor(ca_data, external_session=None, world_type="main"):
    debug_print(f"=== CA PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"Raw CA data: {ca_data}")
    debug_print(f"External session provided: {external_session is not None}")

    config_prefix = get_config_prefix(world_type)
    is_seasonal = world_type == SEASONAL_WORLD_TYPE
    has_xf_entry = False
    session, use_external_session = select_session_and_flag(external_session)
    debug_print(f"Using external session: {use_external_session}")
    player_name = ca_data["player_name"]
    account_hash = ca_data["acc_hash"]
    points_awarded = ca_data["points"]
    points_total = ca_data["total_points"]
    completed_tier = ca_data.get("completed", None)
    task_name = ca_data.get("task", None)
    tier = ca_data["tier"]
    auth_key = ca_data.get("auth_key", "")
    attachment_url = ca_data.get("attachment_url", None)
    attachment_type = ca_data.get("attachment_type", None)
    downloaded = ca_data.get("downloaded", False)
    image_url = ca_data.get("image_url", None)
    used_api = ca_data.get("used_api", False)
    unique_id = ca_data.get("guid", None)
    video_key = ca_data.get("video_key")
    video_url = ca_data.get("video_url")
    notice = ""
    debug_print(
        f"Extracted CA data - Player: {player_name}, Task: {task_name}, Tier: {tier}"
    )
    debug_print(
        f"Points awarded: {points_awarded}, Total points: {points_total}, Completed tier: {completed_tier}"
    )
    debug_print(
        f"Account hash: {account_hash[:8]}... (truncated), Used API: {used_api}"
    )

    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        debug_print("Player still not found in the database, aborting")
        return
    player_id = player.player_id
    if not user_exists or not authed:
        debug_print("User failed auth check")
        return

    dedup_type = "seasonal_ca" if is_seasonal else "ca"
    if not await ensure_can_create(session, unique_id, dedup_type):
        debug_print(
            f"Combat Achievement entry with Unique ID {unique_id} already exists in the database, aborting"
        )
        return
    from db import CombatAchievementEntry

    ca_model = SeasonalCombatAchievementEntry if is_seasonal else CombatAchievementEntry
    ca_entry = (
        session.query(ca_model)
        .filter(
            ca_model.player_id == player_id,
            ca_model.task_name == task_name,
        )
        .first()
    )
    is_new_ca = False

    if not ca_entry:
        debug_print(
            "CA entry not found in the database, creating new entry - Task tier: "
            + str(tier)
        )
        dl_path = ""
        ca_entry = ca_model(
            player_id=player_id,
            task_name=task_name,
            date_added=datetime.now(),
            image_url=dl_path,
            video_url=video_url,
            used_api=used_api,
            unique_id=unique_id,
        )
        session.add(ca_entry)
        is_new_ca = True
        if attachment_url and not downloaded:
            try:
                from .common import get_extension_from_content_type, download_player_image

                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"ca_{player_id}_{task_name.replace(' ', '_')}_{int(datetime.now().timestamp())}"
                player = session.query(type(player)).filter(type(player).player_id == player_id).first()
                if not player:
                    debug_print("Player not found in database, aborting")
                    return
                dl_path, external_url = await download_player_image(
                    submission_type="ca",
                    file_name=file_name,
                    player=player,
                    attachment_url=attachment_url,
                    file_extension=file_extension,
                    entry_id=ca_entry.id,
                    entry_name=task_name,
                )
                if external_url:
                    ca_entry.image_url = external_url
            except Exception as e:
                from .common import app_logger

                app_logger.log(
                    log_type="error",
                    data=f"Couldn't download CA image: {e}",
                    app_name="core",
                    description="ca_processor",
                )
        elif downloaded:
            ca_entry.image_url = image_url
        if video_url:
            ca_entry.video_url = video_url
    session.commit()
    debug_print("Committed a new CA entry")

    # Event engine hook (Task 17): gated, fire-and-forget LPUSH; new CA
    # entries only. Never fails the submission.
    if is_new_ca:
        try:
            from services.event_engine import queue_submission
            queue_submission(
                "ca", player_id, unique_id,
                {
                    "task_name": task_name,
                    "tier": tier,
                    "image_url": ca_entry.image_url,
                    "source_id": getattr(ca_entry, "id", None),
                },
                world_type=world_type, player_name=player_name,
            )
        except Exception:
            pass

    ca_tier = ""
    match str(tier).strip().lower():
        case "easy":
            points = 1
            ca_tier = "easy_ca"
        case "medium":
            points = 2
            ca_tier = "medium_ca"
        case "hard":
            points = 3
            ca_tier = "hard_ca"
        case "elite":
            points = 4
            ca_tier = "elite_ca"
        case "master":
            points = 5
            ca_tier = "master_ca"
        case "grandmaster":
            points = 6
            ca_tier = "grandmaster_ca"
        case _:
            points = 1
    if not is_seasonal:
        try:
            award_points_to_player(
                player_id=player_id,
                amount=points,
                source=f"Combat Achievement: {task_name}",
                expires_in_days=60,
            )
        except Exception as e:
            debug_print(f"Couldn't award points to player: {e}")
            from .common import app_logger
            app_logger.log(
                log_type="error",
                data=f"Couldn't award points to player: {e}",
                app_name="core",
                description="ca_processor",
            )
    if is_new_ca:
        debug_print("New CA entry, creating notification")
        player_groups = get_player_groups_with_global(session, player)
        for group in player_groups:
            debug_print("Checking group: " + str(group))
            group_id = group.group_id
            group_points_result = {
                "receiver_points_awarded": 0,
                "receiver_current_points": 0,
                "total_points_awarded": 0,
                "awarded_members": [],
            }
            if is_seasonal:
                # TODO: award group points for seasonal CAs once award_points_in_leagues
                # config key is supported.
                pass
            else:
                try:
                    from .common import check_group_point_system_active
                    if check_group_point_system_active(group_id, session):
                        from .point_awards import check_and_award_points
                        group_points_result = await check_and_award_points(
                            ca_tier,
                            group_id,
                            player_id,
                            1,
                            entry_id=getattr(ca_entry, "id", None),
                            submission_timestamp=ca_data.get("timestamp"),
                            external_session=session,
                        )
                except Exception as e:
                    print(f"Couldn't perform check against group point awards... e: {e}")
            from utils import group_config as gc
            ca_notify_val = gc.get(session, group_id, f"{config_prefix}notify_cas")
            debug_print("CA notify config: " + str(ca_notify_val))
            if gc.is_truthy(ca_notify_val):
                min_tier_raw = gc.get(session, group_id, f"{config_prefix}min_ca_tier_to_notify")
                # Wrap in a 1-tuple to preserve the existing min_tier[0] access pattern below
                min_tier = (min_tier_raw,) if min_tier_raw is not None else None
                tier_order = ["easy", "medium", "hard", "elite", "master", "grandmaster"]
                if min_tier != "disabled" or group_id == 2:
                    if (min_tier and min_tier[0].lower() in tier_order) or group_id == 2:
                        min_tier_value = min_tier[0].lower()
                        min_tier_index = tier_order.index(min_tier_value)
                        task_tier_index = tier_order.index(tier.lower()) if tier.lower() in tier_order else -1
                        if task_tier_index < min_tier_index:
                            debug_print(
                                f"Skipping {task_name} ({tier}) as it's below minimum tier {min_tier_value} for group {group_id}"
                            )
                            continue
                        else:
                            debug_print("Tier meets minimum notification tier")
                            if await screenshot_required(session,group_id):
                                # Treat video submissions as satisfying screenshot requirement
                                if not ca_entry.image_url and not video_key:
                                    notice = f"Your combat achievement submission did not include a screenshot (required for {group.group_name}). Please enable screenshots in the DropTracker plugin configuration to accurately share your achievements!"
                                    continue ## Skip this group inside the loop
                            notification_data = {
                                "player_name": player_name,
                                "player_id": player_id,
                                "guid": unique_id,
                                "task_name": task_name,
                                "tier": tier,
                                "points_awarded": points_awarded,
                                "points_total": points_total,
                                "completed_tier": completed_tier,
                                "image_url": ca_entry.image_url,
                                "video_key": video_key,
                                "group_points_awarded": int(group_points_result.get("receiver_points_awarded", 0)),
                                "group_points_receiver_total": int(group_points_result.get("receiver_current_points", 0)),
                                "group_points_member_count": len(group_points_result.get("awarded_members", []) or []),
                                "group_points_members_awarded": group_points_result.get("awarded_members", []) or [],
                                "world_type": world_type,
                            }
                            if player and player.user:
                                user = session.query(type(player.user)).filter(type(player.user).user_id == player.user_id).first()
                                if user:
                                    from .common import is_user_dm_enabled

                                    if is_user_dm_enabled(session, user.user_id, "dm_cas"):
                                        await create_notification(
                                            "dm_ca",
                                            player_id,
                                            notification_data,
                                            group_id,
                                            existing_session=session if use_external_session else None,
                                        )
                            await create_notification(
                                "ca",
                                player_id,
                                notification_data,
                                group_id,
                                existing_session=session if use_external_session else None,
                            )
    debug_print(f"=== CA PROCESSOR END ===")
    return SubmissionResponse(success=True,
                              message="Combat Achievement proccessed successfully.",
                              notice=notice if notice else "")


