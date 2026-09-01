"""Typed group-configuration registry (Task 05, FRONTEND_PLAN.md §11.1).

A faithful Python port of the shared TypeScript registry
(``packages/api-types/src/group-config.ts``) — the single validation authority
for the 55+ ``group_configurations`` keys. The typed ``GET/PATCH
/api/v1/groups/{id}/config`` endpoints validate against this; the legacy
``load_config`` used by the RuneLite plugin is untouched.

Each field also carries the human-readable metadata (``label``, ``category``,
``help``) copied VERBATIM from the TS registry so Discord-native surfaces can
render the same wording as the web editor. ``CONFIG_CATEGORIES`` mirrors the
TS constant of the same name. Keep both registries in sync when editing.

A parity test (``tests/unit/test_group_config_registry.py``) asserts this key
set matches the TS ``allConfigKeys()``.

Edge case (matches the TS ``getConfigField``): ``seasonal_boards`` is a real base
key that starts with ``seasonal_`` and is NOT a mirror — resolve exact keys
before stripping the prefix. Seasonal mirrors resolve to their base field, so
they inherit the base key's label/category/help.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

SEASONAL_PREFIX = "seasonal_"

# Ordered category list (mirrors the TS CONFIG_CATEGORIES; TS calls the key
# field `id`).
CONFIG_CATEGORIES: List[Dict[str, str]] = [
    {"key": "channels", "label": "Channels"},
    {"key": "drops", "label": "Drop notifications"},
    {"key": "deaths", "label": "Deaths"},
    {"key": "levels", "label": "Level notifications"},
    {"key": "milestones", "label": "Milestones"},
    {"key": "pbs", "label": "Personal best"},
    {"key": "cas", "label": "Combat achievements"},
    {"key": "board", "label": "Lootboard"},
    {"key": "recaps", "label": "Monthly recaps"},
    {"key": "clan_log", "label": "Clan Log"},
    {"key": "integration", "label": "Integration & info"},
]

# type ∈ channel | boolean | int | string | text | csv | bosslist | select
# (bosslist stores a comma-separated boss-name list like csv; the frontend
# renders it as a picker backed by GET /groups/{id}/pb-bosses.)
GROUP_CONFIG_FIELDS: List[Dict[str, Any]] = [
    # --- Channels ---
    # NOTE: notification routing reads the channel_id_to_post_* keys (see
    # services/notification_service.py). The registry previously used
    # *_channel_id names that nothing consumed; migration web20a copied any
    # values groups saved under those dead keys into the canonical ones.
    {
        "key": "channel_id_to_post_loot",
        "label": "Drops channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel where drop notifications are posted.",
        "default": None,
    },
    {
        "key": "lootboard_channel_id",
        "label": "Lootboard channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel where the lootboard image is posted/updated.",
        "default": None,
    },
    {
        "key": "lootboard_message_id",
        "label": "Lootboard message id",
        "category": "channels",
        "type": "string",
        "help": "Message the bot edits when reposting the board. Managed automatically.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_levels",
        "label": "Levels channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for level-up notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_pb",
        "label": "Personal best channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for personal-best notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_ca",
        "label": "Combat achievements channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for combat-achievement notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_pets",
        "label": "Pets channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for pet notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_quests",
        "label": "Quests channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for quest-completion notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_clog",
        "label": "Collection log channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for collection-log notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_diaries",
        "label": "Diaries channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for achievement-diary notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_kc",
        "label": "KC milestones channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for kill-count milestone notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "channel_id_to_post_ranks",
        "label": "Rank milestones channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel for hiscores-rank milestone notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "announcements_channel_id",
        "label": "Announcements channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel where published announcements are syndicated (FRONTEND_PLAN.md §10).",
        "default": None,
    },
    # Channel for the standing "Open DropTracker" card (the Discord Activity
    # launcher). The bot posts/moves/removes it as this changes; the message id
    # it manages lives in the un-registered `activity_launch_message_id` row.
    {
        "key": "activity_launch_channel",
        "label": "Activity launcher channel",
        "category": "channels",
        "type": "channel",
        "help": "Post an “Open DropTracker” card in this channel with a button that opens the in-Discord app. The bot keeps one card here and moves or removes it when you change this.",
        "default": None,
    },

    # --- Drop notifications ---
    # Defaults here must match the runtime fallbacks the processors use when a
    # key is absent (data/submissions/drop.py), otherwise the editor shows one
    # behavior and the bot does another.
    {
        "key": "minimum_value_to_notify",
        "label": "Minimum value to notify",
        "category": "drops",
        "type": "int",
        "help": "Suppress drop notifications below this GP value.",
        "default": 2500000,
        "min": 0,
    },
    {
        "key": "only_include_items_over_minimum",
        "label": "Only items over minimum",
        "category": "drops",
        "type": "boolean",
        "help": "On stacked/multi-item drops, only include items above the minimum value.",
        "default": False,
        "seasonal": True,
    },
    {
        "key": "only_send_messages_with_images",
        "label": "Only send with images",
        "category": "drops",
        "type": "boolean",
        "help": "Require a screenshot before posting a drop.",
        "default": False,
        "seasonal": True,
    },
    {
        "key": "send_stacks_of_items",
        "label": "Announce item stacks",
        "category": "drops",
        "type": "boolean",
        "help": "Announce drops of stackable items (e.g. rune/coin stacks) when their total value passes the minimum.",
        "default": False,
        "seasonal": True,
    },
    {
        "key": "notify_clogs",
        "label": "Notify collection logs",
        "category": "drops",
        "type": "boolean",
        "help": "Post a notification on new collection-log slots.",
        "default": True,
        "seasonal": True,
    },
    {
        "key": "notify_cas",
        "label": "Notify combat achievements",
        "category": "drops",
        "type": "boolean",
        "help": "Post a notification on combat-achievement completions.",
        "default": True,
        "seasonal": True,
    },
    {
        "key": "notify_pets",
        "label": "Notify pets",
        "category": "drops",
        "type": "boolean",
        "help": "Post a notification on pet drops.",
        "default": True,
        "seasonal": True,
    },
    {
        "key": "notify_quests",
        "label": "Notify quests",
        "category": "drops",
        "type": "boolean",
        "help": "Post a notification on quest completions.",
        "default": False,
        "seasonal": True,
    },
    {
        "key": "notify_special_quests",
        "label": "Notify special quests",
        "category": "drops",
        "type": "boolean",
        "help": "Notify on milestone/special quests even when general quest notifications are off.",
        "default": True,
        "seasonal": True,
    },
    {
        "key": "notify_diaries",
        "label": "Notify achievement diaries",
        "category": "drops",
        "type": "boolean",
        "help": "Post a notification on achievement-diary completions.",
        "default": False,
        "seasonal": True,
    },
    # Announce anything that awarded group points but wouldn't have been
    # announced by the group's other settings (below-minimum drop, notify_*
    # toggle off, CA tier below minimum). Opt-in (default OFF): the default
    # point template awards for every clog/PB/CA, so a default-ON override
    # silently discarded the notify_* toggles of every paid group (2026-09-01).
    # Only groups with the point system active ever award points, so this is
    # inert for everyone else. Not seasonal: group points are main-world only.
    # Read by data/submissions/* via common.points_notify_enabled (absent row
    # = disabled).
    {
        "key": "notify_points_awarded",
        "label": "Announce point awards",
        "category": "drops",
        "type": "boolean",
        "help": "When your point system awards points for a submission that wouldn't be announced on its own — a drop below the minimum value, a notification type you've turned off, or a combat achievement below your tier minimum — announce it anyway. Off by default; with the default point rules this posts every collection log, personal best and combat achievement. Screenshot requirements still apply.",
        "default": False,
    },

    # --- Deaths ---
    # Death notifications get their own section: toggle, channel and the
    # custom message variants (randomized clan-broadcast-style death lines).
    # death_message_variants is a JSON string array stored via LONG_VALUE_KEYS
    # (web_api/routes/config.py) so lists past 255 chars spill into long_value.
    {
        "key": "notify_deaths",
        "label": "Notify deaths",
        "category": "deaths",
        "type": "boolean",
        "help": "Post a notification when a member dies.",
        "default": False,
        "seasonal": True,
    },
    {
        "key": "notify_deaths_safe",
        "label": "Notify safe deaths",
        "category": "deaths",
        "type": "boolean",
        "help": (
            "Also announce deaths that cost nothing — raid and Gauntlet wipes, "
            "Castle Wars, Soul Wars, Barbarian Assault, Nightmare Zone, your own "
            "house. Off by default so the deaths channel shows the ones that hurt. "
            "Inferno and Fight Caves always count as real deaths regardless of this "
            "setting; mute those by blacklisting the region instead."
        ),
        "default": False,
        "seasonal": True,
    },
    {
        "key": "channel_id_to_post_deaths",
        "label": "Deaths channel",
        "category": "deaths",
        "type": "channel",
        "help": "Channel for player-death notifications. Falls back to the drops channel when unset.",
        "default": None,
    },
    {
        "key": "death_message_variants",
        "label": "Death messages",
        "category": "deaths",
        "type": "messagelist",
        "help": "Custom death messages, one picked at random per death — like the in-game clan broadcasts. Placeholders like {player_name}, {source}, {region_name} and {value_lost} are filled in. {value_lost} is blank for members on a plugin older than 6.0.4, which sends no value. Leave empty for the default message. Groups using a Components layout for deaths keep their layout; these messages don't apply there.",
        "default": "",
    },
    {
        "key": "death_message_as_embed_description",
        "label": "Show message inside the embed",
        "category": "deaths",
        "type": "boolean",
        "help": "On: the picked message replaces the embed description (including a custom embed's). Off: it's sent as the plain message text above the embed.",
        "default": False,
    },

    # --- Level notifications ---
    # notify_levels is the master toggle for the whole level/XP family
    # (level-ups, total-level milestones, post-99 XP milestones).
    {
        "key": "notify_levels",
        "label": "Notify levels",
        "category": "levels",
        "type": "boolean",
        "help": "Master toggle for level-up, total-level milestone, and post-99 XP milestone notifications.",
        "default": False,
        "seasonal": True,
    },
    {
        "key": "level_minimum_for_notifications",
        "label": "Minimum level",
        "category": "levels",
        "type": "int",
        "help": "Only notify for skill levels at or above this value. Set to 99 (with the toggles below off) to only announce 99s.",
        "default": 1,
        "min": 1,
        "max": 99,
    },
    {
        "key": "level_increment",
        "label": "Level increment",
        "category": "levels",
        "type": "int",
        "help": "Notify every N skill levels (1 = every level). Level 99 always notifies. In a multi-level jump, every crossed level is checked.",
        "default": 1,
        "min": 1,
        "max": 99,
    },
    # Virtual (100-126) skill level-ups. Off by default so level 99 is the
    # final per-skill level-up notification unless a group opts in.
    {
        "key": "notify_virtual_levels",
        "label": "Virtual levels (100+)",
        "category": "levels",
        "type": "boolean",
        "help": "Also notify for virtual level-ups above 99 (levels 100–126). Off = level 99 is the final level-up notification for a skill.",
        "default": False,
    },
    # Combat level increases. The plugin reports these as level-ups; they are
    # their own opt-in family and ignore the min/increment skill filters.
    {
        "key": "notify_combat_levels",
        "label": "Combat level-ups",
        "category": "levels",
        "type": "boolean",
        "help": "Notify when a member's combat level increases. Combat levels ignore the minimum/increment filters above.",
        "default": False,
    },
    # TOTAL-level milestones (e.g. 1500,2000,2277) that always notify.
    {
        "key": "level_milestones",
        "label": "Total level milestones",
        "category": "levels",
        "type": "csv",
        "help": "Comma-separated TOTAL levels that always notify (e.g. 1500,2000,2277).",
        "default": "",
    },
    # Post-99 XP notification interval; 0 disables. Plugin reports at 1M
    # granularity, so values should be multiples of 1,000,000.
    {
        "key": "post99_xp_interval",
        "label": "Post-99 XP interval",
        "category": "levels",
        "type": "int",
        "help": "After a skill reaches 99, notify every N XP (e.g. 25m = every 25M). Multiples of 1M; 0 disables.",
        "default": 25000000,
        "min": 0,
    },

    # --- Milestones (KC + hiscores rank) ---
    # KC milestones are fed from the plugin's per-kill KC reports (drops and
    # timed kills), WOM-recognized bosses only; rank milestones from the
    # periodic WiseOldMan bulk-hiscores sweep (services/rank_milestones.py).
    # Neither family is seasonal: drops.kill_count is main-world only and WOM
    # mirrors the main-game hiscores.
    {
        "key": "notify_kc_milestones",
        "label": "Notify KC milestones",
        "category": "milestones",
        "type": "boolean",
        "help": "Master toggle for boss kill-count milestone notifications (first kill and every-Nth-kill).",
        "default": False,
    },
    {
        "key": "notify_first_kc",
        "label": "Announce first kills",
        "category": "milestones",
        "type": "boolean",
        "help": "Announce a member's first kill of a boss. Only applies while KC milestones are enabled.",
        "default": True,
    },
    {
        "key": "kc_milestone_interval",
        "label": "KC milestone interval",
        "category": "milestones",
        "type": "int",
        "help": "Announce every Nth kill of a boss (e.g. 100 or 1000). 0 disables interval announcements (first kills can still announce).",
        "default": 100,
        "min": 0,
        "max": 50000,
    },
    {
        "key": "notify_rank_milestones",
        "label": "Notify rank milestones",
        "category": "milestones",
        "type": "boolean",
        "help": "Announce when a member's hiscores rank enters a configured threshold (e.g. top 10,000) on a boss, skill or clue tier. Checked periodically via WiseOldMan.",
        "default": False,
    },
    {
        "key": "rank_milestone_thresholds",
        "label": "Rank thresholds",
        "category": "milestones",
        "type": "csv",
        "help": "Comma-separated rank thresholds to announce on entering (e.g. 10000,5000,1000). Only the deepest newly-entered threshold announces.",
        "default": "10000,5000,1000",
    },
    {
        "key": "rank_milestone_bosses",
        "label": "Boss ranks",
        "category": "milestones",
        "type": "boolean",
        "help": "Include boss kill-count ranks in rank milestone checks.",
        "default": True,
    },
    {
        "key": "rank_milestone_skills",
        "label": "Skill ranks",
        "category": "milestones",
        "type": "boolean",
        "help": "Include skill XP ranks in rank milestone checks.",
        "default": True,
    },
    {
        "key": "rank_milestone_clues",
        "label": "Clue ranks",
        "category": "milestones",
        "type": "boolean",
        "help": "Include clue scroll completion ranks in rank milestone checks.",
        "default": True,
    },

    # --- Personal best ---
    # notify_pbs (PB Discord notifications) is available to every group. The
    # Hall of Fame keys below are premium (see HALL_OF_FAME_CONFIG_KEYS);
    # create_pb_embeds is the master on/off switch the HOF bot keys off of.
    {
        "key": "notify_pbs",
        "label": "Notify personal bests",
        "category": "pbs",
        "type": "boolean",
        "help": "Post personal-best notifications in Discord. Available to all groups.",
        "default": True,
        "seasonal": True,
    },
    {
        "key": "create_pb_embeds",
        "label": "Enable Hall of Fame",
        "category": "pbs",
        "type": "boolean",
        "help": "Post and keep updated the Hall of Fame personal-best leaderboards in Discord. Turn this on, then choose the bosses and channel below.",
        "default": False,
    },
    {
        "key": "personal_best_embed_boss_list",
        "label": "Hall of Fame bosses",
        "category": "pbs",
        "type": "bosslist",
        "help": "Bosses featured in the Hall of Fame. Empty = no bosses shown.",
        "default": "",
    },
    {
        "key": "number_of_pbs_to_display",
        "label": "PBs to display",
        "category": "pbs",
        "type": "int",
        "help": "Top PB entries shown per team-size bracket in Hall of Fame messages.",
        "default": 5,
        "min": 1,
        "max": 10,
    },
    {
        "key": "channel_id_to_send_pb_embeds",
        "label": "Hall of Fame channel",
        "category": "pbs",
        "type": "channel",
        "help": "Channel where the Hall of Fame leaderboards are posted.",
        "default": None,
    },
    {
        "key": "hof_individual_boss_messages",
        "label": "Individual Hall of Fame messages",
        "category": "pbs",
        "type": "boolean",
        "help": "Post one Hall of Fame message per boss. When off, only the directory message is posted and members use its drop-down to view each boss's leaderboard.",
        "default": False,
    },

    # --- Combat achievements ---
    {
        "key": "min_ca_tier_to_notify",
        "label": "Minimum CA tier",
        "category": "cas",
        "type": "select",
        "help": "Lowest combat-achievement tier that triggers a notification.",
        "default": "EASY",
        "options": ["EASY", "MEDIUM", "HARD", "ELITE", "MASTER", "GRANDMASTER"],
        "seasonal": True,
    },

    # --- Achievement diaries ---
    # (TS categorises this under "drops".)
    {
        "key": "min_diary_tier_to_notify",
        "label": "Minimum diary tier",
        "category": "drops",
        "type": "select",
        "help": "Lowest achievement-diary tier that triggers a notification.",
        "default": "EASY",
        "options": ["EASY", "MEDIUM", "HARD", "ELITE"],
        "seasonal": True,
    },

    # --- Board settings ---
    # boardstyle: a lootboards-table row id chosen via the preview picker
    # (GET /lootboard-styles). Existence is validated in the PATCH route —
    # the catalog lives in the DB, not this static registry.
    {
        "key": "loot_board_type",
        "label": "Lootboard style",
        "category": "board",
        "type": "boardstyle",
        "help": "Visual style of the generated lootboard. Browse the catalog with live previews.",
        "default": "1",
    },
    {
        "key": "use_dynamic_colors",
        "label": "Dynamic colors",
        "category": "board",
        "type": "boolean",
        "help": "Color item tiles by relative value.",
        "default": True,
    },
    {
        "key": "use_gp_colors",
        "label": "GP colors",
        "category": "board",
        "type": "boolean",
        "help": "Use GP-value color thresholds on the board.",
        "default": True,
    },
    {
        "key": "repost_lootboard",
        "label": "Repost lootboard",
        "category": "board",
        "type": "boolean",
        "help": "Repost (vs. edit) the board on each update.",
        "default": False,
    },
    {
        "key": "seasonal_boards",
        "label": "Seasonal boards",
        "category": "board",
        "type": "boolean",
        "help": "When enabled, automatically use themed boards for holidays/seasons when made available globally.",
        "default": False,
    },

    # --- Split tracking (GP only; point splitting lives in the points routes) ---
    {
        "key": "split_gp_tracking",
        "label": "Split GP tracking",
        "category": "drops",
        "type": "boolean",
        "help": "Track raid loot splits: members receive their share of a split drop's GP value instead of the receiver keeping the full amount. Point splitting is configured separately on the Points tab.",
        "default": False,
    },

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
        "label": "Manual submissions",
        "category": "drops",
        "type": "select",
        "help": "How drops submitted manually on the website count for this group. They always count globally and for the player's other groups — this only controls this group's boards and notifications.",
        "default": "allow",
        "options": ["allow", "confirm", "authorized_only", "block"],
    },
    # Optional: channel for the "manual submission awaiting review" ping under
    # the 'confirm' policy. Unset => no Discord ping (web review queue only).
    {
        "key": "channel_id_to_post_manual_review",
        "label": "Manual review channel",
        "category": "channels",
        "type": "channel",
        "help": "Optional. Where to ping when a manual submission is held for approval (the \"Hold for admin approval\" policy). Leave unset to review only on the website.",
        "default": None,
    },

    # --- Member activity log + voice-channel stat displays ---
    # channel_id_to_send_logs: member join/leave embeds (db/ops.py notify_group).
    # vc_to_display_*: voice channels renamed every 10 min with live stats
    # (services/channel_names.py). The channel picker's manual-id entry is how
    # voice channels are selected (the guild channel cache is text-only).
    {
        "key": "channel_id_to_send_logs",
        "label": "Member log channel",
        "category": "channels",
        "type": "channel",
        "help": "Channel where member join/leave log messages are posted. Leave unset to disable.",
        "default": None,
    },
    {
        "key": "vc_to_display_monthly_loot",
        "label": "Monthly loot voice channel",
        "category": "integration",
        "type": "channel",
        "help": "Voice channel renamed every 10 minutes to show the group's monthly loot total. Nobody needs to be able to talk in it — the name is the display. Give the bot Manage Channel on it.",
        "default": None,
    },
    {
        "key": "vc_to_display_monthly_loot_text",
        "label": "Monthly loot channel text",
        "category": "integration",
        "type": "string",
        "help": "Template for the loot voice channel name. Placeholders: {month}, {gp_amount}.",
        "default": "{month}: {gp_amount} gp",
    },
    {
        "key": "vc_to_display_droptracker_users",
        "label": "Member count voice channel",
        "category": "integration",
        "type": "channel",
        "help": "Voice channel renamed every 10 minutes to show the group's tracked member count. Nobody needs to be able to talk in it — the name is the display. Give the bot Manage Channel on it.",
        "default": None,
    },
    {
        "key": "vc_to_display_droptracker_users_text",
        "label": "Member count channel text",
        "category": "integration",
        "type": "string",
        "help": "Template for the member-count voice channel name. Placeholder: {member_count}.",
        "default": "{member_count} members",
    },

    # --- Misc / integration ---
    # Not a setting of its own: this is an editable view of groups.group_name,
    # the column every surface displays. PATCH routes it through
    # db/group_rename.py; max_length matches that VARCHAR(30) column.
    {
        "key": "group_name",
        "label": "Group name",
        "category": "integration",
        "type": "string",
        "help": "Display name of the group. Renaming updates it everywhere — group page, leaderboards, search and Discord messages.",
        "default": "",
        "max_length": 30,
    },
    {
        "key": "group_description",
        "label": "Description",
        "category": "integration",
        "type": "text",
        "help": "Short description shown on the public group page.",
        "default": "",
    },
    {
        "key": "clan_chat_name",
        "label": "Clan chat name",
        "category": "integration",
        "type": "string",
        "help": "Your in-game clan chat channel name, exactly as it appears in game. Required for clan broadcast tracking: relayed broadcasts only bind to this group when the relayer's clan matches this name.",
        "default": "",
    },
    # --- Clan broadcast tracking (chat-relayed drops for non-plugin members) ---
    # Gate + storage floor for data/submissions/clan_broadcast.py. Broadcasts
    # only bind to a group when clan_broadcast_tracking is on AND the group's
    # clan_chat_name matches the relaying member's in-game clan. Chat rows are
    # authed=False / source='clan_chat' and never feed events, points or splits.
    {
        "key": "clan_broadcast_tracking",
        "label": "Clan broadcast tracking",
        "category": "integration",
        "type": "boolean",
        "help": "Track drops, pets and collection log slots for members who don't run the plugin, parsed from in-game clan broadcast messages relayed by clanmates who do. Requires the clan chat name to be set. Chat-tracked entries are unverified, carry no screenshots, and never count toward events, points or splits.",
        "default": False,
    },
    {
        "key": "clan_broadcast_min_value",
        "label": "Clan broadcast minimum value",
        "category": "integration",
        "type": "int",
        "help": "Extra GP floor for chat-relayed drops: broadcasts below this are not recorded for this group at all. 0 records everything the clan's in-game broadcast threshold lets through.",
        "default": 0,
        "min": 0,
    },
    # Relayed broadcasts can never carry a screenshot, so only_send_messages_
    # with_images would otherwise record chat rows and notify nothing. Default
    # True: opting into tracking opts into its imageless notifications.
    {
        "key": "clan_broadcast_notify_without_images",
        "label": "Notify clan broadcasts without screenshots",
        "category": "integration",
        "type": "boolean",
        "help": "Relayed clan broadcasts never carry a screenshot, so leave this on if you use \"Only send messages with images\" — otherwise chat-tracked drops, personal bests, pets and collection log slots are recorded but never announced. Turn it off to keep those announcements out of your channels entirely.",
        "default": True,
    },
    # --- Clan chat bridge (two-way game ↔ Discord chat sync) ---
    # services/clan_chat_bridge.py + data/submissions/clan_chat.py. Requires
    # clan_chat_name; one toggle drives both directions (game lines mirrored
    # into the channel, channel messages shown in game for plugin users).
    {
        "key": "clan_chat_bridge_enabled",
        "label": "Clan chat bridge",
        "category": "integration",
        "type": "boolean",
        "help": "Two-way sync between your in-game clan chat and the bridge channel: game chat is mirrored into the channel, and channel messages appear in game for members running the plugin with the bridge enabled. Requires the clan chat name and a bridge channel.",
        "default": False,
    },
    {
        "key": "channel_id_clan_chat_bridge",
        "label": "Clan chat bridge channel",
        "category": "integration",
        "type": "channel",
        "help": "The Discord channel your in-game clan chat is mirrored to, and whose messages are relayed into the game. Anyone who can type in this channel can speak to the clan — restrict it accordingly.",
        "default": None,
    },
    {
        "key": "discord_url",
        "label": "Discord invite URL",
        "category": "integration",
        "type": "string",
        "help": "Public Discord invite shown on the group page.",
        "default": "",
    },
    {
        "key": "auto_provision_members",
        "label": "Auto-add WiseOldMan members",
        "category": "integration",
        "type": "boolean",
        "help": "Creates DropTracker profiles ahead of time for everyone in this group's linked WiseOldMan group, so members join this group automatically the moment they install the plugin — instead of waiting up to an hour for the next member sync.",
        "default": False,
    },
    {
        "key": "export_api_key",
        "label": "Export API key",
        "category": "integration",
        "type": "string",
        "help": "Per-group key used for on-demand WOM sync. Treat as a secret.",
        "default": None,
    },

    # --- Events: WOM reconciliation ---
    # Hybrid event XP/KC tracking from WiseOldMan bulk gains
    # (services/event_wom_reconciler.py). On by default for any group with a
    # linked WOM group id; this key force-disables it.
    {
        "key": "event_wom_reconciliation",
        "label": "Event WiseOldMan tracking",
        "category": "integration",
        "type": "boolean",
        "help": "During events, top up XP and boss KC task progress from WiseOldMan hiscores so members without the plugin still count. Never double-counts progress the plugin already tracked.",
        "default": True,
    },
    # Optional WOM group verification code: lets event freshness passes queue
    # a group-wide WOM "update-all" (one API call) instead of per-player
    # updates. Admin-only, redacted in audit logs like export_api_key.
    # "password" coerces exactly like "string" (both coerce_* functions fall
    # through); the type only changes how the web editor renders the input.
    {
        "key": "wom_verification_code",
        "label": "WiseOldMan verification code",
        "category": "integration",
        "type": "password",
        "help": "Your WiseOldMan group's verification code. Optional — lets DropTracker queue a group-wide WOM update when events start and end, keeping hiscores-based event progress fresh. Treat as a secret.",
        "default": None,
    },

    # --- Monthly recaps ---
    # The clan's "Wrapped" card, posted on the 1st for the month just ended
    # (services/recap.py builds it; the delivery job posts it).
    #
    # Off by default: every clan receives one unsolicited card, after which an
    # admin turns this on to keep getting them. That first post is authorised by
    # a seeding pass rather than by code, so the flag always reflects the truth —
    # a clan can switch it off in advance and never receive one at all.
    {
        "key": "recaps_enabled",
        "label": "Post monthly recaps",
        "category": "recaps",
        "type": "boolean",
        "help": "Post your clan's recap card on the 1st of each month, covering the month just ended. Every clan receives one card to begin with; turn this on to keep receiving them, or off to stop.",
        "default": False,
    },
    # Where it goes. Empty falls back to lootboard_channel_id, which is where a
    # clan's monthly totals already live, so most groups need not set this.
    {
        "key": "channel_id_to_post_recaps",
        "label": "Recap channel",
        "category": "recaps",
        "type": "channel",
        "help": "Where the monthly recap card is posted. Leave empty to use your lootboard channel.",
        "default": None,
    },
    # Local hour (0-23) on the 1st. Combined with recap_timezone below, which is
    # seeded from the browser of the first admin who opens this page, so the
    # default is "noon, their time" rather than a number someone has to reason
    # about. Noon and not midnight because the month closes at 00:00 UTC, which
    # is the middle of the night across Europe and the Americas.
    #
    # A card cannot be built before that close, so a clan far enough ahead of UTC
    # gets the earliest moment after it — which is still their afternoon.
    {
        "key": "recap_post_hour",
        "label": "Post at (hour)",
        "category": "recaps",
        "type": "int",
        "help": "Hour of the 1st, in the timezone below, to post the card. Defaults to 12 (midday) — the month closes at 00:00 UTC, which is the middle of the night for most people. A card can't exist before that close, so clans far enough ahead of UTC receive theirs at the first moment after it.",
        "default": 12,
        "min": 0,
        "max": 23,
    },
    # IANA name (e.g. "America/New_York"). Empty means UTC.
    {
        "key": "recap_timezone",
        "label": "Timezone",
        "category": "recaps",
        "type": "string",
        "help": "IANA timezone name, e.g. Europe/London. Set automatically from your browser the first time an admin opens this page; empty means UTC.",
        "default": None,
    },

    # --- Clan Log ---
    # The standing "how far through every boss's uniques are we" message, edited
    # in place as members pull things (services/clan_log.py builds the board,
    # the core bot's refresher posts it). Off by default: it is a message the
    # bot owns and keeps editing in someone's channel, which no clan should get
    # without asking. The board itself is always available on the website and
    # through /clan-log, for every group and every tier.
    {
        "key": "clan_log_enabled",
        "label": "Post a live Clan Log board",
        "category": "clan_log",
        "type": "boolean",
        "help": "Keep a standing message in your Discord showing how far through every boss's uniques your clan is, edited automatically as members pull things. Your board is always on the website and available through /clan-log — this is only the Discord message.",
        "default": False,
    },
    # Where the standing message lives. Unlike the recap this does NOT fall back
    # to the lootboard channel — an ever-editing message would fight the
    # lootboard for the same slot.
    {
        "key": "clan_log_channel_id",
        "label": "Clan Log channel",
        "category": "clan_log",
        "type": "channel",
        "help": "Where the standing Clan Log message lives. Pick a channel of its own: the bot edits this message continuously, so it will bury conversation in a busy channel.",
        "default": None,
    },
    # The message the bot edits. Written by the bot, not by an admin.
    {
        "key": "clan_log_message_id",
        "label": "Clan Log message id",
        "category": "clan_log",
        "type": "string",
        "help": "Message the bot edits when updating the board. Managed automatically.",
        "default": None,
    },
]

_BY_KEY = {f["key"]: f for f in GROUP_CONFIG_FIELDS}

# Sensitive keys never returned to non-admins (the endpoint is admin-gated, but
# guard explicitly per §11 / Task 05).
SENSITIVE_KEYS = {"export_api_key", "wom_verification_code"}

# Keys whose value is a public Discord link shown to visitors, and which must
# therefore never hold a webhook URL — that carries its own auth token, so
# publishing one hands write access to the clan's server to anyone who looks.
_DISCORD_LINK_KEYS = frozenset({"discord_url"})


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


# Limits for `messagelist` fields — the TS registry
# (packages/api-types/src/group-config.ts) enforces the same.
MESSAGE_LIST_MAX_ENTRIES = 30
MESSAGE_LIST_MAX_ENTRY_LENGTH = 200
MESSAGE_LIST_MAX_RAW_LENGTH = 8000
# Message content pings for real (embed text doesn't), so mention syntax is
# rejected outright rather than trusting the placement checkbox's state.
MESSAGE_LIST_MENTION_RE = re.compile(r"@everyone|@here|<@[&!]?\d+>")


def coerce_message_list(key: str, value: Any) -> str:
    """Validate + normalize a messagelist value to its stored form: a JSON
    array of message strings, or "" when unset. Raises ConfigValidationError."""
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, str):
        if len(value) > MESSAGE_LIST_MAX_RAW_LENGTH:
            raise ConfigValidationError(key, f"'{key}' is too large.")
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise ConfigValidationError(key, f"'{key}' must be a JSON array of strings.")
    if not isinstance(value, list) or any(not isinstance(e, str) for e in value):
        raise ConfigValidationError(key, f"'{key}' must be a JSON array of strings.")
    if not value:
        return ""
    if len(value) > MESSAGE_LIST_MAX_ENTRIES:
        raise ConfigValidationError(
            key, f"'{key}' allows at most {MESSAGE_LIST_MAX_ENTRIES} messages."
        )
    for entry in value:
        if not entry.strip():
            raise ConfigValidationError(key, f"'{key}' messages can't be blank.")
        if len(entry) > MESSAGE_LIST_MAX_ENTRY_LENGTH:
            raise ConfigValidationError(
                key,
                f"'{key}' messages must be at most {MESSAGE_LIST_MAX_ENTRY_LENGTH} characters.",
            )
        if MESSAGE_LIST_MENTION_RE.search(entry):
            raise ConfigValidationError(
                key, f"'{key}' messages can't contain @everyone, @here or Discord mentions."
            )
    return json.dumps(value, ensure_ascii=False)


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

    if ftype == "messagelist":
        return coerce_message_list(key, value)

    # channel / string / text / csv / bosslist
    if value is None:
        return ""
    if not isinstance(value, (str, int)):
        raise ConfigValidationError(key, f"'{key}' must be a string.")
    text_value = str(value)

    # A Discord webhook URL contains its own token: anyone who reads it can post
    # into the clan's server as the clan. This field is published on the group
    # page, so accepting one would hand it to every visitor. Rejected loudly
    # rather than silently blanked, because the person pasting it has almost
    # certainly confused this box with the webhook setting and needs telling.
    if key in _DISCORD_LINK_KEYS:
        from utils.discord_urls import is_discord_credential_url

        if is_discord_credential_url(text_value):
            raise ConfigValidationError(
                key,
                f"'{key}' is a public invite link shown on your group page, and "
                "that is a Discord webhook URL — anyone who saw it could post to "
                "your server. Use an invite link (discord.gg/…) here; webhooks "
                "belong in the notification channel settings.",
            )

    # A declared max_length means "trimmed, bounded string" — trailing spaces
    # must not push a name over the limit its column can hold.
    limit = field.get("max_length")
    if limit is not None:
        text_value = text_value.strip()
        if len(text_value) > limit:
            raise ConfigValidationError(
                key, f"'{key}' must be {limit} characters or fewer."
            )
    return text_value
