"""Typed group-configuration registry (Task 05, FRONTEND_PLAN.md §11.1).

A faithful Python port of the shared TypeScript registry
(``packages/api-types/src/group-config.ts``) — the single validation authority
for the 55+ ``group_configurations`` keys. The typed ``GET/PATCH
/api/v1/groups/{id}/config`` endpoints validate against this; the legacy
``load_config`` used by the RuneLite plugin is untouched.

A parity test (``tests/unit/test_group_config_registry.py``) asserts this key
set matches the TS ``allConfigKeys()``.

Edge case (matches the TS ``getConfigField``): ``seasonal_boards`` is a real base
key that starts with ``seasonal_`` and is NOT a mirror — resolve exact keys
before stripping the prefix.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

SEASONAL_PREFIX = "seasonal_"

# type ∈ channel | boolean | int | string | text | csv | bosslist | select
# (bosslist stores a comma-separated boss-name list like csv; the frontend
# renders it as a picker backed by GET /groups/{id}/pb-bosses.)
GROUP_CONFIG_FIELDS: List[Dict[str, Any]] = [
    # --- Channels ---
    # NOTE: notification routing reads the channel_id_to_post_* keys (see
    # services/notification_service.py). The registry previously used
    # *_channel_id names that nothing consumed; migration web20a copied any
    # values groups saved under those dead keys into the canonical ones.
    {"key": "channel_id_to_post_loot", "type": "channel", "default": None},
    {"key": "lootboard_channel_id", "type": "channel", "default": None},
    {"key": "lootboard_message_id", "type": "string", "default": None},
    {"key": "channel_id_to_post_levels", "type": "channel", "default": None},
    {"key": "channel_id_to_post_pb", "type": "channel", "default": None},
    {"key": "channel_id_to_post_ca", "type": "channel", "default": None},
    {"key": "channel_id_to_post_pets", "type": "channel", "default": None},
    {"key": "channel_id_to_post_quests", "type": "channel", "default": None},
    {"key": "channel_id_to_post_clog", "type": "channel", "default": None},
    {"key": "channel_id_to_post_deaths", "type": "channel", "default": None},
    {"key": "channel_id_to_post_diaries", "type": "channel", "default": None},
    {"key": "announcements_channel_id", "type": "channel", "default": None},

    # --- Drop notifications ---
    # Defaults here must match the runtime fallbacks the processors use when a
    # key is absent (data/submissions/drop.py), otherwise the editor shows one
    # behavior and the bot does another.
    {"key": "minimum_value_to_notify", "type": "int", "default": 2500000, "min": 0},
    {"key": "only_include_items_over_minimum", "type": "boolean", "default": False, "seasonal": True},
    {"key": "only_send_messages_with_images", "type": "boolean", "default": False, "seasonal": True},
    {"key": "send_stacks_of_items", "type": "boolean", "default": False, "seasonal": True},
    {"key": "notify_clogs", "type": "boolean", "default": True, "seasonal": True},
    {"key": "notify_cas", "type": "boolean", "default": True, "seasonal": True},
    {"key": "notify_pets", "type": "boolean", "default": True, "seasonal": True},
    {"key": "notify_quests", "type": "boolean", "default": False, "seasonal": True},
    {"key": "notify_special_quests", "type": "boolean", "default": True, "seasonal": True},
    {"key": "notify_deaths", "type": "boolean", "default": False, "seasonal": True},
    {"key": "notify_diaries", "type": "boolean", "default": False, "seasonal": True},

    # --- Level notifications ---
    # notify_levels is the master toggle for the whole level/XP family
    # (level-ups, total-level milestones, post-99 XP milestones).
    {"key": "notify_levels", "type": "boolean", "default": False, "seasonal": True},
    {"key": "level_minimum_for_notifications", "type": "int", "default": 1, "min": 1, "max": 99},
    {"key": "level_increment", "type": "int", "default": 1, "min": 1, "max": 99},
    # TOTAL-level milestones (e.g. 1500,2000,2277) that always notify.
    {"key": "level_milestones", "type": "csv", "default": ""},
    # Post-99 XP notification interval; 0 disables. Plugin reports at 1M
    # granularity, so values should be multiples of 1,000,000.
    {"key": "post99_xp_interval", "type": "int", "default": 25000000, "min": 0},

    # --- Personal best ---
    # notify_pbs (PB Discord notifications) is available to every group. The
    # Hall of Fame keys below are premium (see HALL_OF_FAME_CONFIG_KEYS);
    # create_pb_embeds is the master on/off switch the HOF bot keys off of.
    {"key": "notify_pbs", "type": "boolean", "default": True, "seasonal": True},
    {"key": "create_pb_embeds", "type": "boolean", "default": False},
    {"key": "personal_best_embed_boss_list", "type": "bosslist", "default": ""},
    {"key": "number_of_pbs_to_display", "type": "int", "default": 5, "min": 1, "max": 10},
    {"key": "channel_id_to_send_pb_embeds", "type": "channel", "default": None},
    {"key": "hof_individual_boss_messages", "type": "boolean", "default": False},

    # --- Combat achievements ---
    {
        "key": "min_ca_tier_to_notify",
        "type": "select",
        "default": "EASY",
        "options": ["EASY", "MEDIUM", "HARD", "ELITE", "MASTER", "GRANDMASTER"],
        "seasonal": True,
    },

    # --- Board settings ---
    # boardstyle: a lootboards-table row id chosen via the preview picker
    # (GET /lootboard-styles). Existence is validated in the PATCH route —
    # the catalog lives in the DB, not this static registry.
    {"key": "loot_board_type", "type": "boardstyle", "default": "1"},
    {"key": "use_dynamic_colors", "type": "boolean", "default": True},
    {"key": "use_gp_colors", "type": "boolean", "default": True},
    {"key": "repost_lootboard", "type": "boolean", "default": False},
    {"key": "seasonal_boards", "type": "boolean", "default": False},

    # --- Split tracking (GP only; point splitting lives in the points routes) ---
    {"key": "split_gp_tracking", "type": "boolean", "default": False},

    # --- Manual submissions (suggestion #45) ---
    # How website manual submissions count for THIS group (never affects
    # global tracking or other groups):
    #   allow           — count immediately (default / legacy behavior)
    #   authorized_only — only group admins/authorized users' manual
    #                     submissions count; everyone else's are excluded
    #                     from this group's boards & notifications
    #   block           — no manual submission ever counts for this group
    {
        "key": "manual_submission_policy",
        "type": "select",
        "default": "allow",
        "options": ["allow", "confirm", "authorized_only", "block"],
    },
    # Optional: channel for the "manual submission awaiting review" ping under
    # the 'confirm' policy. Unset => no Discord ping (web review queue only).
    {"key": "channel_id_to_post_manual_review", "type": "channel", "default": None},

    # --- Member activity log + voice-channel stat displays ---
    # channel_id_to_send_logs: member join/leave embeds (db/ops.py notify_group).
    # vc_to_display_*: voice channels renamed every 10 min with live stats
    # (services/channel_names.py). The channel picker's manual-id entry is how
    # voice channels are selected (the guild channel cache is text-only).
    {"key": "channel_id_to_send_logs", "type": "channel", "default": None},
    {"key": "vc_to_display_monthly_loot", "type": "channel", "default": None},
    {"key": "vc_to_display_monthly_loot_text", "type": "string", "default": "{month}: {gp_amount} gp"},
    {"key": "vc_to_display_droptracker_users", "type": "channel", "default": None},
    {"key": "vc_to_display_droptracker_users_text", "type": "string", "default": "{member_count} members"},

    # --- Misc / integration ---
    {"key": "group_name", "type": "string", "default": ""},
    {"key": "group_description", "type": "text", "default": ""},
    {"key": "clan_chat_name", "type": "string", "default": ""},
    {"key": "discord_url", "type": "string", "default": ""},
    {"key": "auto_provision_members", "type": "boolean", "default": False},
    {"key": "export_api_key", "type": "string", "default": None},
]

_BY_KEY = {f["key"]: f for f in GROUP_CONFIG_FIELDS}

# Sensitive keys never returned to non-admins (the endpoint is admin-gated, but
# guard explicitly per §11 / Task 05).
SENSITIVE_KEYS = {"export_api_key"}


def all_config_keys() -> List[str]:
    """All effective keys, including seasonal mirrors (mirrors TS allConfigKeys)."""
    keys = [f["key"] for f in GROUP_CONFIG_FIELDS]
    seasonal = [f"{SEASONAL_PREFIX}{f['key']}" for f in GROUP_CONFIG_FIELDS if f.get("seasonal")]
    return keys + seasonal


def get_config_field(key: str) -> Optional[Dict[str, Any]]:
    """Resolve a key (base or seasonal mirror) to its field, exact-match first."""
    exact = _BY_KEY.get(key)
    if exact:
        return exact
    if key.startswith(SEASONAL_PREFIX):
        base = key[len(SEASONAL_PREFIX):]
        return _BY_KEY.get(base)
    return None


# --------------------------------------------------------------------------- #
# Coercion (storage is text; the API returns/accepts registry-typed values).
# --------------------------------------------------------------------------- #
def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def coerce_from_storage(field: Dict[str, Any], stored: Optional[str]) -> Any:
    """Convert a stored text value to the registry-typed JSON value."""
    if stored is None:
        return field.get("default")
    ftype = field["type"]
    if ftype == "boolean":
        return _is_truthy(stored)
    if ftype == "int":
        try:
            value = int(float(stored))
        except (ValueError, TypeError):
            return field.get("default")
        # Out-of-range stored values are legacy sentinels (e.g. the template
        # group's number_of_pbs_to_display='0' means "unset"). The editor
        # can't re-save them anyway (PATCH enforces min/max), so surface the
        # effective default instead of an invalid number.
        if ("min" in field and value < field["min"]) or ("max" in field and value > field["max"]):
            return field.get("default")
        return value
    # channel / string / text / csv / bosslist / select -> string
    return str(stored)


class ConfigValidationError(ValueError):
    def __init__(self, key: str, detail: str):
        super().__init__(detail)
        self.key = key
        self.detail = detail


def coerce_to_storage(key: str, value: Any) -> str:
    """Validate a client value against the registry and return its text form
    for ``group_configurations.config_value``. Raises ConfigValidationError."""
    field = get_config_field(key)
    if field is None:
        raise ConfigValidationError(key, f"Unknown config key '{key}'.")

    ftype = field["type"]

    if ftype == "boolean":
        if not isinstance(value, bool):
            raise ConfigValidationError(key, f"'{key}' must be a boolean.")
        return "1" if value else "0"

    if ftype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            # accept numeric strings too
            try:
                value = int(value)
            except (ValueError, TypeError):
                raise ConfigValidationError(key, f"'{key}' must be an integer.")
        if "min" in field and value < field["min"]:
            raise ConfigValidationError(key, f"'{key}' must be >= {field['min']}.")
        if "max" in field and value > field["max"]:
            raise ConfigValidationError(key, f"'{key}' must be <= {field['max']}.")
        return str(value)

    if ftype == "select":
        sval = str(value)
        if sval not in (field.get("options") or []):
            raise ConfigValidationError(
                key, f"'{key}' must be one of {field.get('options')}."
            )
        return sval

    # channel / string / text / csv / bosslist
    if value is None:
        return ""
    if not isinstance(value, (str, int)):
        raise ConfigValidationError(key, f"'{key}' must be a string.")
    return str(value)
