"""Group-specific point awards for in-group achievements.

These points are separate from the global premium credit system in `services.points`.
They are stored in `player_points` with a `group_id` so per-group balances can be computed
without affecting global credits used for premium features.
"""

import json
import os
from datetime import datetime

from db import PlayerPoints, GroupPointConfig
from db.models import (
    GroupConfiguration,
    GroupPointMods,
    GroupPointTimedEvent,
    GroupPointBlacklist,
    Player,
    user_group_association,
)
import time

# player_points.reason column width (String(125) in db/models/group_points.py)
_PLAYER_POINTS_REASON_MAX_LEN = PlayerPoints.reason.type.length


class PointDebugSettings:
    # ~12 log lines per drop for premium groups; enable via POINT_DEBUG=true
    # (or flip at runtime) only when investigating point/split behavior.
    enabled = os.getenv("POINT_DEBUG", "").lower() in ("true", "1", "yes")


def _point_debug(message: str):
    """Uniform debug output for point/split investigation."""
    if not PointDebugSettings.enabled:
        return
    print(f"[PointDebug] {message}")


def _persist(session, is_external):
    """Flush when using caller-managed transaction, commit when standalone."""
    if is_external:
        session.flush()
    else:
        session.commit()


def _floor_div(value: int, divisor: int) -> int:
    """Integer floor division with guards for point thresholding.

    This intentionally does NOT round up. A value below the divisor should
    award zero points for GP-threshold based rules.
    """
    if divisor <= 0:
        return 0
    if value <= 0:
        return 0
    return value // divisor


def _round_half_up_div(value: int, divisor: int) -> int:
    """Integer division with .5 rounded up for GP-threshold awards."""
    if divisor <= 0:
        return 0
    if value <= 0:
        return 0
    return (value + (divisor // 2)) // divisor


def _compute_split_shares(point_award, target_count, split_method):
    """Divide a point award between the receiver and ``target_count`` other
    in-group participants.

    Returns ``(per_target_share, receiver_share)``.

    - ``equal_split`` / ``equal``: floor-divide the award across every
      participant (receiver + targets). Each target gets the floor share and the
      receiver additionally keeps the indivisible remainder, so the distributed
      total (``per_target_share * target_count + receiver_share``) always equals
      the original award and never exceeds it.
    - ``award_all``: every participant, receiver included, receives the full award.
    - any other/disabled method: targets get nothing; the receiver keeps the
      full award.

    A non-positive award or ``target_count`` yields no split (targets get 0, the
    receiver keeps the whole award).
    """
    total_award = int(point_award) if point_award and int(point_award) > 0 else 0
    try:
        targets = int(target_count)
    except Exception:
        targets = 0
    if targets <= 0:
        return 0, total_award
    if split_method in ("equal_split", "equal"):
        total_participants = targets + 1
        per_person = _floor_div(total_award, total_participants)
        remainder = total_award - per_person * total_participants
        return per_person, per_person + remainder
    if split_method == "award_all":
        return total_award, total_award
    return 0, total_award


def _normalize_unix_seconds(value):
    """Normalize mixed timestamp inputs to unix seconds.

    Supports:
    - seconds (int/float/str)
    - milliseconds / microseconds / nanoseconds (auto-scaled down)
    - ISO-8601 datetime strings
    """
    if value in (None, ""):
        return None

    # Fast-path numeric inputs/strings first.
    try:
        as_float = float(value)
        if as_float <= 0:
            return None
        ts = int(as_float)

        # Normalize common high-precision epochs.
        if ts >= 1_000_000_000_000_000_000:  # ns
            ts = ts // 1_000_000_000
        elif ts >= 1_000_000_000_000_000:  # us
            ts = ts // 1_000_000
        elif ts >= 1_000_000_000_000:  # ms
            ts = ts // 1_000
        return ts
    except Exception:
        pass

    # Fallback: parse ISO datetime strings.
    try:
        as_str = str(value).strip()
        if as_str.endswith("Z"):
            as_str = as_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(as_str)
        return int(dt.timestamp())
    except Exception:
        return None


async def modify_for_event(
    reason,
    group_id,
    player_id,
    default_value,
    item_id=None,
    npc_id=None,
    submission_timestamp=None,
    external_session=None,
) -> int:
    from data.submissions.common import select_session_and_flag
    session, _ = select_session_and_flag(external_session)
    effective_timestamp = _normalize_unix_seconds(submission_timestamp) or int(time.time())

    # Deterministic ordering: newest events win (first match returns).
    # We evaluate window applicability in Python so legacy rows with ms/us/ns
    # values still match after normalization.
    timed_events = (
        session.query(GroupPointTimedEvent)
        .filter(GroupPointTimedEvent.group_id == group_id)
        .order_by(GroupPointTimedEvent.id.desc())
        .all()
    )

    if not timed_events:
        return default_value

    for event in timed_events:
        event_start = _normalize_unix_seconds(getattr(event, "start_time_unix", None))
        event_end = _normalize_unix_seconds(getattr(event, "end_time_unix", None))
        if event_start is None or event_end is None:
            continue
        if event_end < event_start:
            continue
        if not (event_start <= effective_timestamp <= event_end):
            continue

        # Optional filter by reason/event type
        try:
            event_type = (event.event_type or "any").lower()
        except Exception:
            event_type = "any"
        if event_type not in ("any", str(reason).lower()):
            continue

        # Target matching
        try:
            target_type = (event.target_type or "any").lower()
        except Exception:
            target_type = "any"

        try:
            target_id = int(event.target_id) if event.target_id not in (None, "") else 0
        except Exception:
            target_id = 0

        applies = False
        if target_type == "any":
            applies = True
        elif target_type == "item":
            if item_id is not None:
                applies = (target_id == 0) or (target_id == int(item_id))
        elif target_type == "npc":
            if npc_id is not None:
                applies = (target_id == 0) or (target_id == int(npc_id))
        else:
            # Unknown target type; ignore the event
            continue

        if not applies:
            continue

        # Apply operation to computed award
        try:
            operation = (event.operation or "multiply").lower()
        except Exception:
            operation = "multiply"
        try:
            op_value = int(event.operation_value)
        except Exception:
            op_value = 1

        if operation == "multiply":
            return int(default_value) * op_value
        if operation == "add":
            return int(default_value) + op_value
        if operation == "set":
            return int(op_value)

    return default_value

async def get_group_point_stack_config(group_id, external_session=None):
    from data.submissions.common import select_session_and_flag
    session, use_ext = select_session_and_flag(external_session)
    group_point_stack_config = (
        session.query(GroupConfiguration)
        .filter(GroupConfiguration.group_id == group_id, GroupConfiguration.config_key == "stacks_award_points")
        .first()
    )

    if group_point_stack_config and group_point_stack_config.config_value is not None:
        try:
            return int(group_point_stack_config.config_value) == 1
        except Exception:
            return False

    default_config = (
        session.query(GroupConfiguration)
        .filter(GroupConfiguration.group_id == 1, GroupConfiguration.config_key == "stacks_award_points")
        .first()
    )

    default_val = 0
    if default_config and default_config.config_value is not None:
        try:
            default_val = 1 if int(default_config.config_value) == 1 else 0
        except Exception:
            default_val = 0

    try:
        if group_point_stack_config:
            group_point_stack_config.config_value = str(default_val)
        else:
            session.add(
                GroupConfiguration(
                    group_id=group_id,
                    config_key="stacks_award_points",
                    config_value=str(default_val),
                )
            )
        _persist(session, use_ext)
    except Exception:
        return default_val == 1

    return default_val == 1

async def check_and_award_points(
    reason,
    group_id,
    player_id,
    value,
    players_included=None,
    item_id=None,
    npc_id=None,
    quantity=None,
    entry_id=None,
    submission_guid=None,
    submission_timestamp=None,
    *,
    external_session=None,
):
    """Run the point-award pass isolated from the caller's transaction.

    Submission processors pass the session that still holds the uncommitted
    submission row (Drop, PB, clog, ...). Every write in the point pass
    therefore runs inside a SAVEPOINT: on any failure we roll back to the
    savepoint and return an empty result, so a bad point insert can never
    poison the session and take the submission down with it.
    """
    try:
        receiver_player_id = int(player_id)
    except Exception:
        receiver_player_id = player_id
    empty_result = {
        "group_id": int(group_id),
        "receiver_player_id": receiver_player_id,
        "receiver_points_awarded": 0,
        "receiver_current_points": 0,
        "total_points_awarded": 0,
        "awarded_members": [],
    }

    try:
        if external_session is None:
            return await _check_and_award_points(
                reason, group_id, player_id, value,
                players_included=players_included, item_id=item_id, npc_id=npc_id,
                quantity=quantity, entry_id=entry_id, submission_guid=submission_guid,
                submission_timestamp=submission_timestamp, external_session=None,
            )

        savepoint = external_session.begin_nested()
        try:
            result = await _check_and_award_points(
                reason, group_id, player_id, value,
                players_included=players_included, item_id=item_id, npc_id=npc_id,
                quantity=quantity, entry_id=entry_id, submission_guid=submission_guid,
                submission_timestamp=submission_timestamp, external_session=external_session,
            )
            savepoint.commit()
            return result
        except Exception:
            try:
                savepoint.rollback()
            except Exception:
                pass
            raise
    except Exception as e:
        print(
            f"Point award pass failed for group {group_id} "
            f"(guid={submission_guid}, entry_id={entry_id}): {e}"
        )
        return empty_result


async def _check_and_award_points(
    reason,
    group_id,
    player_id,
    value,
    players_included=None,
    item_id=None,
    npc_id=None,
    quantity=None,
    entry_id=None,
    submission_guid=None,
    submission_timestamp=None,
    *,
    external_session=None,
):
    from data.submissions.common import check_group_point_system_active, select_session_and_flag

    session, _ = select_session_and_flag(external_session)
    try:
        receiver_player_id = int(player_id)
    except Exception:
        receiver_player_id = player_id

    result = {
        "group_id": int(group_id),
        "receiver_player_id": receiver_player_id,
        "receiver_points_awarded": 0,
        "receiver_current_points": 0,
        "total_points_awarded": 0,
        "awarded_members": [],
    }
    debug_context = (
        f"guid={submission_guid or 'unknown'} "
        f"entry_id={entry_id if entry_id not in (None, '') else 'unknown'} "
        f"group_id={group_id}"
    )

    def point_log(message: str):
        _point_debug(f"{debug_context} {message}")

    point_log(
        "start "
        f"reason={reason} receiver_player_id={receiver_player_id} "
        f"value={value} quantity={quantity} item_id={item_id} npc_id={npc_id} "
        f"raw_players_included={players_included}"
    )

    def _current_total_for(target_player_id) -> int:
        try:
            total = 0
            rows = (
                session.query(PlayerPoints.amount)
                .filter(
                    PlayerPoints.group_id == int(group_id),
                    PlayerPoints.player_id == int(target_player_id),
                )
                .all()
            )
            for row in rows:
                amount = _extract_scalar(row)
                try:
                    total += int(amount)
                except Exception:
                    continue
            return int(total)
        except Exception:
            return 0

    def _record_award(target_player_id, target_player_name, awarded_points):
        try:
            awarded_int = int(awarded_points)
        except Exception:
            awarded_int = 0
        if awarded_int <= 0:
            return
        current_total = _current_total_for(target_player_id)
        result["total_points_awarded"] += awarded_int
        try:
            target_id_int = int(target_player_id)
        except Exception:
            target_id_int = target_player_id
        try:
            receiver_id_int = int(receiver_player_id)
        except Exception:
            receiver_id_int = receiver_player_id
        if target_id_int == receiver_id_int:
            result["receiver_points_awarded"] += awarded_int
            result["receiver_current_points"] = current_total
        result["awarded_members"].append(
            {
                "player_id": target_id_int,
                "player_name": target_player_name,
                "points_awarded": awarded_int,
                "current_points": current_total,
            }
        )

    if not check_group_point_system_active(group_id, external_session):
        point_log("point system inactive; skipping")
        return result
    group_config = await get_point_config(group_id, external_session)
    rawaward = group_config.get(reason)
    point_log(f"point_config[{reason}]={rawaward}")
    if not rawaward:
        # No configured award for this reason
        point_log(f"no config for reason={reason}; skipping")
        return result

    # Normalize quantity once (drops may be stacked)
    try:
        qty = int(quantity) if quantity not in (None, "") else None
        if qty is not None and qty <= 0:
            qty = None
    except Exception:
        qty = None

    # Parse default "award,divisor" config (divisor may be omitted for non-drop reasons)
    try:
        parts = [p.strip() for p in str(rawaward).split(",") if p.strip() != ""]
        default_award = int(parts[0])
        default_divisor = int(parts[1]) if len(parts) > 1 else 1
    except Exception:
        # If config is malformed, fail closed (no points) rather than throwing
        point_log(f"malformed config rawaward={rawaward}; skipping")
        return result
    point_log(
        f"parsed_config default_award={default_award} default_divisor={default_divisor}"
    )

    award = default_award
    divisor = default_divisor if default_divisor != 0 else 1
    has_mod_override = False

    def _norm_id(raw) -> int:
        """Normalize ids so None/''/0 become 0, otherwise int(value)."""
        try:
            if raw in (None, "", 0, "0"):
                return 0
            return int(raw)
        except Exception:
            return 0

    incoming_item_id = _norm_id(item_id)
    incoming_npc_id = _norm_id(npc_id)

    # Optional group-level include/exclude list gate.
    # - blacklist match => deny immediately
    # - if any whitelist entries exist, at least one must match
    # - no_split match => award only receiver (skip shared awards)
    allow_award, force_no_split = await evaluate_point_list_rules(
        group_id=group_id,
        item_id=incoming_item_id,
        npc_id=incoming_npc_id,
        external_session=external_session,
    )
    point_log(
        f"point_list_rules allow_award={allow_award} force_no_split={force_no_split} "
        f"incoming_item_id={incoming_item_id} incoming_npc_id={incoming_npc_id}"
    )
    if not allow_award:
        return result

    # Global split-source policy: where we don't track splits, shared point
    # awards collapse to the receiver — the same effect as a group's own
    # no_split rule, so it reuses that tested path rather than a new one.
    # Inert unless an admin has set the policy to "enforce" (utils/split_policy).
    if not force_no_split and incoming_npc_id:
        try:
            from utils import split_policy

            if not split_policy.allows_split(incoming_npc_id, session=external_session):
                force_no_split = True
                point_log(f"split_policy forced no_split for npc_id={incoming_npc_id}")
        except Exception:
            pass

    # Optional per-item/per-npc override
    group_point_mods = await get_group_point_mods(group_id, external_session)
    if group_point_mods:
        for mod in group_point_mods:
            mod_item_id = _norm_id(getattr(mod, "item_id", None))
            mod_npc_id = _norm_id(getattr(mod, "npc_id", None))

            # Mod ids act like optional filters:
            # - If mod specifies an id, the incoming event must provide the same id.
            # - If mod does NOT specify an id (NULL/0), it matches any incoming id.
            item_match = (mod_item_id == 0) or (incoming_item_id != 0 and mod_item_id == incoming_item_id)
            npc_match = (mod_npc_id == 0) or (incoming_npc_id != 0 and mod_npc_id == incoming_npc_id)
            mod_event_type = str(getattr(mod, "event_type", "") or "any").lower()
            if item_match and npc_match and mod_event_type in (str(reason).lower(), "any"):
                try:
                    award = int(mod.award)
                    has_mod_override = True
                except Exception:
                    award = default_award
                try:
                    divisor = int(mod.divisor) if int(mod.divisor) != 0 else 1
                except Exception:
                    divisor = default_divisor if default_divisor != 0 else 1
                point_log(
                    f"matched_mod item_id={mod_item_id} npc_id={mod_npc_id} "
                    f"award={award} divisor={divisor}"
                )
                break

    # Calculate point award
    if has_mod_override and qty is not None and qty > 0:
        # Mod override: award per item in the stack
        point_award = int(award) * int(qty)
    elif has_mod_override:
        # Mod override but no quantity: use divisor formula or flat award
        if reason == "drop" and divisor > 1:
            point_award = _round_half_up_div(int(value), int(divisor)) * int(award)
        else:
            point_award = int(award)
    elif reason == "drop":
        # Default drop formula: GP-value based.
        # When stacked drops are disabled, we compute per-item awards so high-value items
        # still award points, but "remainder pooling" from stacking does not.
        try:
            total_value = int(value)
            div = int(divisor) if int(divisor) != 0 else 1
            if qty is not None and qty > 1:
                stacks_award_points = await get_group_point_stack_config(group_id, external_session)
                if not stacks_award_points:
                    per_item_value = total_value // int(qty)
                    per_item_points = _round_half_up_div(int(per_item_value), int(div)) * int(award)
                    point_award = per_item_points * int(qty)
                else:
                    point_award = _round_half_up_div(int(total_value), int(div)) * int(award)
            else:
                point_award = _round_half_up_div(int(total_value), int(div)) * int(award)
        except Exception:
            point_award = 0
    else:
        # Other event types: flat award
        point_award = int(award)
    point_log(
        f"pre_split_point_award={point_award} "
        f"has_mod_override={has_mod_override} award={award} divisor={divisor} qty={qty}"
    )

    receiver_player_name = _get_player_name_by_id(receiver_player_id, session)
    included_player_names = _normalize_player_names(players_included, receiver_player_name)
    point_log(
        f"receiver_player_name={receiver_player_name} "
        f"normalized_players_included={included_player_names} force_no_split={force_no_split}"
    )

    # Optional group-only eligibility gate:
    # When enabled, points are awarded only for solo content or for group content
    # where at least one additional listed participant is in this same group.
    require_group_only = await check_points_require_group_only_mode(group_id, external_session)
    point_log(f"require_group_only={require_group_only}")
    if require_group_only and included_player_names:
        has_in_group_participant = False
        for participant_name in included_player_names:
            participant_id = _get_player_id_by_name(participant_name, session)
            if participant_id is None:
                point_log(
                    f"require_group_only participant='{participant_name}' not found in DB"
                )
                continue
            if await check_split_capability(group_id, participant_id, external_session):
                has_in_group_participant = True
                point_log(
                    f"require_group_only participant='{participant_name}' id={participant_id} in group"
                )
                break
            else:
                point_log(
                    f"require_group_only participant='{participant_name}' id={participant_id} not in group"
                )
                continue
        if not has_in_group_participant:
            point_log(
                f"require_group_only blocked all points for reason={reason}"
            )
            return result

    min_submission_pts, max_submission_pts = await get_group_submission_point_limits(group_id, external_session)
    point_log(
        f"submission_point_limits min={min_submission_pts} max={max_submission_pts}"
    )

    if force_no_split:
        point_log("no_split rule matched; only receiver can be awarded")
    if not included_player_names:
        point_log("no additional normalized participants detected for split")
    if included_player_names and not force_no_split:
        should_share_points, split_method = await check_group_points_sharing(group_id, external_session)
        point_log(
            f"split_settings should_share_points={should_share_points} "
            f"split_method={split_method}"
        )
        if should_share_points:
            valid_share_targets = []
            for nearby_player in included_player_names:
                try:
                    nearby_player_id = _get_player_id_by_name(nearby_player, session)
                    if nearby_player_id is None:
                        point_log(
                            f"split_target='{nearby_player}' not found in DB; skipping"
                        )
                        continue
                    check_split_capable = await check_split_capability(group_id, nearby_player_id, external_session)
                    if not check_split_capable:
                        point_log(
                            f"split_target='{nearby_player}' id={nearby_player_id} "
                            f"not in group; skipping"
                        )
                        continue
                    valid_share_targets.append((nearby_player_id, nearby_player))
                except Exception as e:
                    point_log(f"split_target='{nearby_player}' resolution error: {e}")
                    continue
            point_log(
                f"valid_share_targets={valid_share_targets} target_count={len(valid_share_targets)}"
            )
            target_count = len(valid_share_targets)
            if target_count > 0:
                # Divide the award among the receiver and the in-group targets.
                # equal_split uses FLOOR division so the total distributed never
                # exceeds the item's point value; the indivisible remainder goes
                # to the receiving player. award_all gives everyone the full award.
                per_target_share, receiver_share = _compute_split_shares(
                    point_award, target_count, split_method
                )
                point_log(
                    f"split_shares method={split_method} target_count={target_count} "
                    f"point_award={point_award} per_target_share={per_target_share} "
                    f"receiver_share={receiver_share}"
                )

                for target_player_id, target_player_name in valid_share_targets:
                    points_to_award = per_target_share
                    if points_to_award > 0:
                        points_to_award = await modify_for_event(
                            reason,
                            group_id,
                            target_player_id,
                            points_to_award,
                            item_id=item_id,
                            npc_id=npc_id,
                            submission_timestamp=submission_timestamp,
                            external_session=external_session,
                        )
                    points_to_award = _apply_submission_point_bounds(
                        points_to_award,
                        min_submission_pts=min_submission_pts,
                        max_submission_pts=max_submission_pts,
                    )
                    if points_to_award > 0:
                        point_log(
                            f"awarding shared points={points_to_award} "
                            f"to target_player_id={target_player_id} split_method={split_method}"
                        )
                        shared_awarded = await award_player_points(
                            reason
                            + " (received by "
                            + str(receiver_player_name)
                            + " from "
                            + ", ".join(included_player_names)
                            + ")",
                            group_id,
                            target_player_id,
                            points_to_award,
                            entry_id=entry_id,
                            external_session=external_session,
                        )
                        _record_award(target_player_id, target_player_name, shared_awarded)

                # Reduce the receiver's award to their share (equal_split: floor
                # share + indivisible remainder; award_all: unchanged).
                point_award = receiver_share
        else:
            point_log("point_sharing disabled; split awards skipped")
    point_award = await modify_for_event(
        reason,
        group_id,
        receiver_player_id,
        point_award,
        item_id=item_id,
        npc_id=npc_id,
        submission_timestamp=submission_timestamp,
        external_session=external_session,
    )
    point_award = _apply_submission_point_bounds(
        point_award,
        min_submission_pts=min_submission_pts,
        max_submission_pts=max_submission_pts,
    )
    point_log(f"receiver_award_after_bounds={point_award}")

    if point_award > 0:
        point_log(
            f"awarding receiver points={point_award} receiver_player_id={receiver_player_id}"
        )
        receiver_awarded = await award_player_points(
            reason,
            group_id,
            receiver_player_id,
            point_award,
            entry_id=entry_id,
            external_session=external_session,
        )
        _record_award(receiver_player_id, receiver_player_name, receiver_awarded)
    point_log(f"final_award_result={result}")
    return result

def _extract_scalar(value):
    """Return scalar from ORM tuple/row-like values."""
    if value is None:
        return None
    if isinstance(value, tuple):
        return value[0] if value else None
    try:
        return value[0]
    except Exception:
        return value


def _get_player_id_by_name(player_name, session):
    # OSRS treats spaces and underscores as equivalent in player names.
    # Try exact match first (fast path), then fall back to normalized lookup.
    row = session.query(Player.player_id).filter(Player.player_name.ilike(player_name)).first()
    if row is None and (" " in player_name or "_" in player_name):
        alt_name = player_name.replace(" ", "_") if " " in player_name else player_name.replace("_", " ")
        row = session.query(Player.player_id).filter(Player.player_name.ilike(alt_name)).first()
    player_id = _extract_scalar(row)
    try:
        return int(player_id) if player_id is not None else None
    except Exception:
        return None


def _get_player_name_by_id(player_id, session):
    row = session.query(Player.player_name).filter(Player.player_id == player_id).first()
    return _extract_scalar(row)


def _normalize_player_names(players_included, receiver_player_name):
    if not players_included:
        return []
    if isinstance(players_included, str):
        raw_text = players_included.strip()
        if not raw_text:
            raw_source = []
        elif raw_text.startswith("["):
            parsed_list = None
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, list):
                    parsed_list = parsed
            except Exception:
                parsed_list = None
            if parsed_list is not None:
                raw_source = parsed_list
            else:
                raw_source = [
                    part.strip()
                    for part in raw_text.replace("\n", ",").split(",")
                    if part.strip()
                ]
        else:
            raw_source = [
                part.strip()
                for part in raw_text.replace("\n", ",").split(",")
                if part.strip()
            ]
    elif isinstance(players_included, dict):
        raw_source = players_included.values()
    else:
        raw_source = players_included
    normalized = []
    seen = set()
    receiver_name = str(receiver_player_name).strip().lower() if receiver_player_name else None
    for raw_name in raw_source:
        name = str(raw_name).strip() if raw_name is not None else ""
        if not name:
            continue
        lowered = name.lower()
        if receiver_name and lowered == receiver_name:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(name)
    return normalized


def _apply_submission_point_bounds(points_to_award, min_submission_pts=0, max_submission_pts=0):
    """Apply per-player min/max submission bounds.

    Bounds are only applied when points_to_award is already positive. A bound value
    of 0 means "disabled/unset" for that bound.
    """
    try:
        points = int(points_to_award)
    except Exception:
        return 0

    if points <= 0:
        return 0

    try:
        min_pts = max(0, int(min_submission_pts))
    except Exception:
        min_pts = 0
    try:
        max_pts = max(0, int(max_submission_pts))
    except Exception:
        max_pts = 0

    if max_pts > 0 and min_pts > max_pts:
        min_pts = max_pts

    if max_pts > 0:
        points = min(points, max_pts)
    if min_pts > 0:
        points = max(points, min_pts)

    return int(points)


async def check_split_capability(group_id, player_id, external_session=None):
    from data.submissions.common import select_session_and_flag
    session, _ = select_session_and_flag(external_session)
    check_row = (
        session.query(user_group_association)
        .filter(
            user_group_association.c.player_id == int(player_id),
            user_group_association.c.group_id == int(group_id),
        )
        .first()
    )
    return check_row is not None


async def get_group_submission_point_limits(group_id, external_session=None):
    """Return (min_submission_pts, max_submission_pts) for this group."""
    from data.submissions.common import select_session_and_flag
    session, use_ext = select_session_and_flag(external_session)

    def _read_or_create(config_key, default_fallback="0"):
        config = (
            session.query(GroupConfiguration)
            .filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == config_key,
            )
            .first()
        )
        if config and config.config_value is not None:
            try:
                return max(0, int(config.config_value))
            except Exception:
                return 0

        default_config = (
            session.query(GroupConfiguration)
            .filter(
                GroupConfiguration.group_id == 1,
                GroupConfiguration.config_key == config_key,
            )
            .first()
        )
        default_val = 0
        if default_config and default_config.config_value is not None:
            try:
                default_val = max(0, int(default_config.config_value))
            except Exception:
                default_val = 0
        else:
            try:
                default_val = max(0, int(default_fallback))
            except Exception:
                default_val = 0
        try:
            session.add(
                GroupConfiguration(
                    group_id=group_id,
                    config_key=config_key,
                    config_value=str(default_val),
                )
            )
            _persist(session, use_ext)
        except Exception:
            pass
        return default_val

    min_submission_pts = _read_or_create("min_submission_pts", "0")
    max_submission_pts = _read_or_create("max_submission_pts", "0")
    return min_submission_pts, max_submission_pts


async def check_points_require_group_only_mode(group_id, external_session=None):
    from data.submissions.common import select_session_and_flag
    session, use_ext = select_session_and_flag(external_session)
    config = (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == "points_require_group_only",
        )
        .first()
    )
    if config and config.config_value is not None:
        try:
            return int(config.config_value) == 1
        except Exception:
            return False
    default_config = (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == 1,
            GroupConfiguration.config_key == "points_require_group_only",
        )
        .first()
    )
    default_val = 0
    if default_config and default_config.config_value is not None:
        try:
            default_val = 1 if int(default_config.config_value) == 1 else 0
        except Exception:
            default_val = 0
    try:
        session.add(
            GroupConfiguration(
                group_id=group_id,
                config_key="points_require_group_only",
                config_value=str(default_val),
            )
        )
        _persist(session, use_ext)
    except Exception:
        return default_val == 1
    return default_val == 1

async def check_group_points_sharing(group_id, external_session=None):
    from data.submissions.common import select_session_and_flag
    session, use_ext = select_session_and_flag(external_session)
    group_points_sharing = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id, GroupConfiguration.config_key == "point_sharing").first()
    group_points_sharing_method = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id, GroupConfiguration.config_key == "point_sharing_method").first()
    split_method = "equal_split"
    if group_points_sharing_method and group_points_sharing_method.config_value is not None:
        split_method = group_points_sharing_method.config_value
    else:
        default_config = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == 1, GroupConfiguration.config_key == "point_sharing_method")
            .first()
        )
        if default_config and default_config.config_value is not None:
            new_config = GroupConfiguration(group_id=group_id, config_key="point_sharing_method", config_value=str(default_config.config_value))
            session.add(new_config)
            _persist(session, use_ext)
            split_method = default_config.config_value
        else:
            split_method = "equal_split"
    should_share_points = False
    if group_points_sharing and group_points_sharing.config_value is not None:
        should_share_points = int(group_points_sharing.config_value) == 1
    else:
        default_config = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == 1, GroupConfiguration.config_key == "point_sharing")
            .first()
        )
        if default_config and default_config.config_value is not None:
            new_config = GroupConfiguration(group_id=group_id, config_key="point_sharing", config_value=str(default_config.config_value))
            session.add(new_config)
            _persist(session, use_ext)
            should_share_points = int(default_config.config_value) == 1
        else:
            should_share_points = False
    return should_share_points, split_method

async def get_group_point_mods(group_id, external_session=None):
    from data.submissions.common import select_session_and_flag
    session, _ = select_session_and_flag(external_session)
    group_point_mods = session.query(GroupPointMods).filter(GroupPointMods.group_id == group_id).all()
    if group_point_mods:
        return group_point_mods
    else:
        return None


def _point_list_entry_matches(entry, incoming_item_id: int, incoming_npc_id: int) -> bool:
    try:
        entry_item_id = int(getattr(entry, "item_id", 0) or 0)
    except Exception:
        entry_item_id = 0
    try:
        entry_npc_id = int(getattr(entry, "npc_id", 0) or 0)
    except Exception:
        entry_npc_id = 0

    # Empty rows are ignored.
    if entry_item_id == 0 and entry_npc_id == 0:
        return False

    item_match = (entry_item_id == 0) or (incoming_item_id != 0 and incoming_item_id == entry_item_id)
    npc_match = (entry_npc_id == 0) or (incoming_npc_id != 0 and incoming_npc_id == entry_npc_id)
    return item_match and npc_match


async def evaluate_point_list_rules(group_id, item_id=None, npc_id=None, external_session=None):
    from data.submissions.common import select_session_and_flag

    session, _ = select_session_and_flag(external_session)
    entries = (
        session.query(GroupPointBlacklist)
        .filter(GroupPointBlacklist.group_id == group_id)
        .all()
    )
    if not entries:
        return True, False

    try:
        incoming_item_id = int(item_id or 0)
    except Exception:
        incoming_item_id = 0
    try:
        incoming_npc_id = int(npc_id or 0)
    except Exception:
        incoming_npc_id = 0

    whitelist_entries = []
    no_split_entries = []
    for entry in entries:
        list_type = str(getattr(entry, "list_type", "blacklist") or "blacklist").strip().lower()
        if list_type == "whitelist":
            whitelist_entries.append(entry)
            continue
        if list_type == "no_split":
            no_split_entries.append(entry)
            continue
        # Default unknown types to blacklist for safer behavior.
        if _point_list_entry_matches(entry, incoming_item_id, incoming_npc_id):
            return False, False

    # If any whitelist entries exist, event must match at least one.
    if whitelist_entries:
        matched_whitelist = False
        for entry in whitelist_entries:
            if _point_list_entry_matches(entry, incoming_item_id, incoming_npc_id):
                matched_whitelist = True
                break
        if not matched_whitelist:
            return False, False

    force_no_split = False
    for entry in no_split_entries:
        if _point_list_entry_matches(entry, incoming_item_id, incoming_npc_id):
            force_no_split = True
            break

    return True, force_no_split

async def award_player_points(
    reason, group_id, player_id, points_to_award, entry_id=None, *, external_session=None
) -> int:
        print(
            f"Awarding {points_to_award} points to {player_id} for {reason} (entry_id={entry_id}) "
        )
        from data.submissions.common import select_session_and_flag
        session, use_ext = select_session_and_flag(external_session)
        try:
            entry = PlayerPoints(
                player_id=player_id,
                group_id=group_id,
                amount=int(points_to_award),
                # MySQL strict mode rejects values longer than the column
                # (raid drops build reasons listing every participant).
                reason=str(reason)[:_PLAYER_POINTS_REASON_MAX_LEN],
                entry_id=int(entry_id) if entry_id not in (None, "", 0, "0") else None,
            )
            if use_ext:
                # SAVEPOINT: a failed insert must roll back only this point
                # award, never the caller's transaction (which holds the
                # not-yet-committed submission row).
                with session.begin_nested():
                    session.add(entry)
                    session.flush()
            else:
                session.add(entry)
                session.commit()
            return points_to_award
        except Exception as e:
            print(f"Could not award points to player: {e}")
            if not use_ext:
                try:
                    session.rollback()
                except Exception:
                    pass
            return 0

async def get_point_config(group_id: int, external_session=None):
    from data.submissions.common import select_session_and_flag
    session, use_ext = select_session_and_flag(external_session)
    group_point_config = session.query(GroupPointConfig).filter(GroupPointConfig.group_id == group_id).all()
    if group_point_config:
        config = {conf.reason: f"{conf.award},{conf.divisor}" for conf in group_point_config}
    else:
        config = _create_default_config(session, group_id, use_ext)
    return config
    
def _create_default_config(session, group_id, is_external=False):
    try:
        drops = GroupPointConfig(group_id=group_id, 
                                reason="drop", 
                                award=1, 
                                divisor=10000000,
                                description="Awarded when a player receives a drop. (Stack value of drop divided by divisor, multiplied by point award)")
        pbs = GroupPointConfig(group_id=group_id, 
                                reason="pb", 
                                award=1,
                                description="Awarded when a player receives a new personal best")
        pets = GroupPointConfig(group_id=group_id, 
                                reason="pet", 
                                award=10,
                                description="Awarded when a player obtains a new pet")
        clogs = GroupPointConfig(group_id=group_id, 
                                reason="clog",
                                award=1,
                                description="Awarded when a player completes a new collection log slot.")
        easy_cas = GroupPointConfig(group_id=group_id, 
                                reason="easy_ca",
                                award=1,
                                description="Awarded when a player completes an Easy combat achievement.")
        medium_cas = GroupPointConfig(group_id=group_id, 
                                reason="medium_ca",
                                award=2,
                                description="Awarded when a player completes a Medium combat achievement.")
        hard_cas = GroupPointConfig(group_id=group_id, 
                                reason="hard_ca",
                                award=3,
                                description="Awarded when a player completes a Hard combat achievement.")
        elite_cas = GroupPointConfig(group_id=group_id, 
                                reason="elite_ca",
                                award=4,
                                description="Awarded when a player completes an Elite combat achievement.")
        master_cas = GroupPointConfig(group_id=group_id, 
                                reason="master_ca",
                                award=5,
                                description="Awarded when a player completes a Master combat achievement.")
        grandmaster_cas = GroupPointConfig(group_id=group_id, 
                                reason="grandmaster_ca",
                                award=6,
                                description="Awarded when a player completes a Grandmaster combat achievement.")
        session.add(drops)
        session.add(pbs)
        session.add(pets)
        session.add(clogs)
        session.add(easy_cas)
        session.add(medium_cas)
        session.add(hard_cas)
        session.add(elite_cas)
        session.add(master_cas)
        session.add(grandmaster_cas)
        _persist(session, is_external)
        return {
            "drop": f"{drops.award},{drops.divisor}",
            "pb": f"{pbs.award},{pbs.divisor}",
            "pet": f"{pets.award},{pets.divisor}",
            "clog": f"{clogs.award},{clogs.divisor}",
            "easy_ca": f"{easy_cas.award},{easy_cas.divisor}",
            "medium_ca": f"{medium_cas.award},{medium_cas.divisor}",
            "hard_ca": f"{hard_cas.award},{hard_cas.divisor}",
            "elite_ca": f"{elite_cas.award},{elite_cas.divisor}",
            "master_ca": f"{master_cas.award},{master_cas.divisor}",
            "grandmaster_ca": f"{grandmaster_cas.award},{grandmaster_cas.divisor}",
        }
    except Exception as e:
        print(f"Could not create new group configurations as defined: {e}")
        return {}