"""Experience/Level-up submissions processor."""

import asyncio
import json
import traceback
import uuid
from datetime import datetime

from services.points import is_feature_active_for_group

from .common import (
    SubmissionResponse,
    apply_account_type,
    ensure_player_by_name_then_auth,
    get_player_groups_with_global,
    is_user_dm_enabled,
    create_notification,
    download_webhook_screenshot,
    is_truthy_config,
    screenshot_required,
    select_session_and_flag,
    debug_print,
    GroupConfiguration,
    award_points_to_player,
    envelope_from_plugin,
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


def _milestone_levels_for_group(cfg: dict) -> list[int]:
    """Resolve a group's TOTAL-level milestone list from its config rows.

    ``level_milestones`` is the canonical key the web config editor writes;
    ``level_milestones_to_notify`` is the legacy key this processor used to
    read, and which neither config registry exposes — so a group can neither
    see nor clear it. Presence of the canonical row, not its truthiness,
    decides: clearing the list in the editor stores "", and treating that as
    "unset" sent the group straight back to the invisible legacy list, which
    made the setting impossible to turn off (group 315, 2026-08-27).
    """
    if "level_milestones" in cfg:
        return _parse_int_list_config(cfg["level_milestones"])
    return _parse_int_list_config(cfg.get("level_milestones_to_notify"))


def _crosses_total_milestone(previous_total: int, new_total: int, milestones) -> bool:
    """Whether this submission CROSSED a total-level milestone.

    Not "is the total sitting on one": total_level counts real levels only
    (the plugin caps each skill at 99), so a maxed player parks on 2376
    permanently. A membership test re-announced the milestone on every later
    level-up, and for a maxed player every one of those is a virtual level —
    which is how >99 levels reached groups with notify_virtual_levels off.

    A zero baseline means this is the first total we have seen for the player;
    we cannot tell what they crossed, so we do not announce.
    """
    if new_total < 1 or previous_total < 1:
        return False
    return any(previous_total < m <= new_total for m in milestones)


def _skill_visible_to_group(
    skill: dict,
    *,
    notify_virtual_levels: bool,
    notify_combat_levels: bool,
) -> bool:
    """Whether a skill may be *named* in a group's announcement at all.

    The opt-in half of :func:`_skill_qualifies_for_group`, without the
    minimum/increment filters. Those two decide whether a level-up is worth an
    announcement of its own; this decides whether a level is one the group
    agreed to ever see. Total-level milestones need exactly this split: the
    milestone is its own event (so a below-minimum skill still belongs on its
    context line) but it must not name a virtual or combat level to a group
    that opted out of them.
    """
    new_level = _safe_int(skill.get("new_level"), default=0)
    if new_level < 1:
        return False
    if skill.get("skill_key") == "combat":
        return notify_combat_levels
    return new_level <= 99 or notify_virtual_levels


def _skill_qualifies_for_group(
    skill: dict,
    *,
    minimum_level: int,
    level_increment: int,
    notify_virtual_levels: bool,
    notify_combat_levels: bool,
) -> bool:
    """Decide whether one leveled skill passes a group's level filters.

    Evaluated per skill, so non-qualifying skills in a coalesced submission
    no longer ride along with a qualifying one. Rules:
      - "combat" is its own opt-in family (notify_combat_levels) and ignores
        the minimum/increment skill filters (those are 1-99 skill concepts;
        combat runs 3-126).
      - Real skills check every level crossed by the gain, so a multi-level
        jump (e.g. a lamp taking a skill 98 -> 100) still announces the 99:
        a crossed level qualifies when it is >= minimum_level and aligned to
        level_increment; level 99 always qualifies regardless of increment;
        levels above 99 (virtual levels) only count when
        notify_virtual_levels is enabled.
    """
    new_level = _safe_int(skill.get("new_level"), default=0)
    if new_level < 1:
        return False
    if skill.get("skill_key") == "combat":
        return notify_combat_levels
    gained = _safe_int(skill.get("levels_gained"), default=0)
    start = max(new_level - gained + 1, 1) if gained >= 1 else new_level
    for crossed in range(start, new_level + 1):
        if crossed > 99 and not notify_virtual_levels:
            continue
        if crossed < minimum_level:
            continue
        if crossed == 99 or level_increment <= 1 or crossed % level_increment == 0:
            return True
    return False


def _parse_skill_data(experience_data: dict) -> dict:
    """Parse skill-specific data from the experience submission.
    
    Returns a dict with:
        - skill_name: lowercase skill name
        - xp_total: total XP in the skill
        - new_level: the new level achieved
        - levels_gained: number of levels gained (usually 1)
    """
    # `or` (not a dict default): milestone submissions send skills_leveled=""
    # with the affected skills in skills_trained.
    skills_leveled = experience_data.get("skills_leveled") or experience_data.get("skills_trained") or ""
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
    skills_leveled = experience_data.get("skills_leveled") or experience_data.get("skills_trained") or ""
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


def _parse_snapshot_skills(experience_data) -> list[dict]:
    """Parse an ``experience_update`` snapshot's ``skills_data`` field.

    ``skills_data`` is a JSON object mapping lowercase skill name to
    ``[xp_total, level]``. The plugin sends one on login/logout/world-hop and
    periodically while an XP-tracking event is active, so xp_target /
    skill_target tasks progress even when no level-up occurs (e.g. 99+ skills).

    Returns a list of ``{"skill_key", "xp_total", "new_level"}`` dicts.
    """
    raw = experience_data.get("skills_data")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, dict):
        return []

    out: list[dict] = []
    for name, values in raw.items():
        key = str(name).strip().lower()
        if key not in SKILL_NAMES:
            continue
        xp, level = 0, 0
        if isinstance(values, (list, tuple)):
            xp = _safe_int(values[0] if len(values) > 0 else 0)
            level = _safe_int(values[1] if len(values) > 1 else 0)
        elif isinstance(values, dict):
            xp = _safe_int(values.get("xp"))
            level = _safe_int(values.get("level"))
        else:
            xp = _safe_int(values)
        if xp < 0:
            continue
        out.append({"skill_key": key, "xp_total": xp, "new_level": level})
    return out


def _is_snapshot_submission(experience_data) -> bool:
    """True for full-snapshot ``experience_update`` submissions."""
    sub_type = str(experience_data.get("type") or "").strip().lower()
    return sub_type == "experience_update" or bool(experience_data.get("skills_data"))


def _is_milestone_submission(experience_data) -> bool:
    """True for post-99 XP milestone submissions.

    The plugin sends these as type ``experience_milestone`` (legacy builds:
    ``xp_milestone``) whenever a 99'd skill crosses a 1M XP boundary, with an
    ``xp_milestone_interval`` field plus per-skill ``{skill}_xp_milestone``
    fields carrying the crossed boundary.
    """
    sub_type = str(experience_data.get("type") or "").strip().lower()
    if sub_type in ("experience_milestone", "xp_milestone"):
        return True
    return "xp_milestone_interval" in experience_data


def _parse_milestone_skills(experience_data: dict) -> list[dict]:
    """Extract the milestone skills from an XP-milestone submission.

    Returns dicts of ``{"skill_key", "skill_name", "milestone_xp", "xp_total"}``
    where ``milestone_xp`` is the crossed boundary (e.g. 50_000_000).
    """
    out: list[dict] = []
    skills_trained = experience_data.get("skills_trained") or experience_data.get("skills_leveled") or ""
    for part in [p.strip() for p in str(skills_trained).split(",") if p and str(p).strip()]:
        key = part.lower()
        if key not in SKILL_NAMES:
            continue
        milestone_xp = _safe_int(experience_data.get(f"{key}_xp_milestone"), default=0)
        xp_total = _safe_int(experience_data.get(f"{key}_xp_total"), default=0)
        if milestone_xp <= 0 and xp_total > 0:
            # Legacy payloads without the explicit milestone field: floor the
            # total to the plugin's 1M reporting granularity.
            milestone_xp = xp_total - (xp_total % 1_000_000)
        if milestone_xp <= 0:
            continue
        out.append({
            "skill_key": key,
            "skill_name": key.title(),
            "milestone_xp": milestone_xp,
            "xp_total": xp_total or milestone_xp,
        })
    return out


async def _resolve_webhook_screenshot(player, experience_data, *, entry_name, subfolder):
    """Resolve a webhook-transport submission's screenshot to a public URL.

    Clients with the plugin's ``useApi`` option off — the default — submit
    through a Discord webhook, and on that transport the screenshot only ever
    arrives as a CDN link on ``attachment_url``; the two API transports save
    the file themselves and hand over a finished ``image_url``.

    XP submissions keep no per-event row to persist the URL onto
    (``PlayerExperience`` is a rolling per-player snapshot, not a log of
    level-ups), so the download's URL goes straight into the notification
    payloads and nowhere else. With no row id to name the file, the submission
    guid stands in.
    """
    return await download_webhook_screenshot(
        player,
        experience_data,
        submission_type="experience",
        entry_name=entry_name or "experience",
        entry_id=experience_data.get("guid") or uuid.uuid4().hex,
        subfolder=subfolder,
    )


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
        plugin_version = experience_data.get("p_v", None)
    
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
        is_snapshot = _is_snapshot_submission(experience_data)
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
        if not skills and not is_snapshot:
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
        apply_account_type(player, experience_data.get("account_type"))
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
    
        # Full-snapshot submissions (login/logout/world-hop/periodic event sync):
        # update stored XP for every reported skill and feed the events engine,
        # but never create level-up notifications.
        if is_snapshot:
            stage = "snapshot_parse"
            snapshot_skills = _parse_snapshot_skills(experience_data)
            if not snapshot_skills:
                return SubmissionResponse(
                    success=False,
                    message="Experience snapshot contained no valid skills."
                )

            stage = "snapshot_upsert"
            if not exp_entry:
                exp_entry = PlayerExperience(
                    player_id=player_id,
                    last_updated=datetime.now()
                )
                session.add(exp_entry)
            exp_entry.last_updated = datetime.now()
            for s in snapshot_skills:
                k = s["skill_key"]
                new_xp = s["xp_total"]
                prev_xp = getattr(exp_entry, k, 0) or 0
                if new_xp > prev_xp:
                    setattr(exp_entry, k, new_xp)

            if total_level > 0 and (not player.total_level or total_level > player.total_level):
                player.total_level = total_level

            stage = "snapshot_commit"
            session.commit()

            # Event engine hook: snapshots are what advance xp_target baselines
            # and deltas for skills that never level up (PRD D10: the first
            # report after join is baseline-only, no retroactive credit).
            try:
                from services.event_engine import queue_submission
                snapshot_world_type = str(experience_data.get("world_type") or "main")
                for s in snapshot_skills:
                    if s["xp_total"] <= 0:
                        continue
                    queue_submission(
                        "experience", player_id,
                        f"{unique_id}:{s['skill_key']}" if unique_id else None,
                        {
                            "skill": s["skill_key"],
                            "xp": s["xp_total"],
                            "level": s["new_level"],
                        },
                        world_type=snapshot_world_type, player_name=player_name,
                        # Plugin traffic regardless of intake transport (API or
                        # Discord webhook); XP has no manual submission path.
                        used_api=envelope_from_plugin(experience_data),
                    )
            except Exception:
                pass

            stage = "snapshot_done"
            return SubmissionResponse(
                success=True,
                message=f"Experience snapshot recorded ({len(snapshot_skills)} skills)."
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
    
        # Update player's total level if provided and higher. Snapshot the
        # pre-update value first: it is the baseline the total-level milestone
        # crossing test in the group loop needs, and after this block
        # player.total_level *is* the new total.
        stage = "update_player_total_level"
        previous_total_level = _safe_int(player.total_level, default=0)
        if total_level > 0 and (not player.total_level or total_level > player.total_level):
            player.total_level = total_level
            #print(f"[EXP] Updated player total level to {total_level}")
    
        stage = "commit_experience"
        session.commit()

        # Event engine hook (Task 17): gated, fire-and-forget LPUSH, one
        # envelope per reported skill (guid suffixed with the skill so the
        # completions ledger stays idempotent per skill). Runs before the
        # bulk-sync early-return so xp baselines still advance on initial
        # syncs (PRD D10: baseline-only, no retroactive credit).
        try:
            from services.event_engine import queue_submission
            for _s in real_skill_updates:
                queue_submission(
                    "experience", player_id,
                    f"{unique_id}:{_s['skill_key']}" if unique_id else None,
                    {
                        "skill": _s["skill_key"],
                        "xp": _safe_int(_s.get("xp_total")),
                        "level": _safe_int(_s.get("new_level")),
                    },
                    world_type="main", player_name=player_name,
                    # Plugin traffic regardless of intake transport (API or
                    # Discord webhook); XP has no manual submission path.
                    used_api=envelope_from_plugin(experience_data),
                )
        except Exception:
            pass

        # Post-99 XP milestone submissions: no level-up occurred, so the
        # level-notification loop below would silently skip every group
        # (new_level is 0). Handle them with their own notification type,
        # gated per group on notify_levels + post99_xp_interval.
        stage = "xp_milestone_detection"
        if _is_milestone_submission(experience_data):
            milestone_skills = _parse_milestone_skills(experience_data)
            if not milestone_skills:
                return SubmissionResponse(
                    success=True,
                    message="XP milestone recorded (no notifiable skills).",
                )

            milestone_text = ", ".join(
                f"{s['skill_name']} — {s['milestone_xp']:,} XP" for s in milestone_skills
            )

            # Resolve the webhook transport's screenshot before the group loop:
            # the URL has to reach both the notification payloads and the
            # screenshot_required() gate below, and a group that requires one
            # would otherwise skip the announcement outright.
            stage = "xp_milestone_screenshot"
            if not image_url:
                image_url = await _resolve_webhook_screenshot(
                    player,
                    experience_data,
                    entry_name=milestone_skills[0]["skill_name"],
                    subfolder="milestones",
                )

            stage = "xp_milestone_group_loop"
            for group in get_player_groups_with_global(session, player):
                await asyncio.sleep(0)  # Yield to event loop
                group_id = group.group_id

                # notify_levels is the master toggle for the level/XP family.
                level_notify_config = (
                    session.query(GroupConfiguration)
                    .filter(
                        GroupConfiguration.group_id == group_id,
                        GroupConfiguration.config_key == "notify_levels",
                    )
                    .first()
                )
                if not level_notify_config or not is_truthy_config(level_notify_config.config_value):
                    continue

                interval_config = (
                    session.query(GroupConfiguration)
                    .filter(
                        GroupConfiguration.group_id == group_id,
                        GroupConfiguration.config_key == "post99_xp_interval",
                    )
                    .first()
                )
                interval = (
                    _safe_int(getattr(interval_config, "config_value", None), default=25_000_000)
                    if interval_config
                    else 25_000_000
                )
                if interval <= 0:
                    # 0 disables post-99 XP milestone notifications for the group.
                    continue

                # The plugin reports every 1M crossing; only notify the group at
                # multiples of its configured interval (200M max XP always counts).
                qualifying = [
                    s for s in milestone_skills
                    if s["milestone_xp"] % interval == 0 or s["milestone_xp"] >= 200_000_000
                ]
                if not qualifying:
                    continue

                if await screenshot_required(session, group_id):
                    if not image_url:
                        notice = (
                            f"Your XP milestone submission did not include a screenshot "
                            f"(required for {group.group_name})."
                        )
                        continue

                qualifying_text = ", ".join(
                    f"{s['skill_name']} — {s['milestone_xp']:,} XP" for s in qualifying
                )
                notification_data = {
                    "player_name": player_name,
                    "player_id": player_id,
                    "skill_name": ", ".join(s["skill_name"] for s in qualifying),
                    "skills_names": ", ".join(s["skill_name"] for s in qualifying),
                    "skills_text": qualifying_text,
                    "new_level": "",
                    "levels_gained": "",
                    "xp_total": max(s["xp_total"] for s in qualifying),
                    "milestone_xp": max(s["milestone_xp"] for s in qualifying),
                    "skills": qualifying,
                    "total_level": total_level,
                    "total_xp": total_xp,
                    "combat_level": combat_level,
                    "image_url": image_url if image_url else "",
                    "plugin_version": plugin_version,
                }

                stage = "create_xp_milestone_notifications"
                await create_notification(
                    "xp_milestone",
                    player_id,
                    notification_data,
                    group_id,
                    existing_session=session if use_external_session else None,
                )

            # Supporter DM: reuse the dm_levels opt-in with milestone text
            # (previously these submissions produced a nonsense "Skill 0" DM).
            stage = "xp_milestone_dm"
            try:
                if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_levels"):
                    await create_notification(
                        "dm_level_up",
                        player_id,
                        {
                            "player_name": player_name,
                            "player_id": player_id,
                            "guid": unique_id,
                            "skill_name": ", ".join(s["skill_name"] for s in milestone_skills),
                            "skills_text": milestone_text,
                            "total_level": total_level,
                            "combat_level": combat_level,
                            "image_url": image_url if image_url else "",
                            "plugin_version": plugin_version,
                        },
                        existing_session=session if use_external_session else None,
                    )
            except Exception as e:
                print(f"Couldn't queue personal XP-milestone DM notification: {e}")

            return SubmissionResponse(
                success=True,
                message=f"XP milestone recorded: {milestone_text}",
                notice=notice if notice else None,
            )

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
    
        # Same resolution for the level-up path (see the milestone branch
        # above). Placed after the bulk-sync early return so an initial level
        # sync, which never notifies, does not pull an image down for nothing.
        stage = "level_up_screenshot"
        if not image_url:
            image_url = await _resolve_webhook_screenshot(
                player,
                experience_data,
                entry_name=skill_name,
                subfolder="levels",
            )

        # Create notifications for groups
        stage = "load_player_groups"
        player_groups = get_player_groups_with_global(session, player)
    
        stage = "group_loop"

        def _build_notification_data(payload_skills):
            """Assemble a level_up/total_level_milestone payload from a
            per-group-filtered skill list, so notifications only show the
            skills that actually passed that group's filters."""
            names = ", ".join(
                [s.get("skill_name", "") for s in payload_skills if s.get("skill_name")]
            ) or ""
            text = ", ".join(
                [
                    f"{s.get('skill_name')} {s.get('new_level')} (+{s.get('levels_gained')})"
                    if _safe_int(s.get("levels_gained"), default=0) > 0
                    else f"{s.get('skill_name')} {s.get('new_level')}"
                    for s in payload_skills
                    if s.get("skill_name")
                ]
            )
            return {
                "player_name": player_name,
                "player_id": player_id,
                # Legacy fields (kept for older templates)
                "skill_name": names or (skill_name or ""),
                "new_level": max(
                    (_safe_int(s.get("new_level"), default=0) for s in payload_skills),
                    default=0,
                ),
                "levels_gained": sum(
                    (_safe_int(s.get("levels_gained"), default=0) for s in payload_skills)
                ),
                "xp_total": xp_total,
                # New multi-skill fields
                "skills": payload_skills,
                "skills_names": names,
                "skills_text": text,
                "total_level": total_level,
                "total_xp": total_xp,
                "combat_level": combat_level,
                "image_url": image_url if image_url else "",
                "plugin_version": plugin_version,
            }

        # Submission-level text for the plugin response (per-group payloads
        # below only contain each group's qualifying skills).
        submission_skills_text = _build_notification_data(skills).get("skills_text", "")

        for group in player_groups:
            await asyncio.sleep(0)  # Yield to event loop
            group_id = group.group_id

            # TODO -- LOCK THIS BEHIND PREMIUM FEATURES
            # if not is_feature_active_for_group(group_id=group_id, feature_key='level_notifications', session=session):
            #     continue

            # Fetch the level-notification config family in one query.
            config_rows = (
                session.query(GroupConfiguration)
                .filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key.in_(
                        [
                            "notify_levels",
                            "level_minimum_for_notifications",
                            "level_increment",
                            "notify_virtual_levels",
                            "notify_combat_levels",
                            "level_milestones",
                            "level_milestones_to_notify",
                        ]
                    ),
                )
                .all()
            )
            cfg = {row.config_key: row.config_value for row in config_rows}

            # notify_levels is the master toggle for the level/XP family.
            if not is_truthy_config(cfg.get("notify_levels")):
                continue

            minimum_level_to_notify = _safe_int(
                cfg.get("level_minimum_for_notifications"), default=1
            )
            if minimum_level_to_notify < 1:
                minimum_level_to_notify = 1
            level_increment = _safe_int(cfg.get("level_increment"), default=1)
            if level_increment < 1:
                level_increment = 1
            notify_virtual_levels = is_truthy_config(cfg.get("notify_virtual_levels"))
            notify_combat_levels = is_truthy_config(cfg.get("notify_combat_levels"))
            is_total_milestone = _crosses_total_milestone(
                previous_total_level, total_level, _milestone_levels_for_group(cfg)
            )

            # Per-skill filtering: only skills that individually pass this
            # group's filters appear in its notification. Previously one
            # qualifying skill dragged every other skill in the submission
            # (pre-99 tag-alongs, combat levels) into the announcement.
            qualifying_skills = [
                s
                for s in skills
                if _skill_qualifies_for_group(
                    s,
                    minimum_level=minimum_level_to_notify,
                    level_increment=level_increment,
                    notify_virtual_levels=notify_virtual_levels,
                    notify_combat_levels=notify_combat_levels,
                )
            ]

            # Context skills for a total-level milestone: every skill in the
            # submission the group agreed to see, min/increment filters aside
            # (the milestone is its own event, so a below-minimum skill still
            # belongs on its context line). A virtual level cannot contribute
            # to a total-level milestone in the first place — total counts real
            # levels only — and naming one here was how >99 levels reached
            # groups with notify_virtual_levels off: the milestone renders with
            # the group's level_up embed template, so in Discord it is
            # indistinguishable from a level-up notification.
            #
            # A crossing is always driven by a real sub-100 level, so this is
            # normally non-empty; when it isn't, the crossing cannot be
            # attributed to anything showable and an empty announcement is
            # worse than none.
            milestone_skills = (
                [
                    s
                    for s in skills
                    if _skill_visible_to_group(
                        s,
                        notify_virtual_levels=notify_virtual_levels,
                        notify_combat_levels=notify_combat_levels,
                    )
                ]
                if is_total_milestone
                else []
            )
            is_total_milestone = bool(milestone_skills)

            if not qualifying_skills and not is_total_milestone:
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

            stage = "create_group_notifications"
            if qualifying_skills:
                await create_notification(
                    "level_up",
                    player_id,
                    _build_notification_data(qualifying_skills),
                    group_id,
                    existing_session=session if use_external_session else None,
                )

            if is_total_milestone:
                # Milestone total levels notify regardless of the min/increment
                # filters, on the group-visible skills built above.
                await create_notification(
                    "total_level_milestone",
                    player_id,
                    _build_notification_data(milestone_skills),
                    group_id,
                    existing_session=session if use_external_session else None,
                )

        # Personal submission DM (supporter perk): queued once per level-up,
        # OUTSIDE the group loop — group notify/min-level/screenshot criteria
        # must not gate or duplicate a personal DM (same fix as drops,
        # c258115). Entitlement + opt-in are re-checked at send time.
        stage = "create_dm_notification"
        try:
            if player and player.user and is_user_dm_enabled(session, player.user_id, "dm_levels"):
                dm_skills_text = ", ".join(
                    [
                        f"{s.get('skill_name')} {s.get('new_level')} (+{s.get('levels_gained')})"
                        if _safe_int(s.get("levels_gained"), default=0) > 0
                        else f"{s.get('skill_name')} {s.get('new_level')}"
                        for s in skills
                        if s.get("skill_name")
                    ]
                )
                await create_notification(
                    "dm_level_up",
                    player_id,
                    {
                        "player_name": player_name,
                        "player_id": player_id,
                        "guid": unique_id,
                        "skill_name": skill_name or "",
                        "skills_text": dm_skills_text,
                        "total_level": total_level,
                        "combat_level": combat_level,
                        "image_url": image_url if image_url else "",
                        "plugin_version": plugin_version,
                    },
                    existing_session=session if use_external_session else None,
                )
        except Exception as e:
            print(f"Couldn't queue personal level-up DM notification: {e}")

        stage = "done"
        #print(f"[EXP] === EXPERIENCE PROCESSOR END ===")
        summary = submission_skills_text
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