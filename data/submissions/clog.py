"""Collection Log submissions processor."""

from datetime import datetime

from .common import (
    SubmissionResponse,
    ensure_item_by_name,
    ensure_player_by_name_then_auth,
    ensure_npc_id_for_player,
    get_player_groups_with_global,
    create_notification,
    is_user_dm_enabled,
    screenshot_required,
    select_session_and_flag,
    ensure_can_create,
    debug_print,
    award_points_to_player,
    get_config_prefix,
    SEASONAL_WORLD_TYPE,
    SeasonalCollectionLogEntry,
)


# Collection-log slots whose single display name maps to MANY distinct in-game
# item IDs — each variant is its own slot. The client resolves item_id by name
# (ClogHandler.findItemId) and our ensure_item_by_name() does the same, so every
# variant collapses onto ONE item_id. Our (player_id, item_id) slot-dedup below
# would then treat a genuinely new slot as an already-owned duplicate and
# silently drop the notification (the webhook still returns 200, so the client
# reports "processed"). For these names we skip slot-dedup so each new unlock
# notifies; exact re-sends/retries are still caught by the unique_id (guid)
# check in ensure_can_create().
#
# TO ADD A NEW ONE: just drop the exact collection-log item name into the right
# group below. Matching is case-insensitive and whitespace-trimmed (see
# is_multi_slot_clog_item), so don't worry about exact casing.
MULTI_SLOT_CLOG_ITEM_NAMES = {
    # Chompy Bird Hunting — 36 milestone hats (Ogre bowman ... Expert dragon archer)
    "Chompy bird hat",
    # Treasure Trails / clue pages & fragments
    "Ancient page",
    "Mysterious page",
    "Medallion fragment",
    # Graceful outfit — a separate slot per recolour region
    "Graceful hood",
    "Graceful top",
    "Graceful legs",
    "Graceful cape",
    "Graceful gloves",
    "Graceful boots",
    # Castle Wars decorative armour — a separate slot per set/colour. The
    # body/legs/skirt pieces share the in-game item name "Decorative armour".
    "Decorative helm",
    "Decorative full helm",
    "Decorative body",
    "Decorative legs",
    "Decorative skirt",
    "Decorative sword",
    "Decorative shield",
    "Decorative boots",
    "Decorative armour",
}

# Pre-normalised for O(1) case-insensitive membership tests.
_MULTI_SLOT_CLOG_LOOKUP = frozenset(n.strip().lower() for n in MULTI_SLOT_CLOG_ITEM_NAMES)


def is_multi_slot_clog_item(item_name) -> bool:
    """True if `item_name` is a collection-log slot that shares its display name
    with other distinct slots (see MULTI_SLOT_CLOG_ITEM_NAMES) and so must never
    be deduped by (player_id, item_id)."""
    return bool(item_name) and item_name.strip().lower() in _MULTI_SLOT_CLOG_LOOKUP


async def clog_processor(clog_data, external_session=None, world_type="main"):
    debug_print(f"=== CLOG PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"Raw clog data: {clog_data}")
    debug_print(f"External session provided: {external_session is not None}")

    config_prefix = get_config_prefix(world_type)
    is_seasonal = world_type == SEASONAL_WORLD_TYPE
    debug_test = False
    player_name = clog_data.get("player_name", clog_data.get("player", None))
    if player_name == "joelhalen":
        debug_test = True
    session, use_external_session = select_session_and_flag(external_session)
    debug_print(f"Using external session: {use_external_session}")
    if not player_name:
        debug_print("No player name found, aborting")
        return
    has_xf_entry = False

    account_hash = clog_data["acc_hash"]
    item_name = clog_data.get("item_name", clog_data.get("item", None))
    if not item_name:
        debug_print("No item name found, aborting")
        return
    auth_key = clog_data.get("auth_key", "")
    attachment_url = clog_data.get("attachment_url", None)
    attachment_type = clog_data.get("attachment_type", None)
    reported_slots = clog_data.get("reported_slots", None)
    downloaded = clog_data.get("downloaded", False)
    image_url = clog_data.get("image_url", None)
    used_api = clog_data.get("used_api", False)
    killcount = clog_data.get("kc", None)
    unique_id = clog_data.get("guid", None)
    video_key = clog_data.get("video_key")
    video_url = clog_data.get("video_url")
    plugin_version = clog_data.get("p_v", None)
    notice = ""
    item = await ensure_item_by_name(session, item_name)

    dedup_type = "seasonal_clog" if is_seasonal else "clog"
    if not await ensure_can_create(session, unique_id, dedup_type):
        print(
            f"Collection Log entry with Unique ID {unique_id} already exists in the database, aborting"
        )
        return
    if not item:
        print(f"Item {item_name} not found in database, aborting")
        return
    item_id = item.item_id
    npc_name = clog_data.get("source", None)
    npc = npc_name
    print(f"NPC: {npc}")
    npc_id = None
    if player_name is None:
        return
    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        print(f"Player does not exist, and creating failed")
        return
    player_id = player.player_id
    npc_id, npc_name = await ensure_npc_id_for_player(
        session, npc_name, player_id, player_name, use_external_session
    )
    if npc_id is None:
        return
    player = session.query(type(player)).filter(type(player).player_id == player_id).first()
    if not player:
        print("Player not found in database, aborting")
        return
    if not user_exists or not authed:
        print("user failed auth check")
        return

    from db import CollectionLogEntry

    clog_model = SeasonalCollectionLogEntry if is_seasonal else CollectionLogEntry
    # Multi-slot items (see MULTI_SLOT_CLOG_ITEM_NAMES) legitimately produce
    # several rows per (player_id, item_id) — one per slot — so never dedup
    # them by item_id; each new-unlock event is a genuinely new slot.
    if is_multi_slot_clog_item(item_name):
        clog_entry = None
    else:
        clog_entry = (
            session.query(clog_model)
            .filter(
                clog_model.player_id == player_id,
                clog_model.item_id == item_id,
            )
            .first()
        )

    is_new_clog = False
    if npc_id is None:
        print(f"We did not find an npc for {npc_name}, aborting")
        return
    if not clog_entry:
        clog_entry = clog_model(
            player_id=player_id,
            reported_slots=reported_slots,
            item_id=item_id,
            npc_id=npc_id,
            date_added=datetime.now(),
            image_url="",
            video_url=video_url,
            used_api=used_api,
            unique_id=unique_id
        )
        session.add(clog_entry)
        session.commit()

        if attachment_url and not downloaded:
            try:
                from .common import get_extension_from_content_type, download_player_image

                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"clog_{player_id}_{item_name.replace(' ', '_')}_{int(datetime.now().timestamp())}"
                dl_path, external_url = await download_player_image(
                    submission_type="clog",
                    file_name=file_name,
                    player=player,
                    attachment_url=attachment_url,
                    file_extension=file_extension,
                    entry_id=clog_entry.log_id,
                    entry_name=item_name,
                )
                clog_entry.image_url = external_url if external_url else ""
            except Exception as e:
                from .common import app_logger

                app_logger.log(
                    log_type="error",
                    data=f"Couldn't download collection log image: {e}",
                    app_name="core",
                    description="clog_processor",
                )
        elif downloaded:
            clog_entry.image_url = image_url
        if video_url:
            clog_entry.video_url = video_url

        is_new_clog = True
        print("Added clog to session")
    print("Committing session")
    session.commit()

    if is_new_clog:
        # Event engine hook (Task 17): gated, fire-and-forget LPUSH; new
        # collection log slots only. Never fails the submission.
        try:
            from services.event_engine import queue_submission
            queue_submission(
                "clog", player_id, unique_id,
                {
                    "item_name": item_name,
                    "item_id": item_id,
                    "kc": killcount,
                    "npc_name": npc,
                    "image_url": clog_entry.image_url,
                    "source_id": getattr(clog_entry, "log_id", None),
                },
                world_type=world_type, player_name=player_name,
                used_api=used_api,
            )
        except Exception:
            pass

        print("New collection log -- Creating notification")
        if not is_seasonal:
            award_points_to_player(
                player_id=player_id,
                amount=5,
                source=f"Collection Log slot: {item_name}",
                expires_in_days=60,
            )
        player_groups = get_player_groups_with_global(session, player)
        # Per-group manual policy (suggestion #45): withhold this group's
        # notification (and group points) for a manual submission the group
        # doesn't trust. Non-drop types have no leaderboard, so this is
        # notification-only (no review queue).
        manual_suppressed = set()
        if clog_data.get("intake_source") == "manual" and not is_seasonal:
            try:
                from .manual_policy import manual_notification_suppressed_groups
                manual_suppressed = manual_notification_suppressed_groups(
                    session, player, [g.group_id for g in player_groups]
                )
            except Exception as e:
                print(f"[ManualPolicy] clog suppression check failed: {e}")
        for group in player_groups:
            print(f"CLOG: Checking group: {group}")
            group_id = group.group_id
            if group_id in manual_suppressed:
                continue
            group_points_result = {
                "receiver_points_awarded": 0,
                "receiver_current_points": 0,
                "total_points_awarded": 0,
                "awarded_members": [],
            }
            if is_seasonal:
                # TODO: award group points for seasonal collection log entries once
                # award_points_in_leagues config key is supported.
                pass
            else:
                async def perform_point_check():
                    nonlocal group_points_result
                    from .common import check_group_point_system_active
                    if check_group_point_system_active(group_id, session):
                        from .point_awards import check_and_award_points
                        group_points_result = await check_and_award_points(
                            "clog",
                            group_id,
                            player_id,
                            1,
                            entry_id=getattr(clog_entry, "log_id", None),
                            submission_timestamp=clog_data.get("timestamp"),
                            external_session=session,
                        )
                    else:
                        pass
                try:
                    await perform_point_check()
                except Exception as e:
                    print(f"Couldn't perform check against group point awards... e: {e}")
                    pass
            from utils import group_config as gc
            if gc.is_truthy(gc.get(session, group_id, f"{config_prefix}notify_clogs")):
                if await screenshot_required(session, group_id):
                    # Treat video submissions as satisfying screenshot requirement
                    if not clog_entry.image_url and not video_key:
                        notice = f"Your Collection Log submission ({item_name}) did not include a screenshot (required for {group.group_name}). Please enable screenshots in the DropTracker plugin configuration to accurately share your achievements!"
                        continue ## Skip this group in the loop
                notification_data = {
                    "player_name": player_name,
                    "player_id": player_id,
                    "guid": unique_id,
                    "item_name": item_name,
                    "npc_name": npc,
                    "image_url": clog_entry.image_url,
                    "video_key": video_key,
                    "kc_received": killcount,
                    "item_id": item_id,
                    "group_points_awarded": int(group_points_result.get("receiver_points_awarded", 0)),
                    "group_points_receiver_total": int(group_points_result.get("receiver_current_points", 0)),
                    "group_points_member_count": len(group_points_result.get("awarded_members", []) or []),
                    "group_points_members_awarded": group_points_result.get("awarded_members", []) or [],
                    "world_type": world_type,
                    "plugin_version": plugin_version,
                }
                await create_notification(
                    "clog",
                    player_id,
                    notification_data,
                    group_id,
                    existing_session=session if use_external_session else None,
                )
        # Personal submission DM (supporter perk): queued once per new clog
        # slot, OUTSIDE the group loop with no group_id — group notify
        # criteria must not gate or duplicate a personal DM (same fix as
        # drops, c258115). Entitlement + opt-in are re-checked at send time.
        try:
            if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_clogs"):
                await create_notification(
                    "dm_clog",
                    player_id,
                    {
                        "player_name": player_name,
                        "player_id": player_id,
                        "guid": unique_id,
                        "item_name": item_name,
                        "npc_name": npc,
                        "image_url": clog_entry.image_url,
                        "video_key": video_key,
                        "kc_received": killcount,
                        "item_id": item_id,
                        "world_type": world_type,
                    },
                    existing_session=session if use_external_session else None,
                )
        except Exception as e:
            print(f"Couldn't queue personal clog DM notification: {e}")
    debug_print("Returning clog entry")
    debug_print(f"=== CLOG PROCESSOR END ===")
    return SubmissionResponse(success=True,
                             message="Collection Log processed successfully.",
                             notice=notice if notice else "")
    # return clog_entry


