"""Drop submissions processor."""

import asyncio
import json
from datetime import datetime, timedelta
import time

from .common import (
    received_at,
    SubmissionResponse,
    ensure_item_for_drop,
    ensure_player_and_auth,
    ensure_npc_id_for_player,
    resolve_attachment_from_drop_data,
    get_player_groups_with_global,
    is_user_dm_enabled,
    screenshot_required,
    select_session_and_flag,
    create_notification,
    get_point_divisor,
    get_true_item_value,
    RedisClient,
    DatabaseOperations,
    debug_print,
    FeatureActivation,
    award_points_to_player,
    player_list,
    redis_updates,
    get_config_prefix,
    envelope_from_plugin,
    SEASONAL_WORLD_TYPE,
    SeasonalDrop,
)
from utils.valuation_guard import sanitize_quantity, exceeds_value_sanity_limit


redis_client = RedisClient()
db = DatabaseOperations()
last_board_updates = {}


def _verify_high_value_drop_sync(item_name, npc_name, timeout: float = 20.0) -> bool:
    """Verify a >1M drop's item/NPC pairing against the OSRS wiki, off the loop.

    Runs inside a worker thread with its own event loop (dispatched via
    ``asyncio.to_thread``) so the wiki lookup can never stall the caller's event
    loop. The webhook bot shares that loop with a systemd-watchdog health check;
    a slow/hanging wiki call here used to block the loop past the 30s health
    threshold, so the external monitor SIGTERM-restarted the service (~2x/hr).
    The osrs_api aiohttp client sets no request timeout, so we bound it here.

    Returns True when the item is a valid drop from the NPC, or when the check
    can't finish within ``timeout`` (fail-open — a slow wiki must not reject a
    legitimate high-value submission). Returns False only when the wiki
    definitively reports the pairing is invalid.
    """
    from .common import osrs_api

    async def _check() -> bool:
        async with osrs_api.create_client() as client:
            return await client.semantic.check_drop(item_name, npc_name)

    try:
        return asyncio.run(asyncio.wait_for(_check(), timeout=timeout))
    except asyncio.TimeoutError:
        print(f"[DropPerf] high_value_verification timed out after {timeout}s for "
              f"{item_name!r} from {npc_name!r}; allowing drop (fail-open)")
        return True
    except Exception as e:
        print(f"High value verification errored for {item_name!r} from "
              f"{npc_name!r}: {e}; allowing drop (fail-open)")
        return True


def _resolve_split_participants(session, players_included, receiver_player_id):
    """Submitted participant names -> the distinct non-receiver Player rows.

    The plugin submits the RSN exactly as the game spells it ("X-tra",
    "Beast_Owned") — it does no normalization — while WOM, our identity source,
    folds both "-" and "_" to spaces, so the row we stored is usually "x tra".
    Neither spelling is wrong and utf8mb4_general_ci does not treat them as
    equal, so resolution has to fold both separators in both directions. A
    lookup that only knows about underscores drops every hyphenated
    participant (X-tra, Tzuk-Kal-Lag, NoX-EvilAce, …): they resolve to nothing
    and silently miss their share of the split.

    Two filters ride along, both of which only matter once the folding above
    makes more names resolve:

    * the receiver is dropped by **id**, not name — they are counted separately
      (they anchor the divisor and take the receiver adjustment), and upstream
      only filters them out of the list by name, which cannot see through the
      spelling gap;
    * a payload naming one account twice ("X-tra" and "x tra") now resolves to
      the same row twice, and must still be credited once.
    """
    from db.ops import resolve_player_for_display

    participants = []
    seen_player_ids = set()
    for name in players_included or []:
        p = resolve_player_for_display(session, name)
        if p is None:
            continue
        try:
            if int(p.player_id) == int(receiver_player_id):
                continue
        except (TypeError, ValueError):
            pass
        if p.player_id in seen_player_ids:
            continue
        seen_player_ids.add(p.player_id)
        participants.append(p)
    return participants


async def _award_split_gp_credits(session, drop, group, receiver_player_id: int,
                                   players_included: list, drop_value: int,
                                   world_type: str = "main",
                                   split_size: int = None) -> int:
    """
    For groups with split_gp_tracking enabled, distribute group leaderboard GP
    credit equally among all split participants (including the receiver).

    - Non-receiver participants get split_value credited via add_split_credit().
    - The receiver's group leaderboard score is reduced by (full_value - split_value)
      so their net group credit equals split_value rather than the full drop value.
    - A DropSplit row is persisted for each non-receiver participant as the
      source-of-truth for Redis force-rebuilds.

    The global leaderboard and individual player:*:total_loot keys are never
    modified here.

    ``split_size`` is the number of people the drop was ACTUALLY split between,
    including the receiver — the real-world party size. It exists because the
    denominator and the credit list are different questions: a share taken by
    someone DropTracker can't credit (not tracked at all, tracked but not in this
    group, or deliberately left unnamed) still has to shrink everyone else's
    share. Without it the divisor collapsed to the people we happened to
    recognise, so a 4-way split with 3 tracked members paid out thirds and
    over-credited the whole group by a full share. Group-independent by design:
    the party size is a fact about the kill, not about any one group's roster.

    ``split_size`` only ever RAISES the divisor (``max`` against the people we
    actually resolved), so a stale or under-counted value can never inflate
    anyone's credit.

    Returns the number of non-receiver participants credited.
    """
    from db.models.drop_split import DropSplit
    from db.models import user_group_association

    group_id = group.group_id
    partition = drop.partition

    # Keep only the resolved participants who are members of this group — split
    # credit is a per-group statement, so an outsider's share is uncreditable
    # here (it still shrinks everyone's cut, via the divisor below).
    valid_participants = []
    for p in _resolve_split_participants(session, players_included, receiver_player_id):
        is_member = (
            session.query(user_group_association)
            .filter(
                user_group_association.c.player_id == p.player_id,
                user_group_association.c.group_id == group_id,
            )
            .first()
        )
        if is_member:
            valid_participants.append(p)

    # Total participants = receiver + everyone else who took a share. Prefer the
    # submitted party size, but never let it go below the people we resolved
    # (a bogus small split_size must not inflate credit).
    resolved_count = 1 + len(valid_participants)
    try:
        total_count = max(int(split_size or 0), resolved_count)
    except (TypeError, ValueError):
        total_count = resolved_count

    if total_count <= 1:
        return 0

    split_value = drop_value // total_count

    # Guard: nothing to do if split equals full value (1-way "split")
    if split_value >= drop_value:
        return 0

    # Uncreditable shares (untracked / out-of-group / unnamed) still shrink
    # everyone's cut, but there is nobody to hand them to — they are credited to
    # no one rather than being redistributed.
    uncredited = total_count - resolved_count
    if uncredited > 0:
        print(f"[SplitTracking] Drop {drop.drop_id}: {total_count}-way split, "
              f"{uncredited} share(s) not creditable (untracked or not in group "
              f"{group_id}); {split_value} gp each goes to nobody")

    if not valid_participants:
        # Every share went to someone this group can't credit. The receiver's
        # reduction is deliberately NOT applied: it is reconstructed on rebuild
        # from a DropSplit row's split_value (see redis_updates
        # ._apply_split_receiver_adjustments), so with no rows to anchor it the
        # reduction would be silently undone by the next force-rebuild. A
        # consistent no-op beats a reduction that quietly reverts itself.
        print(f"[SplitTracking] Drop {drop.drop_id}: no split participants are in "
              f"group {group_id} — leaving the receiver at full group credit "
              f"(nothing would persist a reduction).")
        return 0

    # 1. Adjust receiver's group leaderboard down from full_value to split_value
    receiver_adjustment = split_value - drop_value  # negative
    redis_updates.add_split_credit(receiver_player_id, receiver_adjustment, partition, group_id, world_type)

    # 2. Credit each non-receiver participant
    for p in valid_participants:
        # Persist the split record (skip if already exists to avoid duplicates)
        existing = (
            session.query(DropSplit)
            .filter(
                DropSplit.drop_id == drop.drop_id,
                DropSplit.player_id == p.player_id,
                DropSplit.group_id == group_id,
            )
            .first()
        )
        if existing is None:
            split_row = DropSplit(
                drop_id=drop.drop_id,
                player_id=p.player_id,
                group_id=group_id,
                split_value=split_value,
            )
            session.add(split_row)

        redis_updates.add_split_credit(p.player_id, split_value, partition, group_id, world_type)

    session.flush()
    return len(valid_participants)


def _strip_empty_sentinel(cleaned):
    """Drop the legacy "none" sentinel meaning no players were nearby.

    Plugins since ~5.3 (NearbyPlayerTracker) send "nearby_players" and simply
    omit the field when empty; the literal string "none" came from the legacy
    "members" field and is still stripped here for older clients.
    """
    if cleaned and len(cleaned) == 1 and cleaned[0].lower() == "none":
        return None
    return cleaned or None


def _normalize_incoming_players(players_included):
    """Normalize participant payloads from all drop ingestion paths."""
    if players_included is None:
        return None
    if isinstance(players_included, list):
        cleaned = [str(name).strip() for name in players_included if name and str(name).strip()]
        return _strip_empty_sentinel(cleaned)
    if isinstance(players_included, str):
        raw_text = players_included.strip()
        if not raw_text:
            return None
        if raw_text.startswith("["):
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, list):
                    cleaned = [str(name).strip() for name in parsed if name and str(name).strip()]
                    return _strip_empty_sentinel(cleaned)
            except Exception:
                pass
        cleaned = [part.strip() for part in raw_text.replace("\n", ",").split(",") if part.strip()]
        return _strip_empty_sentinel(cleaned)
    if isinstance(players_included, dict):
        cleaned = [str(name).strip() for name in players_included.values() if name and str(name).strip()]
        return _strip_empty_sentinel(cleaned)
    return None


# A split bigger than a raid team is a typo, not a party. Chambers of Xeric caps
# at 100 players, which is the largest group content in the game.
MAX_SPLIT_SIZE = 100


def _normalize_split_size(raw, players_included):
    """Coerce a submitted party size into a trustworthy divisor, or None.

    Returns None for anything absent, unparseable, or too small to matter — the
    caller then falls back to counting the participants it resolved. A value is
    only meaningful when it is at least (receiver + named participants); anything
    lower contradicts the names in the same payload, so it is ignored rather than
    used to shrink the divisor.
    """
    if raw is None or raw is True or raw is False:
        return None
    try:
        size = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if size < 2 or size > MAX_SPLIT_SIZE:
        return None
    named = len(players_included or [])
    return size if size >= named + 1 else None


async def drop_processor(drop_data, external_session=None, world_type="main"):
    """Process a drop submission and create notifications when appropriate."""

    debug_print(f"=== DROP PROCESSOR START (world_type={world_type}) ===")
    debug_print(f"Raw drop data: {drop_data}")
    debug_print(f"External session provided: {external_session is not None}")

    session, use_external_session = select_session_and_flag(external_session)
    debug_print(f"Using external session: {use_external_session}")

    # Set variables to be used in SubmissionResponse data
    notice = ""
    message = ""
    start_time = time.perf_counter()
    last_checkpoint = start_time
    CHECKPOINT_THRESHOLD = 0.2

    def log_checkpoint(label: str):
        nonlocal last_checkpoint
        now = time.perf_counter()
        delta = now - last_checkpoint
        total = now - start_time
        if delta >= CHECKPOINT_THRESHOLD:
            print(f"[DropPerf] {label} delta={delta:.3f}s total={total:.3f}s")
        last_checkpoint = now

    try:
        npc_name = drop_data.get("source", drop_data.get("npc_name", None))
        value = drop_data["value"]
        item_id = drop_data.get("item_id", drop_data.get("id", None))
        item_name = drop_data.get("item_name", drop_data.get("item", None))
        quantity = sanitize_quantity(drop_data.get("quantity"))
        if quantity is None:
            return SubmissionResponse(success=False, message="Invalid drop quantity")
        auth_key = drop_data.get("auth_key", None)
        player_name = drop_data.get("player_name", drop_data.get("player", None))
        account_hash = drop_data["acc_hash"]
        plugin_version = drop_data.get("p_v", None)
        # The RuneLite plugin sends this embed field as "killcount" (no
        # underscore) and uses 0 when the KC isn't available — treat both as
        # absent rather than letting 0 read as a real kill count.
        kill_count = drop_data.get("kill_count", drop_data.get("killcount", None))
        try:
            kill_count = int(kill_count)
        except (TypeError, ValueError):
            kill_count = None
        if kill_count is not None and kill_count <= 0:
            kill_count = None
        player_name = str(player_name).strip()
        account_hash = str(account_hash)
        guid = drop_data.get("guid", None)
        downloaded = drop_data.get("downloaded", False)
        image_url = drop_data.get("image_url", None)
        used_api = drop_data.get("used_api", False)
        # Intake path: 'manual' = website manual submit (/manual-submit). NOT
        # drop_data["source"] — that key is the NPC name (line below). Drives
        # per-group manual-submission policies (suggestion #45).
        intake_source = drop_data.get("intake_source") or None
        raw_players_included = drop_data.get("players_included", drop_data.get("nearby_players"))
        if raw_players_included is None:
            # Legacy fallback: pre-5.3 plugins sent the participant list as a
            # "members" embed field (comma-separated, literal "none" when
            # empty). Since ~5.3 (NearbyPlayerTracker, plugin commit 84356cc)
            # the plugin sends "nearby_players" — handled above — and omits it
            # entirely when nobody is nearby; see services/split_observer.py.
            raw_players_included = drop_data.get("members")
        players_included = _normalize_incoming_players(raw_players_included)
        # How many people the drop was ACTUALLY split between, receiver included.
        # Lets a submitter account for shares taken by people DropTracker can't
        # credit (untracked, not in the group, or left unnamed) so the divisor
        # matches the real party instead of collapsing to whoever we recognise.
        split_size = _normalize_split_size(drop_data.get("split_size"), players_included)
        debug_print(
            "Drop split payload players_included "
            f"raw_type={type(raw_players_included).__name__} raw_value={raw_players_included} "
            f"normalized_type={type(players_included).__name__} normalized_value={players_included}"
        )

        config_prefix = get_config_prefix(world_type)
        is_seasonal = world_type == SEASONAL_WORLD_TYPE
        dedup_type = "seasonal_drop" if is_seasonal else "drop"

        # dedupe via NotifiedSubmission cache in caller; keep local prevention via ensure_can_create
        from .common import ensure_can_create

        if not await ensure_can_create(session, guid, dedup_type):
            return

        debug_print(
            f"Extracted data - Player: {player_name}, Item: {item_name} (ID: {item_id}), NPC: {npc_name}"
        )
        debug_print(f"Value: {value}, Quantity: {quantity}, Used API: {used_api}")
        debug_print(f"Account hash: {account_hash[:8]}... (truncated), Kill count: {kill_count}")
        debug_print(f"Ensuring item exists for drop...")

        item = await ensure_item_for_drop(session, item_id, item_name)
        if not item:
            debug_print(f"Item {item_name} not found in database, aborting")
            return SubmissionResponse(success=False, message=f"Item was not found in the database")
        item_id = item.item_id
        debug_print(f"Item validated - ID: {item_id}, Name: {item_name}")
        log_checkpoint("ensure_item_for_drop")

        debug_print(f"Ensuring player and auth...")
        player, authed, user_exists = await ensure_player_and_auth(
            session, player_name, account_hash, auth_key
        )
        if not player:
            debug_print("Player not found in the database")
            return SubmissionResponse(success=False, message=f"Player {player_name} not found in the database")
        if not user_exists or not authed:
            debug_print(player_name + " failed auth check")
            return SubmissionResponse(success=False, message=f"Player {player_name} failed auth check")
        debug_print(
            f"Player validated - ID: {player.player_id}, Name: {player_name}, Authed: {authed}"
        )
        log_checkpoint("ensure_player_and_auth")

        debug_print(f"Ensuring NPC ID for {npc_name}...")
        npc_id, npc_name = await ensure_npc_id_for_player(
            session, npc_name, player.player_id, player_name, use_external_session
        )
        if npc_id is None:
            debug_print(f"NPC ID could not be resolved for {npc_name}, aborting")
            return SubmissionResponse(success=False, message=f"NPC ID could not be resolved for {npc_name}, aborting")
        debug_print(f"NPC validated - ID: {npc_id}, Name: {npc_name}")
        log_checkpoint("ensure_npc_id_for_player")

        player_id = player.player_id
        # "Item is known" cache. Namespaced + TTL'd: this used to be
        # `SET <item_id> <item_id>` with no expiry, which littered the
        # keyspace with thousands of permanent bare-number keys.
        item_known_key = f"item:known:{item_id}"
        item_cache = redis_client.get(item_known_key)
        if not item_cache:
            item_cache = session.query(type(item).item_id).filter(type(item).item_id == item_id).first()
        if item_cache:
            redis_client.client.set(item_known_key, "1", ex=7 * 24 * 3600)
        else:
            notification_data = {
                "item_name": item_name,
                "player_name": player_name,
                "item_id": item_id,
                "npc_name": npc_name,
                "value": value,
            }
            await create_notification(
                "new_item",
                player_id,
                notification_data,
                existing_session=session if use_external_session else None,
            )
            debug_print(f"Item not found... {item_id} {item_name}")
            return SubmissionResponse(
                success=False, message=f"Item {item_name} not found in the database"
            )

        debug_print(f"Calculating drop value...")
        raw_drop_value = await get_true_item_value(item_name, int(value), item_id=item_id)
        drop_value = int(raw_drop_value) * int(quantity)
        debug_print(
            f"Drop value calculated - Raw: {raw_drop_value}, Total: {drop_value} ({quantity}x)"
        )

        # Reject implausible totals (spoofed quantity on a valuable item).
        if exceeds_value_sanity_limit(raw_drop_value, quantity):
            print(
                f"[DropGuard] Rejected implausible drop total: {item_name} "
                f"unit={raw_drop_value} x qty={quantity} from {player_name}"
            )
            return SubmissionResponse(
                success=False,
                message="Drop value exceeds the plausible maximum and was rejected",
            )

        if drop_value > 1000000:
            debug_print(f"High value drop detected, verifying item/NPC combination...")
            # Offload the wiki lookup to a worker thread so a slow/hanging
            # verification can't block this event loop (and the systemd-watchdog
            # health check that shares it). See _verify_high_value_drop_sync.
            is_from_npc = await asyncio.to_thread(
                _verify_high_value_drop_sync, item_name, npc_name
            )
            if not is_from_npc:
                print(f"Verification failed: {item_name} is not from {npc_name}")
                return SubmissionResponse(
                    success=False, message=f"Item {item_name} is not from NPC {npc_name}"
                )
            debug_print(f"Item/NPC combination verified successfully")
        log_checkpoint("high_value_verification")

        debug_print(f"Processing attachment data...")
        attachment_url, attachment_type = resolve_attachment_from_drop_data(drop_data)
        debug_print(f"Attachment resolved - URL: {attachment_url}, Type: {attachment_type}")
        video_key = drop_data.get("video_key")

        debug_print(f"Creating drop object in database...")
        drop = await db.create_drop_object(
            item_id=item_id,
            player_id=player_id,
            # Server ACCEPT time, not worker-pickup time: a queue backlog must
            # not book a kill earned before midnight into the next month.
            date_received=received_at(drop_data),
            npc_id=npc_id,
            value=int(raw_drop_value),
            quantity=int(quantity),
            image_url=attachment_url if attachment_url else None,
            authed=authed,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
            used_api=used_api,
            unique_id=guid,
            existing_session=session if use_external_session else None,
            model_class=SeasonalDrop if is_seasonal else None,
            source=intake_source,
            kill_count=kill_count,
        )
        

        

        #debug_print(f"Drop created successfully - Drop ID: {drop.drop_id if drop else 'None'}")
        if not drop:
            debug_print("Failed to create drop")
            return SubmissionResponse(success=False, message=f"Failed to create drop")
        log_checkpoint("create_drop_object")

        # Per-group manual-submission policy (suggestion #45): a group may
        # withhold manual drops from ITS boards/notifications — permanently
        # ('excluded': block / authorized_only) or pending an admin's review
        # ('pending': confirm). Never affects global tracking or the player's
        # other groups. Recorded durably so board rebuilds re-apply it
        # (drop_group_moderation). Both statuses withhold at intake.
        manual_moderation = {}
        if intake_source == "manual" and not is_seasonal:
            try:
                from .manual_policy import manual_moderation_for_player, record_moderation
                manual_moderation = manual_moderation_for_player(
                    session, player, [g.group_id for g in (player.groups or [])]
                )
                if manual_moderation:
                    record_moderation(session, drop.drop_id, manual_moderation)
                    try:
                        if use_external_session:
                            session.flush()
                        else:
                            session.commit()
                    except Exception:
                        # A failed flush/commit leaves the session pending-
                        # rollback; clear it before the outer handler's
                        # "never fail the submission" continue, or every
                        # later commit on this session dies too.
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        raise
                    print(f"[ManualPolicy] Drop {drop.drop_id} withheld from groups "
                          f"{ {g: s for g, (s, _p) in manual_moderation.items()} } "
                          f"by manual_submission_policy")
                    # Ping each group's admin channel about drops now awaiting
                    # review (confirm policy). Best-effort; never fails intake.
                    try:
                        from .manual_policy import notify_pending_review
                        await notify_pending_review(
                            session, drop, player, item_name, npc_name, drop_value,
                            [g for g, (s, _p) in manual_moderation.items() if s == "pending"],
                            use_external_session=use_external_session,
                        )
                    except Exception as ne:
                        print(f"[ManualPolicy] Couldn't queue pending-review ping: {ne}")
            except Exception as e:
                # A policy failure must never fail the submission; the drop
                # counts everywhere (legacy behavior) rather than nowhere.
                manual_moderation = {}
                print(f"[ManualPolicy] Policy check failed for drop {getattr(drop, 'drop_id', '?')}: {e}")
        excluded_group_ids = set(manual_moderation)
        log_checkpoint("manual_policy")

        try:
            debug_print("Updating player in redis...")
            redis_updates.add_to_player(
                player, drop, world_type=world_type, item_name=item_name, npc_name=npc_name,
                exclude_group_ids=excluded_group_ids,
            )
            debug_print("Player redis update completed")
        except Exception as e:
            print(f"[RedisSync] Failed to update player {player_id} in redis for drop {getattr(drop, 'drop_id', '?')}: {e}")
        log_checkpoint("redis_add_to_player")

        # Event engine hook (Task 17): one gated, fire-and-forget LPUSH.
        # No DB queries; a Redis hiccup can never fail the submission.
        try:
            from services.event_engine import queue_submission
            queue_submission(
                "drop", player_id, guid,
                {
                    "item_id": item_id,
                    "item_name": item_name,
                    "npc_id": npc_id,
                    "npc_name": npc_name,
                    "value": int(raw_drop_value),
                    "quantity": int(quantity),
                    "total_value": int(drop_value),
                    "kill_count": kill_count,
                    "image_url": drop.image_url,
                    "source_id": getattr(drop, "drop_id", None),
                    "plugin_version": plugin_version,
                },
                world_type=world_type, player_name=player_name,
                # The envelope's used_api means "came from the plugin" to the
                # events engine (submission_policy confirm_non_api/api_only) —
                # independent of the Drop row's used_api column, which records
                # the intake transport (API vs Discord webhook).
                used_api=envelope_from_plugin(drop_data),
            )
        except Exception:
            pass

        # TEMP (2026-08) split-source observation: records which sources arrive
        # with nearby players so we can pick where split tracking belongs.
        # Fail-open; remove this call with services/split_observer.py once the
        # split-source list is settled.
        try:
            if world_type == "main" and envelope_from_plugin(drop_data):
                from services.split_observer import record_drop
                record_drop(
                    npc_id=npc_id,
                    npc_name=npc_name,
                    player_name=player_name,
                    players=players_included,
                    plugin_version=plugin_version,
                    guid=guid,
                )
        except Exception:
            pass

        debug_print(f"Getting player groups for {player_name}...")
        player_groups = get_player_groups_with_global(session, player)
        log_checkpoint("get_player_groups")
        debug_print(
            f"Player groups found: {[group.group_name for group in player_groups]}"
        )
        group_ids = [group.group_id for group in player_groups]
        group_config_values = {}
        instant_update_group_ids = set()
        if group_ids:
            from utils import group_config as gc
            group_config_values = gc.get_bulk(
                session,
                group_ids,
                [
                    f"{config_prefix}minimum_value_to_notify",
                    f"{config_prefix}send_stacks_of_items",
                    "split_gp_tracking",
                ],
            )
            debug_print(
                "Loaded group notification configs: "
                + ", ".join(
                    f"group_id={gid} {key}={val}"
                    for (gid, key), val in group_config_values.items()
                )
                if group_config_values
                else "Loaded group notification configs: none"
            )

            instant_update_rows = (
                session.query(FeatureActivation.group_id)
                .filter(
                    FeatureActivation.group_id.in_(group_ids),
                    FeatureActivation.feature_id == 2,
                    FeatureActivation.status == "active",
                )
                .all()
            )
            instant_update_group_ids = {row[0] for row in instant_update_rows}

        log_checkpoint("group_config_queries")

        # Split-source policy: whether this NPC is a place we track splits at
        # all. Group-independent (a fact about the kill, not about a roster), so
        # it is resolved once here rather than per group. Ships in shadow mode:
        # allows_split() only ever returns False once an admin sets the policy
        # to "enforce" — until then it just counts what enforcement would stop.
        split_source_allowed = True
        try:
            from utils import split_policy

            split_source_allowed = split_policy.allows_split(
                npc_id, npc_name=npc_name, session=session
            )
        except Exception as e:
            # A policy read must never cost a submission its splits.
            print(f"[SplitPolicy] Eligibility check failed for npc_id={npc_id}: {e}")
        log_checkpoint("split_policy")

        sent_group_notifications = []
        debug_print(f"Processing notifications for {len(player_groups)} groups...")
        has_awarded_points = False
        for group in player_groups:
            await asyncio.sleep(0)
            group_id = group.group_id
            if group_id in excluded_group_ids:
                # manual_submission_policy withheld this drop from this group:
                # no notification, no group points, no split credits.
                debug_print(f"Group {group_id} excluded by manual_submission_policy - skipping")
                continue
            group_points_result = {
                "receiver_points_awarded": 0,
                "receiver_current_points": 0,
                "total_points_awarded": 0,
                "awarded_members": [],
            }
            # print(f"Processing group: {group.group_name} (ID: {group_id})")
            """ Here we can process the drop to determine if it needs to be calculated for a point award (group-specific points) """

            if is_seasonal:
                # TODO: award group points for seasonal drops once award_points_in_leagues
                # config key is supported. Check group config for "award_points_in_leagues"
                # before calling check_and_award_points with the seasonal entry_id.
                pass
            else:
                async def perform_point_check():
                    nonlocal has_awarded_points, group_points_result
                    from .common import check_group_point_system_active
                    point_system_active = check_group_point_system_active(group_id, session)
                    debug_print(
                        f"Group {group_id} point check start: active={point_system_active}, "
                        f"players_included={players_included}, item_id={item_id}, npc_id={npc_id}, "
                        f"value={int(drop.value) * int(drop.quantity)}, quantity={int(drop.quantity)}"
                    )
                    if point_system_active:
                        from .point_awards import check_and_award_points
                        group_points_result = await check_and_award_points(
                            "drop",
                            group_id,
                            player_id,
                            int(drop.value) * int(drop.quantity),
                            players_included=players_included,
                            item_id=item_id,
                            npc_id=npc_id,
                            quantity=int(drop.quantity),
                            entry_id=getattr(drop, "drop_id", None),
                            submission_guid=guid,
                            submission_timestamp=drop_data.get("timestamp"),
                            external_session=session,
                        )
                        debug_print(
                            f"Group {group_id} point check result: receiver_points_awarded="
                            f"{group_points_result.get('receiver_points_awarded', 0)} "
                            f"receiver_current_points={group_points_result.get('receiver_current_points', 0)} "
                            f"total_points_awarded={group_points_result.get('total_points_awarded', 0)} "
                            f"awarded_members={group_points_result.get('awarded_members', [])}"
                        )
                        if int(group_points_result.get("total_points_awarded", 0)) > 0:
                            has_awarded_points = True
                    else:
                        pass # print("Group does not have custom point system active")

                try:
                    await perform_point_check()
                except Exception as e:
                    print(f"Couldn't perform check against group point awards... e: {e}")
                    pass

            # --- Split GP tracking ---
            split_tracking_enabled = (
                group_config_values.get((group_id, "split_gp_tracking")) == "1"
            )
            if split_tracking_enabled and (players_included or split_size) and not is_seasonal:
                # A split really was about to run here: count it against the
                # source policy (per group, since split credit is per group)
                # before deciding whether the policy lets it through.
                try:
                    from utils import split_policy

                    split_policy.record_split_event(npc_id, npc_name, session=session)
                except Exception:
                    pass
                if not split_source_allowed:
                    print(f"[SplitPolicy] Skipped split for group {group_id}: {npc_name} "
                          f"(npc_id={npc_id}) is not a split-eligible source "
                          f"(drop_id={getattr(drop, 'drop_id', '?')})")
                else:
                    try:
                        credited = await _award_split_gp_credits(
                            session=session,
                            drop=drop,
                            group=group,
                            receiver_player_id=player_id,
                            players_included=players_included or [],
                            drop_value=drop_value,
                            world_type=world_type,
                            split_size=split_size,
                        )
                        if credited:
                            print(f"[SplitTracking] Split {drop_value} gp across {credited + 1} participants "
                                  f"for group {group_id} (drop_id={drop.drop_id}, receiver={player_id})")
                    except Exception as e:
                        print(f"[SplitTracking] Failed to apply split credits for group {group_id}: {e}")

            min_value_raw = group_config_values.get((group_id, f"{config_prefix}minimum_value_to_notify"))
            try:
                min_value_to_notify = int(min_value_raw) if min_value_raw is not None else 2500000
            except (TypeError, ValueError):
                min_value_to_notify = 2500000
            debug_print(f"Group {group_id} minimum value to notify: {min_value_to_notify}")

            send_stacks_value = group_config_values.get((group_id, f"{config_prefix}send_stacks_of_items"))
            send_stacks = str(send_stacks_value).lower() in ("1", "true") if send_stacks_value is not None else False
            debug_print(
                f"Group {group_id} config snapshot: minimum_value_to_notify_raw={min_value_raw}, "
                f"minimum_value_to_notify={min_value_to_notify}, send_stacks_of_items_raw={send_stacks_value}, "
                f"send_stacks_of_items={send_stacks}"
            )

            debug_print(
                f"Checking notification criteria - Raw value: {raw_drop_value}, Drop value: {drop_value}, Send stacks: {send_stacks}"
            )
            group_points_awarded = int(group_points_result.get("receiver_points_awarded", 0))
            group_has_awarded_points = int(group_points_result.get("total_points_awarded", 0)) > 0
            awarded_members = group_points_result.get("awarded_members", []) or []
            if int(raw_drop_value) >= min_value_to_notify or (
                send_stacks == True and int(drop_value) > min_value_to_notify
            ):
                if await screenshot_required(session, group_id):
                    # Treat video submissions as satisfying screenshot requirement
                    if not drop.image_url and not video_key:
                        notice = f"Your {item_name} submission did not include a screenshot (required for {group.group_name}). Please enable screenshots in the DropTracker plugin configuration to accurately share your achievements!"
                        continue ## Skipping this group in the loop; as there is no image despite the group's requirement of one

                debug_print(f"Notification criteria met for group {group_id}")
                if not is_seasonal:
                    point_divisor = get_point_divisor()
                    if group_id != 2 and has_awarded_points == False and int(drop_value) > point_divisor:
                        print(
                            f"Awarding points to {player_name} for drop {item_name} from {npc_name}"
                        )
                        has_awarded_points = True
                        points_to_award = int(drop_value / point_divisor)
                        # Global points award (not group-scoped)
                        award_points_to_player(
                            player_id=player_id,
                            amount=points_to_award,
                            source=f"Drop: {item_name} from {npc_name}",
                            expires_in_days=60,
                        )
                notification_data = {
                    "drop_id": drop.drop_id,
                    "guid": guid,
                    "item_name": item_name,
                    "npc_name": npc_name,
                    "value": value,
                    "quantity": quantity,
                    "total_value": drop_value,
                    "kill_count": kill_count,
                    "player_name": player_name,
                    "player_id": player_id,
                    "image_url": drop.image_url,
                    "video_key": video_key,
                    "attachment_type": attachment_type,
                    "points_awarded": group_points_awarded,
                    "has_awarded_points": group_has_awarded_points,
                    "group_points_awarded": group_points_awarded,
                    "group_points_receiver_total": int(group_points_result.get("receiver_current_points", 0)),
                    "group_points_member_count": len(awarded_members),
                    "group_points_members_awarded": awarded_members,
                    "world_type": world_type,
                    "plugin_version": plugin_version,
                }
                if group_id > 2:
                    sent_group_notifications.append(group.group_name)
                    debug_print(f"Added {group.group_name} to notification list")

                print(f"Creating group notification for {player_name} in group {group_id}")
                await create_notification(
                    "drop",
                    player_id,
                    notification_data,
                    group_id,
                    existing_session=session if use_external_session else None,
                )
                should_instantly_update = group_id in instant_update_group_ids
                if group_id == 2 or should_instantly_update:
                    if group_id not in last_board_updates:
                        last_board_updates[group_id] = datetime.now() - timedelta(seconds=10)
                    if last_board_updates[group_id] > datetime.now() - timedelta(seconds=10):
                        debug_print(
                            f"Skipping group {group_id}: within 10 second window for instant update"
                        )
                        continue
                    last_board_updates[group_id] = datetime.now()
            else:
                debug_print(
                    f"Notification criteria NOT met for group {group_id} - skipping"
                )
        # --- Personal submission DM (supporter perk) -----------------------
        # Queued ONCE per drop, OUTSIDE the group loop: the user's own
        # dm_min_value is the only value filter. Group notification criteria
        # (minimum_value_to_notify, screenshot requirements) must not gate a
        # personal DM — that coupling silently dropped DMs for low-value
        # drops. Entitlement + opt-in are checked in is_user_dm_enabled and
        # re-checked at send time.
        try:
            if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_drops"):
                from db.models import UserConfiguration

                min_dm_raw = (
                    session.query(UserConfiguration.config_value)
                    .filter(
                        UserConfiguration.user_id == player.user_id,
                        UserConfiguration.config_key == "dm_min_value",
                    )
                    .scalar()
                )
                try:
                    min_dm_value = int(min_dm_raw) if min_dm_raw else 0
                except (TypeError, ValueError):
                    min_dm_value = 0
                if int(drop_value) >= min_dm_value:
                    debug_print(f"Creating personal DM notification for user {player.user_id}")
                    await create_notification(
                        "dm_drop",
                        player_id,
                        {
                            "drop_id": drop.drop_id,
                            "guid": guid,
                            "item_name": item_name,
                            "npc_name": npc_name,
                            "value": value,
                            "quantity": quantity,
                            "total_value": drop_value,
                            "kill_count": kill_count,
                            "player_name": player_name,
                            "player_id": player_id,
                            "image_url": drop.image_url,
                            "video_key": video_key,
                            "attachment_type": attachment_type,
                            "world_type": world_type,
                            "plugin_version": plugin_version,
                        },
                        existing_session=session if use_external_session else None,
                    )
        except Exception as e:
            print(f"Couldn't queue personal DM notification: {e}")

        if not use_external_session:
            debug_print(f"Committing session (we own it)")
            session.commit()
        else:
            debug_print(f"Not committing session (external session)")

        debug_print(f"Drop processor completed for {player_name}")
        if sent_group_notifications != []:
            if len(sent_group_notifications) == 1:
                group_name = sent_group_notifications[0]
            else:
                group_name = {", ".join(sent_group_notifications)}
            debug_print(
                f"Returning success with group notifications: {group_name}"
            )
            debug_print(f"=== DROP PROCESSOR END (SUCCESS) ===")
            return SubmissionResponse(
                success=True,
                message=f"Drop created successfully",
                notice=notice if notice != "" else f"Drop processed - a message has been sent to {group_name} for you",
            )
        else:
            debug_print(f"Returning success without group notifications")
            debug_print(f"=== DROP PROCESSOR END (SUCCESS) ===")
            return SubmissionResponse(success=True, message=f"Drop created successfully")

    except Exception as e:
        log_checkpoint("exception_handling")
        if not use_external_session:
            debug_print(f"Exception occurred, rolling back session: {e}")
            session.rollback()
        else:
            debug_print(f"Exception occurred with external session: {e}")
        debug_print(f"Error in drop_processor: {e}")
        debug_print(f"=== DROP PROCESSOR END (ERROR) ===")
        raise
    finally:
        total_elapsed = time.perf_counter() - start_time
        player_label = locals().get("player_name", "unknown") or "unknown"
        item_label = locals().get("item_name", "unknown") or "unknown"
        #print(f"[DropPerf] total processing time {total_elapsed:.3f}s for player {player_label} item {item_label}")


