"""Pet submissions processor."""

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from .common import (
    SubmissionResponse,
    apply_account_type,
    ensure_player_by_name_then_auth,
    ensure_item_by_name,
    ensure_npc_id_for_player,
    get_player_groups_with_global,
    is_user_dm_enabled,
    create_notification,
    screenshot_required,
    select_session_and_flag,
    ensure_can_create,
    debug_print,
    award_points_to_player,
    get_config_prefix,
    envelope_from_plugin,
    SEASONAL_WORLD_TYPE,
    SeasonalPlayerPet,
)


async def pet_processor(pet_data, external_session=None, world_type="main"):
    debug_print(f"=== PET PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"Raw pet data: {pet_data}")
    debug_print(f"External session provided: {external_session is not None}")

    config_prefix = get_config_prefix(world_type)
    is_seasonal = world_type == SEASONAL_WORLD_TYPE
    session, use_external_session = select_session_and_flag(external_session)
    debug_print(f"Using external session: {use_external_session}")

    player_name = pet_data.get("player_name", pet_data.get("player", None))
    if not player_name:
        debug_print("No player name found, aborting")
        return
    account_hash = pet_data.get("acc_hash", pet_data.get("account_hash", None))
    if not account_hash:
        debug_print("No account hash found, aborting")
        return
    pet_name = pet_data.get("pet_name", None)
    if not pet_name:
        debug_print("No pet name found, aborting")
        return

    auth_key = pet_data.get("auth_key", "")
    attachment_url = pet_data.get("attachment_url", None)
    attachment_type = pet_data.get("attachment_type", None)
    downloaded = pet_data.get("downloaded", False)
    image_url = pet_data.get("image_url", None)
    used_api = pet_data.get("used_api", False)
    source = pet_data.get("source", None)
    killcount = pet_data.get("killcount", None)
    milestone = pet_data.get("milestone", None)
    duplicate = pet_data.get("duplicate", False)
    previously_owned = pet_data.get("previously_owned", None)
    game_message = pet_data.get("game_message", None)
    unique_id = pet_data.get("guid", None)
    video_key = pet_data.get("video_key")
    plugin_version = pet_data.get("p_v", None)
    notice = ""
    dedup_type = "seasonal_pet" if is_seasonal else "pet"
    if not await ensure_can_create(session, unique_id, dedup_type):
        print(
            f"Pet entry with Unique ID {unique_id} already exists in the database, aborting"
        )
        return
    debug_print(
        f"Extracted pet data - Player: {player_name}, Pet: {pet_name}, Source: {source}"
    )
    debug_print(f"Account hash: {account_hash[:8]}... (truncated), Duplicate: {duplicate}")
    debug_print(f"Attachment URL: {attachment_url}, Type: {attachment_type}, Downloaded: {downloaded}")

    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        debug_print("Player not found in the database, aborting")
        return
    player_id = player.player_id
    if not user_exists or not authed:
        debug_print("User failed auth check")
        return
    apply_account_type(player, pet_data.get("account_type"), world_type)

    pet_item = await ensure_item_by_name(session, pet_name)
    if not pet_item:
        debug_print(f"Pet item {pet_name} not found in database")
        pet_item_id = None
    else:
        pet_item_id = pet_item.item_id
        debug_print(f"Pet item validated - ID: {pet_item_id}, Name: {pet_name}")

    npc_id = None
    npc_name = source
    if source:
        npc_id, npc_name = await ensure_npc_id_for_player(
            session, source, player_id, player_name, use_external_session
        )
        debug_print(f"NPC resolved - ID: {npc_id}, Name: {npc_name}")
    else:
        # Skilling pets carry no NPC/boss source and the plugin doesn't send one.
        # Attribute the skill as the source for display, but do NOT run it through
        # NPC resolution — a skill is not an npc_list entry (would mint a bogus row
        # and queue a "new_npc" notification).
        from utils.osrs_pets import skilling_pet_source

        skill_source = skilling_pet_source(pet_name)
        if skill_source:
            source = skill_source
            npc_name = skill_source
            debug_print(f"Skilling pet source attributed: {source}")

    from db import PlayerPet

    pet_model = SeasonalPlayerPet if is_seasonal else PlayerPet
    existing_pet = None
    new_pet = None
    if pet_item_id:
        existing_pet = (
            session.query(pet_model)
            .filter(pet_model.player_id == player_id, pet_model.item_id == pet_item_id)
            .first()
        )

    is_new_pet = existing_pet is None
    dl_path = ""
    if is_new_pet and pet_item_id:
        debug_print(f"Creating new pet entry for {player_name}: {pet_name}")
        try:
            new_pet = pet_model(player_id=player_id, item_id=pet_item_id, pet_name=pet_name, unique_id=unique_id, date_added=datetime.now())
            # SAVEPOINT, not a bare add: a unique-violation on unique_id must
            # discard only this row, not whatever else the caller has staged
            # on a shared session. Same shape as services/badges.py.
            with session.begin_nested():
                session.add(new_pet)
                session.flush()
            session.commit()
            debug_print(f"Pet entry created successfully")
        except IntegrityError:
            # web84a's UNIQUE index caught a replay of this exact submission —
            # the row already exists, so this is a no-op SUCCESS, not an error.
            # Treated as "not new" so it doesn't notify a second time.
            debug_print(f"Pet entry for unique_id {unique_id} already exists — replay, ignoring")
            is_new_pet = False
            new_pet = None
        except Exception as e:
            debug_print(f"Error creating pet entry: {e}")
            if not use_external_session:
                session.rollback()
            return
    elif existing_pet:
        debug_print(f"Pet {pet_name} already exists for player {player_name}")

    if is_new_pet and attachment_url and not downloaded:
        try:
            from .common import get_extension_from_content_type, download_player_image

            file_extension = get_extension_from_content_type(attachment_type)
            file_name = f"pet_{player_id}_{pet_name.replace(' ', '_')}_{int(datetime.now().timestamp())}"
            dl_path, external_url = await download_player_image(
                submission_type="pet",
                file_name=file_name,
                player=player,
                attachment_url=attachment_url,
                file_extension=file_extension,
                entry_id=existing_pet.id if existing_pet else 0,
                entry_name=pet_name,
            )
            if external_url:
                dl_path = external_url
        except Exception as e:
            from .common import app_logger

            app_logger.log(
                log_type="error",
                data=f"Couldn't download pet image: {e}",
                app_name="core",
                description="pet_processor",
            )
    elif downloaded:
        dl_path = image_url
    if is_new_pet and not is_seasonal:
        award_points_to_player(
            player_id=player_id, amount=50, source=f"Pet: {pet_name}", expires_in_days=60
        )
        # Site-wide ticker (rt:feed): pets are rare enough to always announce.
        try:
            from services.realtime import publish_feed_pet

            publish_feed_pet(
                player_id=player_id,
                player_name=player.player_name or player_name,
                pet_name=pet_name,
                item_id=pet_item_id,
                npc_name=npc_name,
            )
        except Exception as e:
            debug_print(f"Ticker pet publish failed: {e}")

    if not is_seasonal:
        # Event engine hook: fire-and-forget LPUSH — DUPLICATES INCLUDED.
        # `is_new_pet` answers "has DropTracker seen this pet for this player
        # before", NOT "did a pet drop just happen", and only the latter can
        # decide event credit. An item_collection task listing a pet in
        # `config.pet_items` genuinely needs the duplicate: a "5 of these 4
        # items" tile is unsatisfiable without one, and a pet-flagged name is
        # refused from drop/clog envelopes, so this is its ONLY credit path.
        # The flag rides along instead and the MATCHER decides per task type
        # (pet_collection / loot_sweep reject duplicates — match_task).
        # Never fails the submission.
        try:
            from services.event_engine import queue_submission
            queue_submission(
                "pet", player_id, unique_id,
                {
                    "pet_name": pet_name,
                    "item_id": pet_item_id,
                    "item_name": pet_name,
                    "npc_name": npc_name,
                    "killcount": killcount,
                    # None on a duplicate — no player_pets row is written for
                    # one. submission_guid is what dedupes the ledger anyway.
                    "source_id": getattr(new_pet, "id", None),
                    # DB-derived bool. Deliberately not the payload's
                    # `duplicate`, which arrives from the webhook as the STRING
                    # "true"/"false" (both truthy).
                    "is_new_pet": is_new_pet,
                },
                world_type=world_type, player_name=player_name,
                # used_api means "came from the plugin" to the events engine
                # (API and Discord-webhook intake both count; mirrors drop.py).
                used_api=envelope_from_plugin(pet_data),
            )
        except Exception:
            pass

    should_notify = is_new_pet or (duplicate and not is_new_pet)
    if should_notify:
        debug_print(f"Creating notifications for pet submission")
        player_groups = get_player_groups_with_global(session, player)
        # Per-group manual policy (suggestion #45): suppress this group's
        # notification for a manual submission it doesn't trust. NB pet_data
        # ["source"] is the pet's NPC — the intake marker is "intake_source".
        manual_suppressed = set()
        if pet_data.get("intake_source") == "manual" and not is_seasonal:
            try:
                from .manual_policy import manual_notification_suppressed_groups
                manual_suppressed = manual_notification_suppressed_groups(
                    session, player, [g.group_id for g in player_groups]
                )
            except Exception as e:
                print(f"[ManualPolicy] pet suppression check failed: {e}")
        for group in player_groups:
            debug_print(f"Checking group: {group.group_name}")
            group_id = group.group_id
            if group_id in manual_suppressed:
                continue
            from utils import group_config as gc
            pet_notify_val = gc.get(session, group_id, f"{config_prefix}notify_pets")
            group_points_result = {
                "receiver_points_awarded": 0,
                "receiver_current_points": 0,
                "total_points_awarded": 0,
                "awarded_members": [],
            }
            if is_seasonal:
                # TODO: award group points for seasonal pets once award_points_in_leagues
                # config key is supported.
                pass
            else:
                async def perform_point_check():
                    nonlocal group_points_result
                    from .common import check_group_point_system_active
                    if check_group_point_system_active(group_id, session):
                        from .point_awards import check_and_award_points
                        pet_entry_id = None
                        if new_pet is not None:
                            pet_entry_id = getattr(new_pet, "id", None)
                        elif existing_pet is not None:
                            pet_entry_id = getattr(existing_pet, "id", None)

                        group_points_result = await check_and_award_points(
                            "pet",
                            group_id,
                            player_id,
                            1,
                            entry_id=pet_entry_id,
                            submission_timestamp=pet_data.get("timestamp"),
                            external_session=session,
                        )
                    else:
                        pass
                try:
                    await perform_point_check()
                except Exception as e:
                    print(f"Couldn't perform check against group point awards... e: {e}")
                    pass
            debug_print(f"Pet notify config for group {group_id}: {pet_notify_val}")
            if await screenshot_required(session, group_id):
                # Treat video submissions as satisfying screenshot requirement
                if not dl_path and not video_key:
                    notice = f"Your pet submission did not include a screenshot (required for {group.group_name}). Please enable screenshots in the DropTracker plugin configuration to accurately share your achievements!"
                    continue
            notify_reason = "config" if gc.is_truthy(pet_notify_val) else None
            if notify_reason is None and int(group_points_result.get("total_points_awarded", 0)) > 0:
                # notify_pets is off but points landed: the notify_points_awarded
                # toggle (opt-in, default OFF) announces the pet so points are never
                # awarded silently.
                from .common import points_notify_enabled
                if points_notify_enabled(session, group_id):
                    notify_reason = "points"
            if notify_reason is not None:
                debug_print(f"Group {group_id} pet notification firing (reason: {notify_reason})")
                awarded_members = group_points_result.get("awarded_members", []) or []
                notification_data = {
                    "group_id": group_id,
                    "player_name": player_name,
                    "player_id": player_id,
                    "guid": unique_id,
                    "pet_name": pet_name,
                    "source": source,
                    "npc_name": npc_name,
                    "killcount": killcount,
                    "milestone": milestone,
                    "duplicate": duplicate,
                    "previously_owned": previously_owned,
                    "game_message": game_message,
                    "image_url": dl_path,
                    "video_key": video_key,
                    "item_id": pet_item_id,
                    "npc_id": npc_id,
                    "is_new_pet": is_new_pet,
                    "group_points_awarded": int(group_points_result.get("receiver_points_awarded", 0)),
                    "group_points_receiver_total": int(group_points_result.get("receiver_current_points", 0)),
                    "group_points_member_count": len(awarded_members),
                    "group_points_members_awarded": awarded_members,
                    "notify_reason": notify_reason,
                    "world_type": world_type,
                    "plugin_version": plugin_version,
                }
                await create_notification(
                    "pet",
                    player_id,
                    notification_data,
                    group_id,
                    existing_session=session if use_external_session else None,
                )
                debug_print(f"Created pet notification for group {group_id}")

        # Personal submission DM (supporter perk): queued once per pet,
        # OUTSIDE the group loop — group notify/screenshot criteria must not
        # gate or duplicate a personal DM (same fix as drops, c258115).
        # Entitlement + opt-in are re-checked at send time.
        try:
            if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_pets"):
                await create_notification(
                    "dm_pet",
                    player_id,
                    {
                        "player_name": player_name,
                        "player_id": player_id,
                        "guid": unique_id,
                        "pet_name": pet_name,
                        "source": source,
                        "npc_name": npc_name,
                        "killcount": killcount,
                        "milestone": milestone,
                        "duplicate": duplicate,
                        "image_url": dl_path,
                        "video_key": video_key,
                        "world_type": world_type,
                        "plugin_version": plugin_version,
                    },
                    existing_session=session if use_external_session else None,
                )
        except Exception as e:
            print(f"Couldn't queue personal pet DM notification: {e}")

    debug_print(f"=== PET PROCESSOR END ===")
    return SubmissionResponse(success=True,
                            message="Pet processed successfully.",
                            notice=notice if notice else "")
    # return existing_pet if existing_pet else (new_pet if is_new_pet and pet_item_id else None)


