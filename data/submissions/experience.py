"""Experience/Level-up submissions processor."""

import asyncio
import json
import traceback
from datetime import datetime

from services.points import is_feature_active_for_group

from .common import (
    SubmissionResponse,
    ensure_player_by_name_then_auth,
    get_player_groups_with_global,
    is_user_dm_enabled,
    create_notification,
    is_truthy_config,
    screenshot_required,
    select_session_and_flag,
    debug_print,
    GroupConfiguration,
    award_points_to_player,
)

# All OSRS skills in lowercase (matching PlayerExperience model columns)
SKILL_NAMES = [
    "attack", "strength", "defence", "ranged", "prayer", "magic",
    "runecraft", "hitpoints", "crafting", "mining", "smithing",
    "woodcutting", "farming", "firemaking", "fishing", "hunter",
    "herblore", "cooking", "thieving", "construction", "slayer",
    "agility", "fletching", "sailing"
]

# Level milestones that award points
POINT_MILESTONES = {
    99: 100,   # Max level in a skill
    200: 50,   # Level 200M XP milestone conceptually (virtual levels)
}

# Heuristic: ignore "initial sync" submissions where many skills jump a lot at once.
# This commonly happens when the player first logs in and the plugin detects 0 -> actual levels.
_BULK_SYNC_LEVEL_GAIN_THRESHOLD = 10   # "more than 5 levels each"
_BULK_SYNC_SKILLS_THRESHOLD = 5       # "more than a few skills"

def _safe_int(value, default: int = 0) -> int:
    """Convert value to int with a default on None/invalid."""
    try:
        if value is None:
            return default
        return int(value)
    except (ValueError, TypeError):
        return default

def _parse_int_list_config(value) -> list[int]:
    """Parse config values like '[99, 1000]' or '99,1000' into a list[int]."""
    if value is None:
        return []
    # Support passing a GroupConfiguration object by accident
    if hasattr(value, "config_value"):
        value = getattr(value, "config_value")
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        out: list[int] = []
        for x in value:
            try:
                out.append(int(x))
            except (ValueError, TypeError):
                continue
        return out

    if isinstance(value, (int, float)):
        try:
            return [int(value)]
        except Exception:
            return []

    # Strings: allow JSON arrays or comma-separated values
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return [int(x) for x in arr if str(x).strip("-").isdigit()]
            except Exception:
                # fall through to manual parsing
                pass
        s = s.replace("[", "").replace("]", "").replace(" ", "")
        if not s:
            return []
        out: list[int] = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except (ValueError, TypeError):
                continue
        return out

    # Fallback: try string cast
    try:
        return _parse_int_list_config(str(value))
    except Exception:
        return []


def _parse_skill_data(experience_data: dict) -> dict:
    """Parse skill-specific data from the experience submission.
    
    Returns a dict with:
        - skill_name: lowercase skill name
        - xp_total: total XP in the skill
        - new_level: the new level achieved
        - levels_gained: number of levels gained (usually 1)
    """
    skills_leveled = experience_data.get("skills_leveled", experience_data.get("skills_trained", ""))
    skill_name = skills_leveled.lower() if skills_leveled else None
    
    if not skill_name:
        return {}
    
    result = {
        "skill_name": skill_name,
        "xp_total": 0,
        "new_level": 0,
        "levels_gained": 0,
    }
    
    # Parse skill-specific fields (e.g., attack_xp_total, attack_new_level)
    xp_key = f"{skill_name}_xp_total"
    level_key = f"{skill_name}_new_level"
    gained_key = f"{skill_name}_level_gained"
    
    if xp_key in experience_data:
        try:
            result["xp_total"] = int(experience_data[xp_key])
        except (ValueError, TypeError):
            pass
    
    if level_key in experience_data:
        try:
            result["new_level"] = int(experience_data[level_key])
        except (ValueError, TypeError):
            pass
    
    if gained_key in experience_data:
        try:
            result["levels_gained"] = int(experience_data[gained_key])
        except (ValueError, TypeError):
            pass
    
    return result


def _parse_skills_data(experience_data: dict) -> list[dict]:
    """
    Parse multi-skill level up submissions.

    The plugin may send `skills_leveled` like "Attack,Combat". "Combat" is not an OSRS skill
    column in PlayerExperience, but we still want to include it in notifications.

    Returns a list of dicts like:
        {
          "skill_key": "attack" | ... | "combat",
          "skill_name": "Attack" | ... | "Combat",
          "xp_total": int,
          "new_level": int,
          "levels_gained": int,
        }
    """
    skills_leveled = experience_data.get("skills_leveled", experience_data.get("skills_trained", "")) or ""
    raw_parts = [p.strip() for p in str(skills_leveled).split(",") if p and str(p).strip()]
    seen: set[str] = set()
    out: list[dict] = []

    for part in raw_parts:
        key = part.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)

        if key == "combat":
            new_level = _safe_int(experience_data.get("combat_new_level") or experience_data.get("combat_level"), default=0)
            gained = _safe_int(experience_data.get("combat_level_gained"), default=0)
            out.append(
                {
                    "skill_key": "combat",
                    "skill_name": "Combat",
                    "xp_total": 0,
                    "new_level": new_level,
                    "levels_gained": gained,
                }
            )
            continue

        if key not in SKILL_NAMES:
            continue

        xp_total = _safe_int(experience_data.get(f"{key}_xp_total"), default=0)
        new_level = _safe_int(experience_data.get(f"{key}_new_level"), default=0)
        gained = _safe_int(experience_data.get(f"{key}_level_gained"), default=0)
        out.append(
            {
                "skill_key": key,
                "skill_name": key.title(),
                "xp_total": xp_total,
                "new_level": new_level,
                "levels_gained": gained,
            }
        )

    return out


async def experience_processor(experience_data, external_session=None):
    """Process experience/level-up submissions.
    
    Handles:
        1. Player authentication
        2. Updating PlayerExperience record with new XP values
        3. Creating notifications for level-ups to subscribed groups
        4. Awarding points for milestone levels (e.g., 99)
        5. DM notifications if user has them enabled
    """
    stage = "start"
    debug_print(f"=== EXPERIENCE PROCESSOR START ===")
    #print(f"[EXP] stage={stage} external_session={external_session is not None}")
    # Keep this verbose while stabilizing; contains useful clues when parsing fails.
    #print(f"[EXP] raw={experience_data}")

    try:
        stage = "select_session"
        session, use_external_session = select_session_and_flag(external_session)
        debug_print(f"Using external session: {use_external_session}")

        # Extract core fields
        stage = "extract_core_fields"
        player_name = experience_data.get("player_name")
        account_hash = experience_data.get("acc_hash")
        auth_key = experience_data.get("auth_key", "")
        unique_id = experience_data.get("guid")
    
        # Parse totals
        stage = "parse_totals"
        try:
            total_xp = int(experience_data.get("total_xp", 0))
        except (ValueError, TypeError):
            total_xp = 0
    
        try:
            total_level = int(experience_data.get("total_level", 0))
        except (ValueError, TypeError):
            total_level = 0
    
        try:
            combat_level = int(experience_data.get("combat_level", 0))
        except (ValueError, TypeError):
            combat_level = 0
    
        # Image data
        stage = "extract_image_data"
        downloaded = experience_data.get("downloaded", False)
        image_url = experience_data.get("image_url", experience_data.get("image_path"))
        used_api = experience_data.get("used_api", False)
    
        # Parse skill-specific data
        stage = "parse_skill_data"
        skills = _parse_skills_data(experience_data)
        # Pick a "primary" skill for backward compatibility with older templates
        primary = next((s for s in skills if s.get("skill_key") in SKILL_NAMES), None) or (skills[0] if skills else None)
        skill_name = (primary or {}).get("skill_name")
        new_level = _safe_int((primary or {}).get("new_level"), default=0)
        xp_total = _safe_int((primary or {}).get("xp_total"), default=0)
        levels_gained = _safe_int((primary or {}).get("levels_gained"), default=0)
    
        notice = ""
    
        stage = "validate_player_fields"
        if not player_name or not account_hash:
            #print("[EXP] Missing player_name or acc_hash, aborting")
            return SubmissionResponse(
                success=False,
                message="Missing required player identification fields."
            )
    
        stage = "validate_skill_name"
        if not skills:
            #print(f"[EXP] No valid skills leveled up. skills_leveled={experience_data.get('skills_leveled')}")
            return SubmissionResponse(
                success=False,
                message="No valid skills leveled up."
            )
    
        # Authenticate player
        stage = "auth_player"
        player, authed, user_exists = await ensure_player_by_name_then_auth(
            session, player_name, account_hash, auth_key
        )
    
        if not player:
            #print(f"[EXP] Player not found: {player_name}")
            return SubmissionResponse(
                success=False,
                message="Player not found or could not be created."
            )
    
        if not user_exists or not authed:
            #print(f"[EXP] Authentication failed for player: {player_name}")
            return SubmissionResponse(
                success=False,
                message="Player authentication failed."
            )
    
        player_id = player.player_id
        #print(f"[EXP] Player authenticated: {player_name} (ID: {player_id})")
    
        # Import PlayerExperience model
        stage = "import_models"
        from db import PlayerExperience
    
        # Get or create PlayerExperience record
        stage = "load_player_experience"
        exp_entry = (
            session.query(PlayerExperience)
            .filter(PlayerExperience.player_id == player_id)
            .first()
        )
    
        stage = "upsert_player_experience"
        # Update PlayerExperience for each real OSRS skill (exclude "combat")
        real_skill_updates = [s for s in skills if s.get("skill_key") in SKILL_NAMES]

        old_xp = 0  # kept for legacy prints / compatibility
        if exp_entry:
            exp_entry.last_updated = datetime.now()
            for s in real_skill_updates:
                k = s["skill_key"]
                new_xp = _safe_int(s.get("xp_total"), default=0)
                prev_xp = getattr(exp_entry, k, 0) or 0
                if new_xp > prev_xp:
                    setattr(exp_entry, k, new_xp)
                    #print(f"[EXP] Updated {k} XP: {prev_xp} -> {new_xp}")
        else:
            # Create new PlayerExperience record
            exp_entry = PlayerExperience(
                player_id=player_id,
                last_updated=datetime.now()
            )
            # Set the skill XP(s)
            for s in real_skill_updates:
                k = s["skill_key"]
                setattr(exp_entry, k, _safe_int(s.get("xp_total"), default=0))
            session.add(exp_entry)
            #print(f"[EXP] Created new PlayerExperience for player {player_id}")
    
        # Update player's total level if provided and higher
        stage = "update_player_total_level"
        if total_level > 0 and (not player.total_level or total_level > player.total_level):
            player.total_level = total_level
            #print(f"[EXP] Updated player total level to {total_level}")
    
        stage = "commit_experience"
        session.commit()

        # If this looks like an initial "0 -> real level" sync, we should NOT notify.
        # Still update the stored XP/levels above so the backend stays accurate.
        stage = "bulk_sync_detection"
        big_gains = [
            s
            for s in skills
            if s.get("skill_key") in SKILL_NAMES
            and _safe_int(s.get("levels_gained"), default=0) > _BULK_SYNC_LEVEL_GAIN_THRESHOLD
        ]
        total_gained = sum((_safe_int(s.get("levels_gained"), default=0) for s in skills))
        if len(big_gains) >= _BULK_SYNC_SKILLS_THRESHOLD:
            try:
                print(
                    "[EXP] Ignoring likely initial level sync: "
                    f"big_gains={len(big_gains)} total_skills={len(skills)} total_gained={total_gained} "
                    f"skills_leveled={experience_data.get('skills_leveled')}"
                )
            except Exception:
                pass
            return SubmissionResponse(
                success=True,
                message="Level-up recorded (notifications skipped: likely initial level sync).",
                notice="Ignored a likely initial bulk level sync (many skills jumped by >5 levels).",
            )
    
    # # Check for milestone level achievements and award points
    # if new_level in POINT_MILESTONES:
    #     points_to_award = POINT_MILESTONES[new_level]
    #     try:
    #         award_points_to_player(
    #             player_id=player_id,
    #             amount=points_to_award,
    #             source=f"Reached level {new_level} in {skill_name.capitalize()}",
    #             expires_in_days=60,
    #         )
    #         debug_print(f"Awarded {points_to_award} points for level {new_level} milestone")
    #     except Exception as e:
    #         debug_print(f"Failed to award milestone points: {e}")
    
        # Create notifications for groups
        stage = "load_player_groups"
        player_groups = get_player_groups_with_global(session, player)
    
        stage = "group_loop"
        for group in player_groups:
            await asyncio.sleep(0)  # Yield to event loop
            group_id = group.group_id

            # TODO -- LOCK THIS BEHIND PREMIUM FEATURES
            # if not is_feature_active_for_group(group_id=group_id, feature_key='level_notifications', session=session):
            #     continue

            # Check if group has level notifications enabled
            level_notify_config = (
                session.query(GroupConfiguration)
                .filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == "notify_levels",
                )
                .first()
            )
            minimum_level = (
                session.query(GroupConfiguration)
                .filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == "level_minimum_for_notifications",
                )
                .first()
            )
            minimum_level_to_notify = (
                _safe_int(getattr(minimum_level, "config_value", None), default=1) if minimum_level else 1
            )
            milestone_levels = (
                session.query(GroupConfiguration)
                .filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == "level_milestones_to_notify",
                )
                .first()
            )
            milestone_levels_to_notify = _parse_int_list_config(milestone_levels)

            # Per-group diagnostic print (helps isolate config-related failures)
            # try:
            #     print(
            #         "[EXP] group_diag "
            #         f"group_id={group_id} "
            #         f"notify_levels={getattr(level_notify_config, 'config_value', None)} "
            #         f"min_level={getattr(minimum_level, 'config_value', None)} "
            #         f"milestones={getattr(milestone_levels, 'config_value', None)} "
            #         f"parsed_milestones={milestone_levels_to_notify}"
            #     )
            # except Exception:
            #     pass

            def is_milestone_level():
                if total_level is None or total_level < 1:
                    return False
                return total_level in milestone_levels_to_notify

            max_new_level = max((_safe_int(s.get("new_level"), default=0) for s in skills), default=0)
            if max_new_level < minimum_level_to_notify and not is_milestone_level():
                continue
            if not level_notify_config:
                continue
            if not is_truthy_config(level_notify_config.config_value):
                continue

            # Check screenshot requirement
            if await screenshot_required(session, group_id):
                if not image_url:
                    notice = (
                        f"Your level-up submission did not include a screenshot "
                        f"(required for {group.group_name}). Please enable screenshots "
                        f"in the DropTracker plugin configuration."
                    )
                    continue

            # Build notification data
            skills_names = ", ".join([s.get("skill_name", "") for s in skills if s.get("skill_name")]) or ""
            skills_text = ", ".join(
                [
                    f"{s.get('skill_name')} {s.get('new_level')} (+{s.get('levels_gained')})"
                    if _safe_int(s.get("levels_gained"), default=0) > 0
                    else f"{s.get('skill_name')} {s.get('new_level')}"
                    for s in skills
                    if s.get("skill_name")
                ]
            )
            notification_data = {
                "player_name": player_name,
                "player_id": player_id,
                # Legacy fields (kept for older templates)
                "skill_name": skills_names or (skill_name or ""),
                "new_level": max_new_level,
                "levels_gained": sum((_safe_int(s.get("levels_gained"), default=0) for s in skills)),
                "xp_total": xp_total,
                # New multi-skill fields
                "skills": skills,
                "skills_names": skills_names,
                "skills_text": skills_text,
                "total_level": total_level,
                "total_xp": total_xp,
                "combat_level": combat_level,
                "image_url": image_url if image_url else "",
            }

            stage = "create_group_notifications"
            await create_notification(
                "level_up",
                player_id,
                notification_data,
                group_id,
                existing_session=session if use_external_session else None,
            )
            #print(f"[EXP] Created level_up notification for group {group_id}")

            if is_milestone_level():
                await create_notification(
                    "total_level_milestone",
                    player_id,
                    notification_data,
                    group_id,
                    existing_session=session if use_external_session else None,
                )
                #print(f"[EXP] Created total_level_milestone notification for group {group_id}")

            # Check for DM notifications
            stage = "create_dm_notifications"
            if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_levels"):
                await create_notification(
                    "dm_level_up",
                    player_id,
                    notification_data,
                    group_id,
                    existing_session=session if use_external_session else None,
                )
                #print(f"[EXP] Created DM level_up notification for user {player.user_id}")
    
        stage = "done"
        #print(f"[EXP] === EXPERIENCE PROCESSOR END ===")
        try:
            summary = notification_data.get("skills_text") if "notification_data" in locals() else ""
        except Exception:
            summary = ""
        return SubmissionResponse(
            success=True,
            message=f"Level-up recorded: {summary}" if summary else "Level-up recorded.",
            notice=notice if notice else None
        )

    except Exception as e:
        #print(f"[EXP] ERROR stage={stage} err={type(e).__name__}: {e}")
        print(traceback.format_exc())
        try:
            # Roll back if we own the session
            if not use_external_session:
                session.rollback()
        except Exception:
            pass
        return SubmissionResponse(success=False, message=f"Experience processor failed at stage '{stage}': {e}")