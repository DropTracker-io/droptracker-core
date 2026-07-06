# DropTracker — System Architecture

## System Overview

DropTracker is a multi-process Python system. Each concern runs as its own process:

```
                    ┌─────────────────────────────────────┐
  RuneLite Plugin ──▶  REST API  (api/app.py, port 31323) │
                    │  Hypercorn / Quart                   │
                    └──────────────┬──────────────────────┘
                                   │ writes to
                    ┌──────────────▼──────────────────────┐
                    │  MySQL: `data` DB  +  Redis          │
                    └──────────────┬──────────────────────┘
                                   │ reads from
                    ┌──────────────▼──────────────────────┐
                    │  Discord Bot  (bots/main.py)         │
                    │  NotificationService + lootboard     │
                    └─────────────────────────────────────┘

                    ┌─────────────────────────────────────┐
  Discord Webhook ──▶  Webhook Bot  (bots/webhook_bot.py) │  (legacy path)
                    └─────────────────────────────────────┘

                    ┌─────────────────────────────────────┐
                    │  Lootboard  (lootboard/_board_gen…)  │
                    │  Runs every 2 minutes, Pillow images │
                    └─────────────────────────────────────┘
```

> **Addendum (2026-07-06):** this document covers the core intake/notification
> pipeline, which is unchanged. Subsystems added since it was written are
> documented in [CLAUDE.md](../CLAUDE.md) and the [README](../README.md):
>
> - **Website API** — `web_api/` (Quart, port 31325, systemd
>   `droptracker-webapi`): JWT auth, profiles, group config, admin surfaces,
>   SSE realtime. Backs the Next.js frontend (separate repo, port 31380).
> - **Events v2** — `db/models/events.py`, `services/event_engine.py` /
>   `event_lifecycle.py` / `event_notifications.py`, consumer in
>   `workers/event_consumer.py`.
> - **Badges** — `services/badges.py`, `db/models/badge.py`.
> - **Subscriptions/PayPal** — `db/models/subscriptions.py`, `web_api/billing.py`.
> - **Async intake (Phase 1)** — `WEBHOOK_QUEUE_MODE` +
>   `workers/webhook_consumer.py` (see [REFACTOR_PLAN.md](REFACTOR_PLAN.md)).
> - Production process management moved from GNU screen to **systemd units**
>   (`droptracker-*.service`).

---

## Component Descriptions

### REST API (`api/`)

- Framework: Quart (async Flask) + Hypercorn (ASGI server)
- Entry: `api/app.py` → calls `create_app()` in `api/__init__.py`
- `create_app()` registers blueprints: `webhook`, `players`, `groups`, `group_create`, `video`, `utils`, `health`, `worker`
- `api/core.py`: DB session factory (`get_db()`), metrics tracker, logger
- Rate limiting is applied per-route via Quart-Limiter decorators

### Discord Bot (`bots/main.py`)

Built on `discord-py-interactions`. On startup loads these Extensions:
- `commands` — slash commands (user, admin, group admin)
- `services.message_handler` — `MessageCreate` + component interaction handler
- `services.channel_names` — voice channel countdown timer updates
- `services.components` — UI component builders
- `services.entry_modifier` — submission editing flow
- `services.user_context` — right-click context menus
- `services.group_poll` — periodic WOM group member sync

Recurring tasks (asyncio):
- Every 8 min: update lootboard images in Discord
- Every 60 min: sync group members from WOM
- Every 3 min: update countdown channel names
- Every 30 sec: force-drain notification queue
- Every 60 sec: heartbeat / reconnect check

`NotificationService` runs as a persistent background task inside the bot process — it polls `notification_queue` every 3 seconds and dispatches Discord embeds.

### Webhook Bot (`bots/webhook_bot.py`)

Legacy fallback for groups that use Discord webhooks instead of the API. The bot listens for Discord `MessageCreate` events in specific channels, parses embedded fields from the plugin's webhook output, and calls the same processor functions as the API path.

---

## Database Schema Reference

Two MySQL databases are used:

### `data` (Application Database)

#### Core Identity Tables

**`users`** — Discord users
- `user_id` (PK), `discord_id`, `auth_token`, `xf_user_id`
- `public` (bool), `ping_on_drops` (bool), `dm_drops` (bool)

**`players`** — OSRS accounts
- `player_id` (PK), `player_name` (canonical RSN), `wom_id`
- `account_hash` (bound hash from plugin), `total_level`, `log_slots`
- `user_id` (FK → users), linked when player claims their account

**`groups`** — Clans / communities
- `group_id` (PK), `group_name`, `wom_id`, `guild_id`
- `invite_url`, `icon_url`, `member_count`
- Group 2 is the global group — every player is a member

**`guilds`** — Discord guild metadata
- `guild_id` (PK), `guild_name`, `group_id` (FK)

**`user_group_association`** — M2M join: player ↔ group
- `player_id`, `group_id`

#### Submission Tables

**`drops`** — Individual loot drops (most important table)
- `drop_id` (PK), `item_id`, `player_id`, `npc_id`
- `value` (GP), `quantity`, `image_url`, `video_url`
- `authed` (bool — player authenticated), `used_api` (bool — came via API not webhook)
- `partition` (YYYYMM string — used for monthly scoping)
- `unique_id` (UUID — deduplication key)

**`seasonal_drops`** — Mirror of `drops` for League/Seasonal worlds

**`personal_best`** — Kill time records
- `player_id`, `npc_id`, `kill_time` (ms), `personal_best` (bool), `team_size`

**`collection`** — Collection log unlocks
- `item_id`, `npc_id`, `player_id`, `reported_slots`

**`combat_achievement`** — CA completions
- `player_id`, `task` (string), `tier`, `points`

**`player_pet`** — Pet records
- `player_id`, `item_id`, `pet_name`, `source`

**`quest_completion`** — Quest records
- `player_id`, `quest_name`

**`seasonal_*`** — Mirror tables for each of the above for Seasonal worlds

#### Catalog Tables

**`items`** (ItemList) — OSRS item catalog
- `item_id` (PK), `item_name`, `noted` (bool), `stackable` (bool), `stacked` (bool)

**`npc_list`** (NpcList) — OSRS NPC catalog
- `npc_id` (PK), `npc_name`

#### Configuration & Notification

**`group_configurations`** — Key-value config per group
- `config_id` (PK), `group_id` (FK), `config_key`, `config_value`
- All group behavior is driven by keys stored here (see CLAUDE.md for key list)

**`notification_queue`** — Pending Discord notifications
- `notification_id` (PK), `type` (drop/pb/clog/ca/pet/quest)
- `player_id`, `group_id`, `data` (JSON blob), `status` (pending/processed/failed)
- `created_at`, `processed_at`

**`notified_submission`** — Dedup guard: already-sent notifications
- `player_id`, `group_id`, `unique_id`, `notification_type`

#### Media

**`video_uploads`** — Video clip metadata
- `upload_id` (PK), `player_id`, `video_key` (UUID used to link to drop)
- `status` (pending/processing/complete/failed)
- `final_key` (B2 object key after processing), `drop_id` (FK, set on link)

#### Premium / Points System

**`feature_activations`** — Active premium features per group
- `group_id`, `feature_id`, `expires_at`

**`premium_features`** — Feature definitions
- `feature_id` (PK), `feature_name`, `description`

**`player_points`** — Point totals per player per group
- `player_id`, `group_id`, `total_points`

**`point_credits`** — Point award ledger (immutable log)
- `player_id`, `group_id`, `amount`, `source`, `created_at`, `expires_at`

**`point_debits`** — Point spending ledger

**`group_point_config`** — Custom point rules per group
- `group_id`, `rule_type`, `rule_value`, `points_awarded`

#### Discord Embeds & Webhooks

**`group_embeds`** — Custom Discord embed templates
- `group_id`, `embed_type` (drop/pb/clog/etc.)
- `title`, `description`, `fields` (JSON), `color`, `thumbnail_url`
- Placeholders (`{player_name}`, `{item_name}`, `{value}`, `{next_refresh}`, etc.) are replaced at notification time

**`webhooks`** — Registered Discord webhooks
- `group_id`, `webhook_url` (Fernet-encrypted), `channel_id`

**`backup_webhooks`** — Fallback webhook if primary fails

#### Lootboard

**`lootboard_styles`** — Per-group lootboard theme config
- `group_id`, `theme` (square/rounded/runelite/etc.), `custom_options` (JSON)

#### Other

**`ignored_players`** — Players excluded from group leaderboards
**`tickets`** — Discord support ticket system
**`analytics`** — Hourly/daily aggregated stats

### `xenforo` (Forum Database)

- Read for user lookups (`xf_user`, `xf_user_upgrade`)
- Written to for alert creation, group creation sync
- `db/xf/recent_submissions.py` creates forum entries for notable drops

---

## Redis Data Structures

All keys use `{YYYYMM}` partitioning for monthly scoping.

| Key Pattern | Type | Contents |
|---|---|---|
| `leaderboard:{YYYYMM}` | Sorted Set | member: `player_id`, score: total loot GP (global) |
| `leaderboard:group:{group_id}:{YYYYMM}` | Sorted Set | member: `player_id`, score: total loot GP in group |
| `leaderboard:npc:{npc_id}:{YYYYMM}` | Sorted Set | member: `player_id`, score: total loot GP from NPC |
| `player:{player_id}:{YYYYMM}:total_loot` | String | Total GP this month |
| `last_group_sync` | String | Timestamp of last WOM group sync |

Leaderboard updates happen in `services/redis_updates.py → RedisLootTracker.add_to_player()` immediately after a drop is written to the DB.

---

## External Service Integrations

### Wise Old Man (WOM) API
- Used by: `utils/wiseoldman.py`
- Purpose: authoritative OSRS player identity (canonical RSN, WOM ID, total level, group membership)
- Called on every new player submission to verify identity before creating DB row
- Also used for group member sync (hourly)

### OSRS Wiki / GE API
- Used by: `utils/ge_value.py`, `osrs_api/`
- Purpose: get Grand Exchange price for items, semantic NPC/item verification
- High-value drops (> 1M GP) are checked to confirm item drops from the stated NPC
- Backed by OpenAI for semantic NPC name matching (`data/submissions/common.py`)

### XenForo
- Used by: `db/xf/`, `services/xf_services.py`
- Separate MySQL DB (`xenforo` schema)
- Purpose: forum integration — user lookup, alerts, group sync, premium upgrade checks

### Backblaze B2
- Used by: `utils/b2_storage.py`, `utils/video_storage.py`
- Purpose: video clip storage
- Presigned PUT URLs generated server-side, uploaded directly by client

### Discord (discord-py-interactions)
- Two bots: primary (`bots/main.py`) and webhook-channel reader (`bots/webhook_bot.py`)
- Slash commands registered globally (or per guild in dev)
- Components (buttons, select menus) handled by `services/components.py`

---

## Authentication & Security

### Plugin Authentication
- First submission from a player: `account_hash` (opaque identifier sent by plugin) is stored and bound to the player
- Subsequent submissions: `account_hash` must match the stored value (`check_auth()` in `data/submissions/common.py`)
- First-time binds are allowed; mismatches flag the drop as `authed=False`

### Server-to-Server (Web → API)
- `POST /groups/create` and related endpoints require `Authorization: Bearer <XF_KEY>` header
- `XF_KEY` is a shared secret in `.env`

### Webhook URL Encryption
- Webhook URLs stored in DB are Fernet-encrypted (`utils/encrypter.py`)
- `ENCRYPTION_KEY` must be a valid Fernet key in `.env`

### High-Value Submission Verification
- Drops > 1M GP: semantic NPC/item check via OSRS Wiki API + OpenAI
- Guards against spoofed high-value submissions

---

## Image Generation (Lootboard)

`lootboard/_board_generator.py` runs as a standalone process with a 2-minute loop:

1. Queries all active groups
2. For each group: calls `lootboard/board_generator.py → generate_server_board()`
3. `generate_server_board()` fetches top players from Redis, downloads player avatars, renders a ranked image using Pillow
4. Image saved to disk; bot process reads it and edits/reposts the Discord lootboard message

Themes are in `lootboard/themes/` (PNG asset packs). Theme selection is stored per group in `group_configurations`.

---

## Known Issues / Tech Debt

See `docs/REFACTOR_PLAN.md` for the primary known issue:

- **Webhook handler blocks on processing.** The `POST /webhook` route currently awaits the full pipeline (DB writes, WOM API calls, OSRS API calls) before returning a response. Under load this produces 10–40s response times and connection pool exhaustion. Planned fix: accept quickly into a Redis queue, process in a separate consumer process.

Other notable debt:
- `bots/main.py` has a legacy embedded Quart server (port 8080, serving the old `web/` blueprints) that predates `api/app.py`; the standalone API is the canonical path
- `services/message_handler.py` has the webhook-channel processing logic commented out (replaced by the API path), but the code still exists as documentation
- Some group configuration reads are scattered across processor files rather than in a single config accessor
