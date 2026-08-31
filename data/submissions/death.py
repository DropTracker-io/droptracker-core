"""Player death submissions processor."""

import asyncio

from db import PlayerDeath
from db.death_filter import parse_flag
from db.models import Group
from utils.death_regions import is_safe_region

from .common import (
    SubmissionResponse,
    attach_webhook_screenshot,
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


def _safe_str(value, limit):
    """A trimmed display string, or ``None``.

    ``addFields`` on the plugin side writes the literal ``"N/A"`` for anything
    it had no value for, so that string means "absent", not a real name.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    return text[:limit]


async def death_processor(death_data, external_session=None, world_type="main"):
    """
    Process player death submissions.

    Expected death-specific keys (sent by the client/plugin):
      - source (killer NPC/player name; aliases: killer, npc_name)
      - region_id, region_name, region_type, location
      - killer_type ("npc" | "player" | "unknown"), is_pvp
      - is_safe_death (whether the location costs items; plugin 6.0+)
      - value_lost, value_kept, items_lost (plugin 6.0.4+)
      - timestamp (unix seconds)

    Everything above arrives as a STRING: the plugin builds embed fields with
    ``String.valueOf``, so ``is_safe_death`` is ``"true"``, not ``True``, and a
    field it had no value for is the literal ``"N/A"``. Parse, never trust the
    type — and keep "absent" distinct from "false", because the two mean very
    different things to the safe-death filter.

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
    plugin_version = death_data.get("p_v", None)

    source = death_data.get("source") or death_data.get("killer") or death_data.get("npc_name") or ""
    region_id = _safe_int(death_data.get("region_id"))
    location = death_data.get("location") or ""
    timestamp = death_data.get("timestamp")

    # Everything the plugin knows about the death beyond who and where. These
    # travelled in the payload long before anything read them; they are parsed
    # here so the row records them AND so they reach `notification_data` below,
    # which is the only thing the send-side filters can see.
    region_name = _safe_str(death_data.get("region_name"), 125) or _safe_str(location, 125)
    region_type = _safe_str(death_data.get("region_type"), 32)
    killer_type = _safe_str(death_data.get("killer_type"), 16)
    is_pvp = parse_flag(death_data.get("is_pvp"))

    # `is_safe_death` is the client's own verdict (6.0+); it is authoritative
    # because only the client can see account type and Pest Control state. When
    # it is missing the server classifies the region alone — see db.death_filter.
    is_safe_death = parse_flag(death_data.get("is_safe_death"))
    if is_safe_death is None and region_id is not None:
        is_safe_death = is_safe_region(region_id)

    # What the death actually cost (plugin 6.0.4+). Absent from older clients,
    # so `None` means "unknown", never "died with nothing".
    value_lost = _safe_int(death_data.get("value_lost"))
    value_kept = _safe_int(death_data.get("value_kept"))
    items_lost = _safe_int(death_data.get("items_lost"))

    debug_print(
        f"[DEATH] source={source} unique_id={unique_id} timestamp={timestamp} "
        f"safe={is_safe_death} value_lost={value_lost}"
    )

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
        region_name=region_name,
        killer_type=killer_type,
        is_pvp=is_pvp,
        is_safe_death=is_safe_death,
        value_lost=value_lost,
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

    # Discord-webhook transport: the screenshot arrives as a CDN link, not as a
    # saved file. Resolve it here, before the loop, so the image reaches the
    # notification payload and the screenshot-required gate below — not just
    # the stored row.
    if not image_url:
        image_url = await attach_webhook_screenshot(
            session,
            player,
            death_entry,
            death_data,
            submission_type="death",
            entry_name=source or "death",
            subfolder=source,
            use_external_session=use_external_session,
        )

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
            "plugin_version": plugin_version,
            # Carried so the SEND-side gates can re-decide. A queued row is all
            # notification_service ever sees, so a key left out here is a filter
            # that silently cannot run — the same way dropping the envelope's
            # `_received_at` made the month-boundary fix inert.
            "region_name": region_name,
            "region_type": region_type,
            "killer_type": killer_type,
            "is_pvp": is_pvp,
            "is_safe_death": is_safe_death,
            "value_lost": value_lost,
            "value_kept": value_kept,
            "items_lost": items_lost,
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
                    "plugin_version": plugin_version,
                    # Not filtered on (a group's settings never reach a member's
                    # own DM), but worth showing them what it cost.
                    "region_name": region_name,
                    "is_safe_death": is_safe_death,
                    "value_lost": value_lost,
                },
                existing_session=session if use_external_session else None,
            )
    except Exception as e:
        print(f"Couldn't queue personal death DM notification: {e}")

    debug_print(f"[DEATH] === DEATH PROCESSOR END ===")
    return SubmissionResponse(success=True, message="Death recorded.", notice=notice or None)
