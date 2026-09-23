"""Slayer task completion submissions processor.

One row per completed task, then one events-engine envelope, then a Discord
notification for each group that asked for one (``notify_slayer_tasks``). No
points. Which masters count is decided per consumer, never here: a
``slayer_target`` event task has its own master list, and a group's
notifications skip the masters in ``slayer_excluded_masters`` (by default the
streak-reset masters, Turael/Aya and Spria).

The master that assigned the task arrives as the RAW ``SLAYER_MASTER`` varbit
value. It is stored as sent and named here from ``utils.slayer_masters`` —
never trusted from the client's own label — so a mapping correction in the
registry corrects every row retroactively.
"""

import asyncio

from db import SlayerTaskCompletionEntry
from utils.slayer_masters import BOSS_TASK_ID, excluded_master_ids_from_config
from utils.slayer_masters import master_name as _registry_master_name

from .common import (
    SubmissionResponse,
    attach_webhook_screenshot,
    create_notification,
    ensure_player_by_name_then_auth,
    get_config_prefix,
    get_player_groups_with_global,
    screenshot_required,
    select_session_and_flag,
    ensure_can_create,
    debug_print,
    SEASONAL_WORLD_TYPE,
    envelope_from_plugin,
)


def _safe_int(value):
    if value in (None, "", "N/A"):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _safe_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


async def slayer_processor(slayer_data, external_session=None, world_type="main"):
    """
    Process a slayer task completion.

    Slayer-specific keys (sent by the plugin's SlayerHandler; all but
    task_name optional):
      - task_name           assignment name in cache spelling ("Cave kraken")
      - task_id             SLAYER_TARGET varp (98 = boss task)
      - boss_id             SLAYER_TARGET_BOSSID varbit, boss tasks only
      - is_boss             explicit flag (else derived from task_id)
      - master_id           SLAYER_MASTER varbit, raw
      - master_name         client's best-effort label (registry wins)
      - area_id             SLAYER_AREA varp
      - amount_initial / amount_killed
      - streak, points_awarded, points_total, xp_gained
      - completion_message  the raw chat line the numbers came from
      - timestamp           unix seconds

    Standard submission keys: player_name, acc_hash, auth_key, guid,
    image_url / image_path, video_key / video_url.
    """
    debug_print(f"=== SLAYER PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"[SLAYER] Raw slayer data: {slayer_data}")

    config_prefix = get_config_prefix(world_type)
    is_seasonal = world_type == SEASONAL_WORLD_TYPE
    session, use_external_session = select_session_and_flag(external_session)

    player_name = slayer_data.get("player_name") or slayer_data.get("player")
    account_hash = slayer_data.get("acc_hash") or slayer_data.get("account_hash")
    auth_key = slayer_data.get("auth_key", "")
    unique_id = slayer_data.get("guid")

    image_url = slayer_data.get("image_url") or slayer_data.get("image_path") or ""
    video_key = slayer_data.get("video_key")
    video_url = slayer_data.get("video_url")

    task_name = str(slayer_data.get("task_name") or slayer_data.get("task") or "").strip()[:120]
    task_id = _safe_int(slayer_data.get("task_id"))
    is_boss = _safe_bool(slayer_data.get("is_boss")) or task_id == BOSS_TASK_ID
    boss_id = _safe_int(slayer_data.get("boss_id")) if is_boss else None
    master_id = _safe_int(slayer_data.get("master_id"))
    master_name = _registry_master_name(master_id)
    if master_name is None:
        client_label = str(slayer_data.get("master_name") or "").strip()
        master_name = client_label[:40] or None
    completion_message = str(slayer_data.get("completion_message") or "").strip()[:255] or None
    timestamp = _safe_int(slayer_data.get("timestamp"))
    plugin_version = slayer_data.get("p_v", None)
    debug_print(
        f"[SLAYER] task_name={task_name} task_id={task_id} master_id={master_id} "
        f"master_name={master_name} unique_id={unique_id}"
    )

    if not player_name or not account_hash:
        return SubmissionResponse(success=False, message="Missing player_name or acc_hash.")
    if not task_name:
        return SubmissionResponse(success=False, message="Missing task_name.")
    dedup_type = "seasonal_slayer" if is_seasonal else "slayer"
    if unique_id and not await ensure_can_create(session, unique_id, dedup_type):
        return SubmissionResponse(success=True, message="Slayer task already processed (duplicate guid).")

    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, auth_key
    )
    if not player:
        return SubmissionResponse(success=False, message="Player not found or could not be created.")
    if not user_exists or not authed:
        return SubmissionResponse(success=False, message="Player authentication failed.")

    player_id = player.player_id
    entry = SlayerTaskCompletionEntry(
        player_id=player_id,
        task_name=task_name,
        task_id=task_id,
        boss_id=boss_id,
        master_id=master_id,
        master_name=master_name,
        area_id=_safe_int(slayer_data.get("area_id")),
        amount_initial=_safe_int(slayer_data.get("amount_initial")),
        amount_killed=_safe_int(slayer_data.get("amount_killed")),
        streak=_safe_int(slayer_data.get("streak")),
        points_awarded=_safe_int(slayer_data.get("points_awarded")),
        points_total=_safe_int(slayer_data.get("points_total")),
        xp_gained=_safe_int(slayer_data.get("xp_gained")),
        completion_message=completion_message,
        world_type=world_type,
        timestamp=timestamp,
        image_url=image_url or None,
        video_url=video_url,
        used_api=bool(slayer_data.get("used_api", False)),
        unique_id=unique_id,
    )
    session.add(entry)
    if use_external_session:
        session.flush()
    else:
        session.commit()
        session.refresh(entry)

    # Discord-webhook transport: the screenshot is a CDN link, not a saved
    # file. Same treatment as diaries so the row keeps its image.
    if not image_url:
        image_url = await attach_webhook_screenshot(
            session,
            player,
            entry,
            slayer_data,
            submission_type="slayer",
            entry_name=task_name,
            use_external_session=use_external_session,
        )

    # Event engine hook: gated, fire-and-forget LPUSH. The guid dedupe above
    # means every envelope is a first completion. Never fails the submission.
    if not is_seasonal:
        try:
            from services.event_engine import queue_submission
            queue_submission(
                "slayer", player_id, unique_id,
                {
                    "task_name": task_name,
                    "task_id": task_id,
                    "boss_id": boss_id,
                    "is_boss": is_boss,
                    "master_id": master_id,
                    "master_name": master_name,
                    "amount_killed": entry.amount_killed,
                    "amount_initial": entry.amount_initial,
                    "streak": entry.streak,
                    "points_awarded": entry.points_awarded,
                    "image_url": image_url or None,
                    "source_id": getattr(entry, "id", None),
                },
                world_type=world_type, player_name=player_name,
                used_api=envelope_from_plugin(slayer_data),
            )
        except Exception:
            pass

    # Group notifications. The completion is stored and counted by now, so a
    # failure here costs the announcement and nothing else.
    notice = ""
    try:
        notice = await _queue_group_notifications(
            session,
            player,
            entry,
            player_name=player_name,
            config_prefix=config_prefix,
            unique_id=unique_id,
            image_url=image_url,
            video_key=video_key,
            video_url=video_url,
            world_type=world_type,
            plugin_version=plugin_version,
            use_external_session=use_external_session,
        )
    except Exception as e:
        print(f"[SLAYER] Couldn't queue slayer task notifications: {e}")

    debug_print("[SLAYER] === SLAYER PROCESSOR END ===")
    return SubmissionResponse(
        success=True, message=f"Slayer task recorded: {task_name}", notice=notice or None
    )


def notification_payload(entry, *, group_id, player_name, unique_id, image_url,
                         video_key, video_url, world_type, plugin_version) -> dict:
    """What the notification sender is given for one group. Figures are the
    stored row's, so the message says what the database says."""
    return {
        "group_id": group_id,
        "player_name": player_name,
        "player_id": entry.player_id,
        "slayer_id": getattr(entry, "id", None),
        "guid": unique_id,
        "task_name": entry.task_name,
        "task_id": entry.task_id,
        "boss_id": entry.boss_id,
        "master_id": entry.master_id,
        "master_name": entry.master_name,
        "amount_initial": entry.amount_initial,
        "amount_killed": entry.amount_killed,
        "streak": entry.streak,
        "points_awarded": entry.points_awarded,
        "points_total": entry.points_total,
        "xp_gained": entry.xp_gained,
        "timestamp": entry.timestamp,
        "image_url": image_url or "",
        "video_key": video_key,
        "video_url": video_url,
        "world_type": world_type,
        "plugin_version": plugin_version,
    }


async def _queue_group_notifications(session, player, entry, *, player_name,
                                     config_prefix, unique_id, image_url, video_key, video_url,
                                     world_type, plugin_version,
                                     use_external_session) -> str:
    """Queue one ``slayer`` notification per group that wants this task.

    Returns the screenshot notice for the plugin, or "". A group is skipped
    when it has the notification off (the default), when the task's master is
    one it leaves out, or when it requires a screenshot and there is none.
    Channel, hidden-player and blacklist gates are create_notification's.
    """
    from utils import group_config as gc

    notice = ""
    for group in get_player_groups_with_global(session, player):
        await asyncio.sleep(0)
        group_id = group.group_id

        if not gc.is_truthy(gc.get(session, group_id, f"{config_prefix}{gc.NOTIFY_SLAYER_TASKS}")):
            continue

        excluded = excluded_master_ids_from_config(
            gc.get(session, group_id, gc.SLAYER_EXCLUDED_MASTERS)
        )
        if entry.master_id is not None and entry.master_id in excluded:
            debug_print(
                f"[SLAYER] {entry.master_name or entry.master_id} tasks are skipped "
                f"by group {group.group_name}"
            )
            continue

        if await screenshot_required(session, group_id):
            if not image_url and not video_key and not video_url:
                notice = (
                    f"Your slayer task submission did not include a screenshot "
                    f"(required for {group.group_name}). Please enable screenshots "
                    f"in the DropTracker plugin configuration."
                )
                continue

        await create_notification(
            "slayer",
            entry.player_id,
            notification_payload(
                entry,
                group_id=group_id,
                player_name=player_name,
                unique_id=unique_id,
                image_url=image_url,
                video_key=video_key,
                video_url=video_url,
                world_type=world_type,
                plugin_version=plugin_version,
            ),
            group_id,
            existing_session=session if use_external_session else None,
        )
    return notice
