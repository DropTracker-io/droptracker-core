"""Personal Best (PB) submissions processors, including TOB batching."""

import asyncio
from datetime import datetime

from sqlalchemy.exc import IntegrityError

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
    envelope_from_plugin,
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
    # Canonical team encoding ("Solo", "2", …, "11-15") — see suggestion #50.
    from utils.npc_names import sanitize_team_size

    team_size = sanitize_team_size(pb_data.get("team_size", 1))
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
    plugin_version = pb_data.get("p_v", None)

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
    # Hard-block: some NPCs have no real personal best (the game exposes none, so
    # our tracking is bugged and produces junk). PB submissions for a runtime-
    # managed set of npc_ids are dropped here at the single intake chokepoint —
    # covering every path (webhook API, queue consumer, both bots) and both
    # world types. Managed by superadmins via utils/pb_blocklist.
    try:
        from utils import pb_blocklist

        if pb_blocklist.is_blocked(npc_id, session):
            debug_print(f"PB submission for blocked npc_id={npc_id} ({npc_name}) dropped")
            return
    except Exception:
        # Fail open: a blocklist read error must never break PB intake.
        pass
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
                # dl_path is the local filesystem path; what gets stored on
                # the row (and shipped in notification embeds) must be the
                # public URL. Assigning through pb_entry here also crashed on
                # first-ever PBs (pb_entry is None until the row is created
                # below) — the non-API screenshot bug reported 2026-07-15.
                if external_url:
                    dl_path = external_url
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
    # Whether the *stored* PB row changed — i.e. whether the Hall of Fame
    # leaderboard for this boss could now look different (a faster time, or a
    # brand-new team-size bracket). Drives the near-real-time HOF refresh below.
    pb_row_changed = False
    # Whether THIS kill demonstrably set the best. A "Personal best: X"
    # segment (pb_ms) on the message means the game did NOT call this kill a
    # PB — plugin message merges can pair a slower kill_time with a faster
    # reported best (ticket #361: ToB's "total completion time" line
    # overwrites the in-raid time), and a stale stored row must not turn that
    # into a fake announcement. Only a kill faster than both the stored row
    # and the reported best earns a notification/points/ticker.
    beats_reported = pb_ms == 0 or (0 < current_ms < pb_ms)
    if pb_entry:
        # `current_ms == 0` means the kill had no timer ("N/A"), NOT an
        # infinitely fast kill — but `stored > 0` is true for every real row,
        # so an untimed kill used to pass this gate and overwrite a good time
        # with whatever the plugin currently believes its PB is, which can be
        # SLOWER than what we already hold.
        if current_ms > 0 and pb_entry.personal_best > current_ms and beats_reported:
            old_time = pb_entry.personal_best
            pb_entry.personal_best = time_ms
            pb_entry.new_pb = True
            pb_entry.kill_time = current_ms
            pb_entry.date_added = datetime.now()
            pb_entry.image_url = dl_path if dl_path else ""
            if video_url:
                pb_entry.video_url = video_url
            is_personal_best = True
            pb_row_changed = True
        else:
            is_personal_best = False
            # Every non-PB kill message still carries the game's standing PB
            # (pb_ms). When it is faster than the row, the row is stale — a
            # missed or clobbered earlier submission. Sync it down silently
            # so PBs self-heal on every kill (mirrors the adventure-log
            # convention): no notification, no points, no ticker — the HOF
            # refresh below still fires because the board changed.
            if 0 < pb_ms < pb_entry.personal_best:
                pb_entry.personal_best = pb_ms
                pb_entry.kill_time = pb_ms
                pb_entry.new_pb = False
                pb_entry.date_added = datetime.now()
                pb_row_changed = True
    else:
        # First row for this (player, npc, team size). time_ms is already
        # min(kill, reported best), so the create itself "syncs"; the
        # notification decision still requires the kill to have earned it.
        is_personal_best = is_personal_best and beats_reported
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
        # SAVEPOINT: web84a made unique_id UNIQUE here, so a replay of this
        # submission raises instead of inserting a second row. That is a no-op
        # SUCCESS — the PB is already recorded — and it must discard only this
        # row, not anything else staged on a caller-owned session.
        try:
            with session.begin_nested():
                session.add(pb_entry)
                session.flush()
            pb_row_changed = True
        except IntegrityError:
            debug_print(
                f"PB entry with Unique ID {unique_id} already exists — replay, ignoring"
            )
            return

    if use_external_session:
        session.flush()
    else:
        session.commit()

    # Near-real-time Hall of Fame refresh (main-world only; the HOF reads
    # PersonalBestEntry, not the seasonal mirror). Best-effort cross-process
    # signal: the HOF bot drains this queue and edits just the affected boss
    # message within seconds instead of waiting for its periodic sweep.
    if pb_row_changed and not is_seasonal:
        try:
            import json as _json
            from utils.redis import redis_client as _rc
            _rc.client.rpush(
                "hof:refresh:queue",
                _json.dumps({"player_id": int(player_id), "npc_id": int(npc_id)}),
            )
        except Exception:
            pass

    # Site-wide ticker (rt:feed): announce a stored new PB when it lands in
    # the top 25 of its (boss, team-size) board. Best-effort — the rank query
    # only runs on actual PB improvements, never the drop hot path.
    if pb_row_changed and is_personal_best and not is_seasonal:
        try:
            from utils.pb_rank import pb_board_rank
            from services.realtime import publish_feed_personal_best

            ranked = pb_board_rank(session, npc_id, team_size, player_id)
            if ranked is not None and ranked[0] <= 25:
                publish_feed_personal_best(
                    player_id=player_id,
                    player_name=player.player_name or player_name,
                    npc_id=npc_id,
                    npc_name=npc_name,
                    time_ms=time_ms,
                    time_display=convert_from_ms(time_ms),
                    team_size=team_size,
                    rank=ranked[0],
                )
        except Exception as e:
            debug_print(f"Ticker PB publish failed: {e}")

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
                # used_api means "came from the plugin" to the events engine
                # (API and Discord-webhook intake both count; mirrors drop.py).
                used_api=envelope_from_plugin(pb_data),
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

        # Per-group manual policy (suggestion #45): suppress this group's
        # notification for a manual submission it doesn't trust (non-drop
        # types are notification-only — no leaderboard, no review queue).
        manual_suppressed = set()
        if pb_data.get("intake_source") == "manual" and not is_seasonal:
            try:
                from .manual_policy import manual_notification_suppressed_groups
                manual_suppressed = manual_notification_suppressed_groups(
                    session, player, group_ids
                )
            except Exception as e:
                print(f"[ManualPolicy] pb suppression check failed: {e}")

        for group in player_groups:
            await asyncio.sleep(0)
            group_id = group.group_id
            if group_id in manual_suppressed:
                continue
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
                    "plugin_version": plugin_version,
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
                        "plugin_version": plugin_version,
                    },
                    existing_session=session if use_external_session else None,
                )
        except Exception as e:
            print(f"Couldn't queue personal PB DM notification: {e}")
    debug_print(f"=== PB PROCESSOR END ===")
    return SubmissionResponse(success=True,
                            message="PB entry created/modified successfully.",
                            notice=notice)


