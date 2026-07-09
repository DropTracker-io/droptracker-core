"""Personal Best (PB) submissions processors, including TOB batching."""

import asyncio
from datetime import datetime

from .common import (
    SubmissionResponse,
    convert_to_ms,
    convert_from_ms,
    ensure_npc_id_for_player,
    ensure_player_by_name_then_auth,
    get_player_groups_with_global,
    create_notification,
    get_player_boss_kills,
    is_user_dm_enabled,
    award_points_to_player,
    screenshot_required,
    select_session_and_flag,
    ensure_can_create,
    debug_print,
    is_truthy_config,
    get_config_prefix,
    SEASONAL_WORLD_TYPE,
    SeasonalPersonalBestEntry,
)


def _time_to_ms(value) -> int:
    """Milliseconds from a plugin time value: formatted ("1:23.40"), raw ms
    int (form API path), or garbage ("N/A" on untimed kills, None) → 0.
    convert_to_ms alone returns None on unparseable input and raises on
    non-strings, and its results were compared unguarded."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    ms = convert_to_ms(str(value))
    return max(int(ms), 0) if ms else 0


async def pb_processor(pb_data, external_session=None, world_type="main"):
    debug_print(f"=== PB PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"Raw PB data: {pb_data}")
    debug_print(f"External session provided: {external_session is not None}")

    session, use_external_session = select_session_and_flag(external_session)
    debug_print(f"Using external session: {use_external_session}")
    config_prefix = get_config_prefix(world_type)
    is_seasonal = world_type == SEASONAL_WORLD_TYPE
    player_name = pb_data["player_name"]
    account_hash = pb_data["acc_hash"]
    boss_name = pb_data.get("npc_name", pb_data.get("boss_name", None))
    current_ms = _time_to_ms(pb_data.get("current_time_ms", pb_data.get("kill_time", 0)))
    pb_ms = _time_to_ms(pb_data.get("personal_best_ms", pb_data.get("best_time", 0)))
    if pb_ms == 0 and current_ms == 0:
        return
    team_size = pb_data.get("team_size", 1)
    # The embed path delivers "true"/"false" strings; the form path booleans.
    is_personal_best = pb_data.get("is_new_pb", pb_data.get("is_pb", False))
    is_personal_best = str(is_personal_best).strip().lower() in ("true", "1", "yes")
    time_ms = (
        current_ms if current_ms < pb_ms and current_ms != 0 else (pb_ms if pb_ms != 0 else current_ms)
    )
    auth_key = pb_data.get("auth_key", "")
    attachment_url = pb_data.get("attachment_url", None)
    attachment_type = pb_data.get("attachment_type", None)
    downloaded = pb_data.get("downloaded", False)
    image_url = pb_data.get("image_url", None)
    used_api = pb_data.get("used_api", False)
    unique_id = pb_data.get("guid", None)
    video_key = pb_data.get("video_key")
    video_url = pb_data.get("video_url")

    notice = ""

    dedup_type = "seasonal_pb" if is_seasonal else "pb"
    if not await ensure_can_create(session, unique_id, dedup_type):
        debug_print(
            f"Personal Best entry with Unique ID {unique_id} already exists in the database, aborting"
        )
        return

    player = None
    dl_path = None
    npc_name = boss_name
    npc_id, npc_name = await ensure_npc_id_for_player(
        session, npc_name, 0, player_name, use_external_session
    )
    if npc_id is None:
        return
    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        return
    player_id = player.player_id
    if not user_exists or not authed:
        return
    from db import PersonalBestEntry

    pb_model = SeasonalPersonalBestEntry if is_seasonal else PersonalBestEntry
    pb_entry = (
        session.query(pb_model)
        .filter(
            pb_model.player_id == player_id,
            pb_model.npc_id == npc_id,
            pb_model.team_size == team_size,
        )
        .first()
    )
    old_time = None

    if is_personal_best:
        if attachment_url and not downloaded:
            try:
                from .common import get_extension_from_content_type, download_player_image

                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"pb_{player_id}_{boss_name.replace(' ', '_')}_{int(datetime.now().timestamp())}"
                dl_path, external_url = await download_player_image(
                    submission_type="pb",
                    file_name=file_name,
                    player=player,
                    attachment_url=attachment_url,
                    file_extension=file_extension,
                    entry_id=pb_entry.id if pb_entry else 0,
                    entry_name=boss_name,
                )
                if external_url:
                    pb_entry.image_url = external_url
            except Exception as e:
                from .common import app_logger

                app_logger.log(
                    log_type="error",
                    data=f"Couldn't download PB image: {e}",
                    app_name="core",
                    description="pb_processor",
                )
        elif downloaded:
            dl_path = image_url
    if pb_entry:
        if pb_entry.personal_best > current_ms:
            old_time = pb_entry.personal_best
            pb_entry.personal_best = time_ms
            pb_entry.new_pb = is_personal_best
            pb_entry.kill_time = current_ms
            pb_entry.date_added = datetime.now()
            pb_entry.image_url = dl_path if dl_path else ""
            if video_url:
                pb_entry.video_url = video_url
            is_personal_best = True
        else:
            is_personal_best = False
    else:
        pb_entry = pb_model(
            player_id=player_id,
            npc_id=npc_id,
            team_size=team_size,
            new_pb=is_personal_best,
            personal_best=time_ms,
            kill_time=current_ms,
            date_added=datetime.now(),
            image_url=dl_path if dl_path else "",
            video_url=video_url,
            used_api=used_api,
            unique_id=unique_id,
        )
        session.add(pb_entry)

    if use_external_session:
        session.flush()
    else:
        session.commit()

    # Event engine hook (Task 17): gated, fire-and-forget LPUSH. EVERY kill
    # time (not just new PBs) is pushed so pb_target tasks match any
    # qualifying kill — but only a time this kill actually demonstrated: the
    # measured kill time, or the reported best when this kill just set it.
    # Never the standing PB on an untimed kill (it may predate the event).
    try:
        from services.event_engine import queue_submission
        if current_ms and current_ms > 0:
            _kill_ms = current_ms
        elif is_personal_best and pb_ms and pb_ms > 0:
            _kill_ms = pb_ms
        else:
            _kill_ms = None
        if _kill_ms:
            try:
                _kill_formatted = convert_from_ms(_kill_ms)
            except Exception:
                _kill_formatted = None
            queue_submission(
                "pb", player_id, unique_id,
                {
                    "npc_name": npc_name,
                    "time_ms": _kill_ms,
                    "team_size": team_size,
                    "kill_time_formatted": _kill_formatted,
                    "image_url": pb_entry.image_url,
                    "source_id": getattr(pb_entry, "id", None),
                },
                world_type=world_type, player_name=player_name,
                used_api=used_api,
            )
    except Exception:
        pass

    if is_personal_best:
        player_groups = get_player_groups_with_global(session, player)
        group_ids = [g.group_id for g in player_groups]
        pb_notify_configs = {}
        if group_ids:
            from utils import group_config as gc
            bulk = gc.get_bulk(session, group_ids, [f"{config_prefix}notify_pbs"])
            pb_notify_configs = {gid: val for (gid, _key), val in bulk.items()}

        for group in player_groups:
            await asyncio.sleep(0)
            group_id = group.group_id
            pb_notify_config = pb_notify_configs.get(group_id)
            group_points_result = {
                "receiver_points_awarded": 0,
                "receiver_current_points": 0,
                "total_points_awarded": 0,
                "awarded_members": [],
            }
            if is_seasonal:
                # TODO: award group points for seasonal PBs once award_points_in_leagues
                # config key is supported. Check group config for "award_points_in_leagues"
                # before calling check_and_award_points with the seasonal entry_id.
                pass
            else:
                async def perform_point_check():
                    nonlocal group_points_result
                    from .common import check_group_point_system_active
                    if check_group_point_system_active(group_id, session):
                        from .point_awards import check_and_award_points
                        group_points_result = await check_and_award_points(
                            "pb",
                            group_id,
                            player_id,
                            1,
                            entry_id=getattr(pb_entry, "id", None),
                            submission_timestamp=pb_data.get("timestamp"),
                            external_session=session,
                        )
                    else:
                        pass
                try:
                    await perform_point_check()
                except Exception as e:
                    print(f"Couldn't perform check against group point awards... e: {e}")
                    pass
            if is_truthy_config(pb_notify_configs.get(group_id)):
                if (await screenshot_required(session, group_id)):
                    # Treat video submissions as satisfying screenshot requirement
                    if not pb_entry.image_url and not video_key:
                        notice = f"Your personal best submission did not include a screenshot (required for {group.group_name}). Please enable screenshots in the DropTracker plugin configuration to accurately share your achievements!"
                        ## Continue past this group in the loop
                        continue
                notification_data = {
                    "player_name": player_name,
                    "player_id": player_id,
                    "pb_id": pb_entry.id,
                    "guid": unique_id,
                    "npc_id": npc_id,
                    "boss_name": boss_name,
                    "time_ms": time_ms,
                    "old_time_ms": old_time,
                    "team_size": team_size,
                    "kill_time_ms": current_ms,
                    "image_url": pb_entry.image_url,
                    "video_key": video_key,
                    "group_points_awarded": int(group_points_result.get("receiver_points_awarded", 0)),
                    "group_points_receiver_total": int(group_points_result.get("receiver_current_points", 0)),
                    "group_points_member_count": len(group_points_result.get("awarded_members", []) or []),
                    "group_points_members_awarded": group_points_result.get("awarded_members", []) or [],
                    "world_type": world_type,
                }
                await create_notification(
                    "pb",
                    player_id,
                    notification_data,
                    group_id,
                    existing_session=session if use_external_session else None,
                )

        # Personal submission DM (supporter perk): queued once per PB,
        # OUTSIDE the group loop — group notify/screenshot criteria must not
        # gate or duplicate a personal DM (same fix as drops, c258115).
        # Entitlement + opt-in are re-checked at send time.
        try:
            if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_pbs"):
                await create_notification(
                    "dm_pb",
                    player_id,
                    {
                        "player_name": player_name,
                        "player_id": player_id,
                        "pb_id": pb_entry.id,
                        "guid": unique_id,
                        "boss_name": boss_name,
                        "time_ms": time_ms,
                        "kill_time_ms": current_ms,
                        "old_time_ms": old_time,
                        "team_size": team_size,
                        "image_url": pb_entry.image_url,
                        "video_key": video_key,
                        "world_type": world_type,
                    },
                    existing_session=session if use_external_session else None,
                )
        except Exception as e:
            print(f"Couldn't queue personal PB DM notification: {e}")
    debug_print(f"=== PB PROCESSOR END ===")
    return SubmissionResponse(success=True,
                            message="PB entry created/modified successfully.",
                            notice=notice)


