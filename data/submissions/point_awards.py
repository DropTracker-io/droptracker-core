"""Group-specific point awards for in-group achievements.

These points are separate from the global premium credit system in `services.points`.
They are stored in `player_points` with a `group_id` so per-group balances can be computed
without affecting global credits used for premium features.
"""

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


def _point_debug(message: str):
    """Uniform debug output for point/split investigation."""
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


def _ceil_div(value: int, divisor: int) -> int:
    """Integer ceil division for split awards."""
    if divisor <= 0:
        return 0
    if value <= 0:
        return 0
    return (value + divisor - 1) // divisor


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
    _point_debug(
        "start "
        f"reason={reason} group_id={group_id} receiver_player_id={receiver_player_id} "
        f"value={value} quantity={quantity} item_id={item_id} npc_id={npc_id} "
        f"entry_id={entry_id} raw_players_included={players_included}"
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
        _point_debug(f"group_id={group_id} point system inactive; skipping")
        return result
    group_config = await get_point_config(group_id, external_session)
    rawaward = group_config.get(reason)
    _point_debug(f"group_id={group_id} point_config[{reason}]={rawaward}")
    if not rawaward:
        # No configured award for this reason
        _point_debug(f"group_id={group_id} no config for reason={reason}; skipping")
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
        _point_debug(f"group_id={group_id} malformed config rawaward={rawaward}; skipping")
        return result
    _point_debug(
        f"group_id={group_id} parsed_config default_award={default_award} default_divisor={default_divisor}"
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
    _point_debug(
        f"group_id={group_id} point_list_rules allow_award={allow_award} force_no_split={force_no_split} "
        f"incoming_item_id={incoming_item_id} incoming_npc_id={incoming_npc_id}"
    )
    if not allow_award:
        return result

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
            if item_match and npc_match and mod.event_type == reason:
                try:
                    award = int(mod.award)
                    has_mod_override = True
                except Exception:
                    award = default_award
                try:
                    divisor = int(mod.divisor) if int(mod.divisor) != 0 else 1
                except Exception:
                    divisor = default_divisor if default_divisor != 0 else 1
                _point_debug(
                    f"group_id={group_id} matched_mod item_id={mod_item_id} npc_id={mod_npc_id} "
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
    _point_debug(
        f"group_id={group_id} pre_split_point_award={point_award} "
        f"has_mod_override={has_mod_override} award={award} divisor={divisor} qty={qty}"
    )

    receiver_player_name = _get_player_name_by_id(receiver_player_id, session)
    included_player_names = _normalize_player_names(players_included, receiver_player_name)
    _point_debug(
        f"group_id={group_id} receiver_player_name={receiver_player_name} "
        f"normalized_players_included={included_player_names} force_no_split={force_no_split}"
    )

    # Optional group-only eligibility gate:
    # When enabled, points are awarded only for solo content or for group content
    # where at least one additional listed participant is in this same group.
    require_group_only = await check_points_require_group_only_mode(group_id, external_session)
    _point_debug(f"group_id={group_id} require_group_only={require_group_only}")
    if require_group_only and included_player_names:
        has_in_group_participant = False
        for participant_name in included_player_names:
            participant_id = _get_player_id_by_name(participant_name, session)
            if participant_id is None:
                _point_debug(
                    f"group_id={group_id} require_group_only participant='{participant_name}' not found in DB"
                )
                continue
            if await check_split_capability(group_id, participant_id, external_session):
                has_in_group_participant = True
                _point_debug(
                    f"group_id={group_id} require_group_only participant='{participant_name}' id={participant_id} in group"
                )
                break
            else:
                _point_debug(
                    f"group_id={group_id} require_group_only participant='{participant_name}' id={participant_id} not in group"
                )
                continue
        if not has_in_group_participant:
            _point_debug(
                f"group_id={group_id} require_group_only blocked all points for reason={reason} entry_id={entry_id}"
            )
            return result

    min_submission_pts, max_submission_pts = await get_group_submission_point_limits(group_id, external_session)
    _point_debug(
        f"group_id={group_id} submission_point_limits min={min_submission_pts} max={max_submission_pts}"
    )

    if force_no_split:
        _point_debug(f"group_id={group_id} no_split rule matched; only receiver can be awarded")
    if not included_player_names:
        _point_debug(f"group_id={group_id} no additional normalized participants detected for split")
    if included_player_names and not force_no_split:
        should_share_points, split_method = await check_group_points_sharing(group_id, external_session)
        _point_debug(
            f"group_id={group_id} split_settings should_share_points={should_share_points} "
            f"split_method={split_method}"
        )
        if should_share_points:
            valid_share_targets = []
            for nearby_player in included_player_names:
                try:
                    nearby_player_id = _get_player_id_by_name(nearby_player, session)
                    if nearby_player_id is None:
                        _point_debug(
                            f"group_id={group_id} split_target='{nearby_player}' not found in DB; skipping"
                        )
                        continue
                    check_split_capable = await check_split_capability(group_id, nearby_player_id, external_session)
                    if not check_split_capable:
                        _point_debug(
                            f"group_id={group_id} split_target='{nearby_player}' id={nearby_player_id} "
                            f"not in group; skipping"
                        )
                        continue
                    valid_share_targets.append((nearby_player_id, nearby_player))
                except Exception as e:
                    _point_debug(f"group_id={group_id} split_target='{nearby_player}' resolution error: {e}")
                    continue
            _point_debug(
                f"group_id={group_id} valid_share_targets={valid_share_targets} target_count={len(valid_share_targets)}"
            )
            target_count = len(valid_share_targets)
            if target_count > 0:
                # For equal_split, include the receiver in the total participant
                # count so the full award is divided among everyone equally.
                if split_method in ("equal_split", "equal"):
                    total_participants = target_count + 1
                    per_person = _ceil_div(int(point_award), int(total_participants))
                else:
                    per_person = None

                for target_player_id, target_player_name in valid_share_targets:
                    if split_method in ("equal_split", "equal"):
                        points_to_award = per_person
                    elif split_method == "award_all":
                        points_to_award = point_award
                    else:
                        points_to_award = 0
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
                        _point_debug(
                            f"group_id={group_id} awarding shared points={points_to_award} "
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

                # Reduce the receiver's award to their equal share
                if per_person is not None:
                    point_award = per_person
                    _point_debug(
                        f"group_id={group_id} equal_split total_participants={total_participants} "
                        f"per_person={per_person}"
                    )
        else:
            _point_debug(f"group_id={group_id} point_sharing disabled; split awards skipped")
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
    _point_debug(f"group_id={group_id} receiver_award_after_bounds={point_award}")

    if point_award > 0:
        _point_debug(
            f"group_id={group_id} awarding receiver points={point_award} receiver_player_id={receiver_player_id}"
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
    _point_debug(f"group_id={group_id} final_award_result={result}")
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
        raw_source = [
            part.strip()
            for part in players_included.replace("\n", ",").split(",")
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
        try:
            from data.submissions.common import select_session_and_flag
            session, use_ext = select_session_and_flag(external_session)
            entry = PlayerPoints(
                player_id=player_id,
                group_id=group_id,
                amount=int(points_to_award),
                reason=str(reason),
                entry_id=int(entry_id) if entry_id not in (None, "", 0, "0") else None,
            )
            session.add(entry)
            _persist(session, use_ext)
            return points_to_award
        except Exception as e:
            print(f"Could not award points to player: {e}")
            return 0
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