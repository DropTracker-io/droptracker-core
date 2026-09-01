# DropTracker — Submission Processing Pipeline

This document traces the complete lifecycle of a submission from plugin to Discord notification.

---

## Submission Types

| Type | Processor | Trigger |
|---|---|---|
| `drop` | `data/submissions/drop.py → drop_processor()` | Player receives loot from an NPC |
| `pb` | `data/submissions/pb.py → pb_processor()` | Player sets a personal best kill time |
| `clog` | `data/submissions/clog.py → clog_processor()` | Player unlocks a collection log slot |
| `ca` | `data/submissions/ca.py → ca_processor()` | Player completes a combat achievement |
| `pet` | `data/submissions/pet.py → pet_processor()` | Player receives a pet drop |
| `quest` | `data/submissions/quest.py → quest_processor()` | Player completes a quest |
| `experience` | `data/submissions/experience.py → experience_processor()` | Player gains XP milestone / level-up |
| `adventure_log` | `data/submissions/adventure_log.py → adventure_log_processor()` | Adventure log event |

---

## Phase 1: Intake (API Route)

**File:** `api/routes/webhook.py`

The RuneLite plugin sends a `multipart/form-data` POST to `POST /webhook`:
- `payload_json` — JSON string with embed-style fields
- `file` — optional screenshot image (PNG/JPEG)

The handler `_process_webhook_request()` does:

1. Parse `payload_json` → extract embed fields via `process_webhook_data()`
2. Detect `world_type`: check embed fields for League/Seasonal world indicators → `"seasonal"` or `"main"`
3. If `file` is present: save image via `utils/download.py → download_image()`, store the local path
4. If `video_key` field is present: look up `VideoUpload` row by key, link it later
5. Build `submission_data` dict via `api/routes/helpers.py → assemble_submission_data()`
6. Route by `type` field using a `match` statement → call the appropriate processor
7. Return `200 OK` with the processor's `SubmissionResponse` (or error status)

**Current limitation:** Steps 1–7 all happen synchronously in the request handler. WOM and OSRS API calls can make this take 5–40 seconds under load. See `docs/REFACTOR_PLAN.md`.

---

## Phase 2: Drop Processing (Core Flow)

**File:** `data/submissions/drop.py → drop_processor(drop_data, external_session, world_type)`

This is the most complex processor. All others follow a similar but simpler pattern.

### Step 2.1 — Extract Fields

```
npc_name, value, item_id, item_name, quantity,
player_name, acc_hash, guid, kill_count,
nearby_players (list, for split tracking),
raid_party_size + roster_source (raid submissions, plugin >= 5.4.3)
```

**Raid-party evidence gate.** For raid-sourced submissions the plugin sends
the evidence behind its participant list, not just the list: `raid_party_size`
(max of the game's own team-size varbits sampled through the raid, the live
read, and named participants + 1) and `roster_source`
(`authoritative | solo | proximity-fallback`). `_apply_raid_party_evidence`
reconciles the list against them *before* anything consumes it:

- proven solo (`raid_party_size == 1` or `roster_source == "solo"`) → any
  claimed participants are impossible and are stripped; no points sharing, no
  GP split. This is the guarantee that a client roster bug can never again
  credit people who were not in the raid (the 2026-08-11 solo-CoX incident,
  where a stale RuneLite party member was credited).
- proven team (`raid_party_size >= 2`, participants present) → the party size
  floors the GP-split divisor (like the manual `split_size`, it can only
  *raise* it), so untracked raiders shrink every tracked share instead of
  silently inflating them.
- absent (pre-5.4.3 client, non-raid, manual) → behavior unchanged.

Gate activity is observable: `[RaidPartyGuard]` lines in the consumer journal,
and the TEMP split observer records `ps`/`rs` on samples plus a
`solo_stripped` counter per NPC (`splitscan:npc:{id}`).

### Step 2.2 — Deduplication

`ensure_can_create(unique_id, player_name)` in `data/submissions/common.py`:

1. Check in-memory `unique_id_cache` (LRU-style dict, max 1000 entries per type)
2. Query DB: `SELECT 1 FROM drops WHERE unique_id = ? AND created_at > NOW() - INTERVAL 1 HOUR`
3. If found in either: return `SubmissionResponse(success=False, message="Duplicate submission")`

### Step 2.3 — Item Validation

`ensure_item_for_drop(item_id, item_name)`:

1. Cache hit? Return cached `ItemList` row
2. Query `items` table by `item_id`
3. If not found: query by `item_name`
4. If still not found: call OSRS API to fetch item metadata, insert new `ItemList` row
5. Return `ItemList` row or `None` (unknown item still proceeds but `item_id` may be null)

### Step 2.4 — Player Authentication

`ensure_player_and_auth(player_name, acc_hash, session)` in `data/submissions/common.py`:

1. Query `players` by `account_hash` → found means returning player
2. If not found by hash: query by `player_name`
3. **WOM API call**: `utils/wiseoldman.py → check_user_by_username(player_name)`
   - Gets canonical RSN (may differ from submitted name due to case/spaces)
   - Gets WOM ID, total level
4. If player row doesn't exist: create it with WOM-verified data
5. `check_auth(player, acc_hash)`:
   - First submission from this player: bind `account_hash` to the player row (`authed = True`)
   - Hash matches stored: `authed = True`
   - Hash mismatch: `authed = False` (drop still recorded but flagged)
6. Return `(Player, authed: bool)`

### Step 2.5 — NPC Resolution

`ensure_npc_id_for_player(npc_name, session)`:

1. Check in-memory NPC name cache
2. Query `npc_list` by `npc_name` (exact match, case-insensitive)
3. If not found: call OSRS semantic API (OpenAI-backed) to find closest NPC match
4. Insert new `NpcList` row if genuinely new NPC
5. Return `npc_id`

### Step 2.6 — High-Value Drop Verification

If `value > 1_000_000` (1M GP):

Call `osrs_api.semantic.check_drop(item_name, npc_name)`:
- Queries OSRS Wiki drop table API
- Backed by OpenAI for fuzzy NPC/item name matching
- Returns `True` if item can plausibly drop from this NPC, `False` otherwise
- If `False`: drop is recorded but flagged (does not block, just marks as suspicious)

### Step 2.7 — Write Drop to DB

`db/ops.DatabaseOperations.create_drop_object(...)`:

- Inserts row into `drops` table (or `seasonal_drops` if `world_type == "seasonal"`)
- Sets `partition = YYYYMM` for monthly scoping
- Commits the session

### Step 2.8 — Update Redis Leaderboards

`services/redis_updates.RedisLootTracker.add_to_player(player_id, group_id, value, partition)`:

- `ZADD leaderboard:{partition} <value> <player_id>` (increment score, global)
- `ZADD leaderboard:group:{group_id}:{partition} <value> <player_id>` (per-group)
- `SET player:{player_id}:{partition}:total_loot <new_total>` (running total string)
- If NPC is notable: also updates `leaderboard:npc:{npc_id}:{partition}`

### Step 2.9 — Group Processing

For each group the player belongs to (via `user_group_association`):

**A. Check notification thresholds:**
- Read `minimum_value_to_notify` from `group_configurations` (default: 2.5M GP)
- If `value < threshold`: skip notification for this group, unless either
  override fires (both still respect the image requirement below):
  - the item (or source NPC) is on the group's **always-announce list**
    (`group_notification_always_list`, edited on the settings page; matcher in
    `db/notification_always_list.py`) — built for notable zero-value items the
    plugin force-screenshots
  - group points were awarded for this drop and `notify_points_awarded` is on
    (opt-in, default OFF) — points are never awarded silently. The same fallback exists
    in the clog/ca/pb/pet processors when their notify toggle (or CA tier
    minimum) would have stayed quiet. `notify_reason` on the queued payload
    records which gate fired (`value` / `config` / `always_list` / `points`).
- If group requires images (`only_send_messages_with_images = true`) and no image: skip

**B. Points system (premium groups only):**
- Check `feature_activations` to see if points are active for this group
- Call `data/submissions/point_awards.py → check_and_award_points(player_id, group_id, value)`
  - Looks up `group_point_config` rules
  - Calls `services/points.py → award_points_to_player()` → inserts `PointCredit` row, updates `PlayerPoints`

**C. GP split tracking (optional):**
- If `split_gp_tracking` is enabled and `nearby_players` is non-empty
- `_award_split_gp_credits()` distributes loot credit proportionally among listed players
- Inserts `DropSplit` rows

**D. Create notification:**
`data/submissions/common.py → create_notification("drop", player_id, group_id, data_dict)`:
- Inserts row into `notification_queue` with `status = "pending"`
- `data` JSON includes all fields needed to render the embed

Optionally also creates a `"dm_drop"` notification if the player has `dm_drops = True` in `UserConfiguration`.

### Step 2.10 — Return

Return `SubmissionResponse(success=True, message="Drop recorded", notice=None)` to the API handler, which returns `200 OK` to the plugin.

---

## Phase 3: Notification Dispatch

**File:** `services/notification_service.py → NotificationService`

Runs as a background asyncio task inside `bots/main.py`. Polls every 3 seconds.

1. `SELECT * FROM notification_queue WHERE status = 'pending' ORDER BY created_at LIMIT 50`
2. For each notification:
   a. Look up group's `channel_id_to_post_loot` from `group_configurations`
   b. Fetch `GroupEmbed` template for this group + notification type
   c. Replace placeholders in template:
      - `{player_name}` → RSN
      - `{item_name}`, `{npc_name}`, `{value}`, `{quantity}`
      - `{next_refresh}` → Discord timestamp for next lootboard update
      - Custom group-specific placeholders
   d. Build `discord.Embed` with rendered fields, image attachment, footer
   e. `await channel.send(embed=embed, files=[image_file])`
   f. Update `notification_queue` row: `status = "processed"`, `processed_at = NOW()`
   g. Insert into `notified_submissions` (dedup guard)

---

## Other Submission Processors

All non-drop processors follow the same basic structure but are simpler:

### `pb_processor()` (Personal Best)
- Extracts: `npc_name`, `kill_time` (ms), `team_size`, `player_name`, `acc_hash`
- Auth: same `ensure_player_and_auth()`
- Compares against existing `PersonalBestEntry` for this player + NPC
- If new PB: sets `personal_best = True`, creates notification
- If not new PB: still records the kill time but `personal_best = False`, no notification

### `clog_processor()` (Collection Log)
- Extracts: `item_name`, `item_id`, `reported_slots`, `player_name`, `acc_hash`
- Checks if this `(player_id, item_id)` combination already exists in `collection`
- If new: inserts, creates notification
- Updates player's `log_slots` count on the `Player` row

### `ca_processor()` (Combat Achievement)
- Extracts: `task` (string), `tier` (easy/medium/hard/elite/master/grandmaster), `points`
- Checks for duplicate `(player_id, task)` in `combat_achievement`
- If new: inserts, creates notification with tier information

### `pet_processor()` (Pet Drop)
- Extracts: `pet_name`, `item_id`, `source` (NPC name)
- Checks for duplicate `(player_id, item_id)` in `player_pet`
- If new: inserts, creates notification

### `quest_processor()` (Quest Completion)
- Extracts: `quest_name`
- Checks for duplicate `(player_id, quest_name)` in `quest_completion`
- If new: inserts, creates notification

### `experience_processor()` (XP / Level-up)
- Extracts: `skill`, `level`, `xp` (total XP)
- Primarily used for milestone announcements (99s, 200M XP)
- Notification threshold configurable per group

### `adventure_log_processor()`
- Handles miscellaneous adventure log events not covered by other types
- Parses event text from embed fields

---

## Legacy Submission Path (Webhook Bot)

**File:** `bots/webhook_bot.py`

For groups that use Discord webhook URLs instead of the API:

1. Plugin sends to a Discord webhook URL → message appears in a Discord channel
2. `DT-webhooks` bot (`bots/webhook_bot.py`) listens for `MessageCreate` in those channels
3. Parses embed fields from the Discord message (same field names as `payload_json`)
4. Builds equivalent `submission_data` dict
5. Calls the same processor functions as the API path
6. Creates `NotificationQueue` entries → same notification dispatch as API path

This path has higher latency and is limited to groups that haven't migrated to the API. The API path is strongly preferred.

---

## Deduplication Summary

Deduplication operates at three levels:

| Level | Mechanism | Scope |
|---|---|---|
| In-memory | `unique_id_cache` dict per processor | Current process lifetime |
| Database | `unique_id` query on drops/submissions tables | Past 1 hour |
| Notification | `notified_submissions` table | Permanent |

The `unique_id` is a UUID generated by the RuneLite plugin for each event and included in every submission payload.

---

## Seasonal / League World Handling

All submission processors check `world_type` before writing:

- `world_type == "main"` → write to standard tables (`drops`, `personal_best`, etc.)
- `world_type == "seasonal"` → write to `seasonal_*` mirror tables

Group configurations use `seasonal_` prefixed keys for seasonal-specific settings (e.g., `seasonal_minimum_value_to_notify`, `seasonal_notify_pbs`). Both standard and seasonal submissions can be active simultaneously for the same group.
