# DropTracker — Agent Reference

This file is auto-loaded by Claude Code. It gives a full orientation to the codebase so agents can work efficiently without repeating broad exploration.

---

## What Is This?

DropTracker is an Old School RuneScape (OSRS) loot and achievement tracking platform for clans and individual players. A custom RuneLite plugin (separate repo) sends submissions to this backend, which processes them, stores results in MySQL, updates Redis leaderboards, generates lootboard images, and posts Discord notifications.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ (async/await throughout) |
| Web / API | Quart 0.19 + Hypercorn (ASGI) |
| Discord | discord-py-interactions 5.14 |
| Database | MySQL (two schemas: `data` + `xenforo`) via SQLAlchemy 2.0 + PyMySQL |
| Cache / Leaderboards | Redis (sorted sets + key-value) |
| Migrations | Alembic 1.13 |
| Image generation | Pillow |
| External APIs | Wise Old Man (WOM) API, OSRS Wiki/GE API, OpenAI (semantic NPC checks) |
| Video storage | Backblaze B2 (prod) or local filesystem |
| Auth / crypto | Fernet (cryptography), PyJWT |
| Forum | XenForo (separate DB, integrated via API) |
| Process management | GNU screen (prod), systemd watchdog |
| Config | python-dotenv (`.env`) |

---

## Running Processes

The system is **multiple separate processes** run concurrently (via `real_startup.sh` with GNU screen):

| Screen Name | Entry Point | Purpose | Port |
|---|---|---|---|
| `DT-api` | `api/app.py` | Primary REST API (submission intake, leaderboards) | 31323 |
| `DTcore` | `bots/main.py` | Primary Discord bot (slash commands, notifications, lootboard updates) | — |
| `DT-webhooks` | `bots/webhook_bot.py` | Discord bot that reads webhook-channel messages (legacy fallback) | — |
| `DT-lootboards` | `lootboard/_board_generator.py` | Generates lootboard images every 2 min | — |
| `DT-hof` | `bots/hall_of_fame.py` | Hall of Fame image bot | — |
| `DT-heartbeat` | `bots/heartbeat.py` | Uptime/monitoring bot | — |

Dev API runs on port **31324** via `droptracker-api-dev` systemd unit (disabled by default, same `.env` as prod).

---

## Directory Map

```
droptracker/
├── api/                    # Quart REST API (port 31323)
│   ├── app.py              # Entry point: starts Hypercorn
│   ├── __init__.py         # create_app() – registers all blueprints
│   ├── core.py             # DB session factory, metrics, logger
│   ├── routes/
│   │   ├── webhook.py      # POST /webhook  ← primary submission intake
│   │   ├── players.py      # /top_players, /player_search, /player, etc.
│   │   ├── groups.py       # /top_groups, /group_search, /groups/…
│   │   ├── group_create.py # /groups/create (web wizard flow)
│   │   ├── video.py        # /presigned_upload_url, /video/…
│   │   ├── utils.py        # /debug_logs, /check, /metrics
│   │   ├── health.py       # /ping, /health
│   │   └── helpers.py      # assemble_submission_data()
│   └── services/
│       └── metrics.py      # Per-request metrics tracker
│
├── bots/
│   ├── main.py             # Primary Discord bot
│   ├── webhook_bot.py      # Legacy webhook-channel reader bot
│   ├── hall_of_fame.py     # HOF image bot
│   └── heartbeat.py        # Heartbeat bot
│
├── commands/               # Discord slash command handlers
│   ├── user.py             # /help, /accounts, /settings, etc.
│   ├── admin.py            # /create-group, /webhooks, etc.
│   ├── group_admin.py      # Point adjustments, audit log
│   └── utils.py            # try_create_user, is_admin, is_user_authorized
│
├── data/
│   └── submissions/        # All submission processor functions
│       ├── __init__.py     # Re-exports all processors
│       ├── common.py       # Shared helpers: auth, player lookup, notification creation
│       ├── drop.py         # drop_processor()       ← most complex, most important
│       ├── pb.py           # pb_processor()
│       ├── clog.py         # clog_processor()
│       ├── ca.py           # ca_processor()
│       ├── pet.py          # pet_processor()
│       ├── quest.py        # quest_processor()
│       ├── experience.py   # experience_processor()
│       ├── adventure_log.py# adventure_log_processor()
│       └── point_awards.py # check_and_award_points()
│
├── db/
│   ├── models/
│   │   ├── __init__.py     # Exports all ORM models
│   │   ├── base.py         # SQLAlchemy engine + session (data + xenforo DBs)
│   │   ├── drop.py         # Drop model (core loot record)
│   │   ├── player.py       # Player (OSRS account)
│   │   ├── user.py         # User (Discord account)
│   │   ├── group.py        # Group (clan)
│   │   ├── group_configuration.py  # K-V config per group
│   │   ├── notification_queue.py   # Pending Discord notifications
│   │   ├── notified_submission.py  # Sent notification dedup table
│   │   ├── seasonal_*.py   # Mirror tables for League/Seasonal worlds
│   │   └── ...             # (see docs/ARCHITECTURE.md for full schema)
│   ├── ops.py              # DatabaseOperations (create_drop_object, etc.)
│   ├── app_logger.py       # Structured JSON logger
│   └── xf/                 # XenForo DB operations
│
├── services/
│   ├── notification_service.py  # Polls notification_queue, sends Discord embeds
│   ├── redis_updates.py         # RedisLootTracker – incremental leaderboard updates
│   ├── points.py                # Premium point system (award/debit ledger)
│   ├── message_handler.py       # Discord MessageCreate/Component event handler
│   ├── hall_of_fame.py          # HOF image generation
│   └── ...
│
├── lootboard/              # Lootboard image generation
│   ├── _board_generator.py # Watchdog loop (generates every 2 min)
│   ├── board_generator.py  # Generates boards for all groups
│   ├── generator.py        # generate_server_board()
│   ├── flexible_generator.py
│   ├── player_board.py     # Per-player board
│   └── themes/             # PNG asset sets (square, rounded, runelite, etc.)
│
├── utils/
│   ├── redis.py            # RedisClient singleton
│   ├── wiseoldman.py       # WOM API helpers
│   ├── ge_value.py         # get_true_item_value() from GE API
│   ├── embeds.py           # Discord embed builders
│   ├── format.py           # format_number(), replace_placeholders(), etc.
│   ├── encrypter.py        # Fernet webhook URL encryption
│   ├── b2_storage.py       # Backblaze B2 presigned URLs
│   └── ...
│
├── alembic/                # DB migrations (env.py configured)
├── docs/                   # Architecture + planning docs
├── games/                  # Board game / Gielinor Race mini-game
├── scripts/                # One-off maintenance scripts
├── .env.example            # All environment variable keys
├── requirements.txt
└── real_startup.sh         # Production startup (GNU screen)
```

---

## API Endpoints (Quick Reference)

**Submission intake:**
- `POST /webhook` — Primary RuneLite plugin submission (rate: 100/s)
- `POST /submit` — Alias (rate: 10/s)
- `POST /manual-submit` — Web frontend submission (rate: 10/s)

**Players:**
- `GET /top_players` — Global leaderboard (Redis, cached 20s)
- `GET /player_search?q=<name>` — Player name search
- `GET /player?id=<id>` or `?name=<name>` — Player lookup

**Groups:**
- `GET /top_groups` — Top groups by monthly loot (cached 20s)
- `GET /group_search?q=<name>` — Group search
- `POST /groups/create` — Create group (server-to-server, `XF_KEY` auth)
- `GET /groups/guild-status/<guild_id>` — Discord guild registration check
- `POST /generate-timeframe-board` — Generate scoped lootboard image

**Video:**
- `GET /presigned_upload_url` — B2 presigned PUT URL
- `POST /video/upload-complete` — Mark upload done, trigger processing

**Health:**
- `GET /ping` — Liveness probe
- `GET /health` — DB + Redis health check

---

## Submission Processing (Summary)

A full walkthrough is in `docs/SUBMISSION_PIPELINE.md`. Short version:

1. RuneLite plugin posts `multipart/form-data` to `POST /webhook` with `payload_json` + optional screenshot
2. `api/routes/webhook.py` parses it, routes by `type` field to the appropriate processor in `data/submissions/`
3. Each processor: deduplicates → validates player/item/NPC → calls WOM API to confirm identity → writes DB row → updates Redis leaderboard → creates `NotificationQueue` entry
4. `services/notification_service.py` (background task, polls every 3s) picks up queued notifications, builds Discord embeds, sends to the group's configured channel

**Submission types:** `drop`, `pb` (personal best), `clog` (collection log), `ca` (combat achievement), `pet`, `quest`, `experience`, `adventure_log`

---

## Key Architectural Rules

**WOM is the identity source of truth.** Every submission calls the Wise Old Man API to resolve the canonical RSN and WOM ID. A `Player` row is only created once WOM confirms the account exists. Never trust the submitted `player_name` alone.

**High-value drop verification.** Drops > 1M GP trigger a semantic check via the OSRS Wiki API (backed by OpenAI) to confirm the item can actually drop from the stated NPC. This blocks spoofed submissions. See `data/submissions/drop.py`.

**Deduplication is multi-layered.** Each submission includes a `unique_id` (GUID). Checked first against an in-memory `unique_id_cache` (up to 1000 entries per type), then against the DB for duplicates within the past hour.

**Seasonal / League worlds are mirrored.** All submission types have `seasonal_*` DB tables, and group configs use `seasonal_`-prefixed keys. The `world_type` field in the payload determines which tables are used.

**Notification flow is async.** Processors write to `notification_queue`, they do not send Discord messages directly. `NotificationService` drains the queue independently.

**Known latency issue (see `docs/REFACTOR_PLAN.md`).** The webhook handler currently blocks until all processing (DB + WOM + OSRS API) finishes. Under load this causes 10–40s response times. The planned fix (async Redis queue with background consumer) is not yet implemented.

---

## Database Overview

Two MySQL databases:

- **`data`** — Application DB (all tables below)
- **`xenforo`** — Forum DB (read/write for XenForo integration)

Key models: `Drop`, `Player`, `User`, `Group`, `Guild`, `GroupConfiguration`, `NotificationQueue`, `NotifiedSubmission`, `ItemList`, `NpcList`, `PersonalBestEntry`, `CollectionLogEntry`, `CombatAchievementEntry`, `PlayerPet`, `QuestCompletionEntry`, `VideoUpload`, `GroupEmbed`, `Webhook`, `PlayerPoints`, `PointCredit`, `PointDebit`

See `db/models/` for ORM definitions and `docs/ARCHITECTURE.md` for full schema reference.

**Redis key patterns:**
- `leaderboard:{YYYYMM}` — Global sorted set (player_id → total loot GP)
- `leaderboard:group:{group_id}:{YYYYMM}` — Per-group sorted set
- `leaderboard:npc:{npc_id}:{YYYYMM}` — Per-NPC sorted set
- `player:{player_id}:{YYYYMM}:total_loot` — String: total GP this month

---

## Common Group Configuration Keys

Stored in `group_configurations` table (key-value per group):

| Key | Purpose |
|---|---|
| `drop_channel_id` | Discord channel to post drop notifications |
| `minimum_value_to_notify` | GP threshold for drop announcements (default: 2.5M) |
| `only_send_messages_with_images` | Require screenshots before notifying |
| `send_stacks_of_items` | Announce stackable items (e.g. runes) |
| `lootboard_channel_id` / `lootboard_message_id` | Where to post the lootboard |
| `repost_lootboard` | Delete + repost vs. edit existing message |
| `split_gp_tracking` | Enable GP split tracking for raids |
| `loot_board_type` | Visual theme for the lootboard |
| `seasonal_*` prefixes | Same keys but for League/Seasonal worlds |

---

## Development Setup

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in: DB_USER, DB_PASS, BOT_TOKEN (or DEV_TOKEN), WOM_API_KEY,
#          XF_KEY, ENCRYPTION_KEY, WEBHOOK_TOKEN, etc.

# 3. Run migrations
cp alembic.ini.template alembic.ini
# Edit alembic.ini sqlalchemy.url with DB credentials
alembic upgrade head

# 4. Start individual processes (dev)
python api/app.py           # REST API on port 31323
python bots/main.py         # Discord bot
python bots/webhook_bot.py  # Webhook channel bot (optional)
python lootboard/_board_generator.py  # Lootboard generator

# 5. Production (all processes via screen)
bash real_startup.sh
```

`STATE=dev` in `.env` uses `DEV_TOKEN` instead of `BOT_TOKEN`. Dev API port is 31324 (systemd unit: `droptracker-api-dev`).

---

## Where to Look for What

| Task | Start here |
|---|---|
| Change how drops are processed | `data/submissions/drop.py` |
| Change how any other submission type works | `data/submissions/<type>.py` |
| Change API endpoint behavior | `api/routes/webhook.py` (submissions) or the relevant routes file |
| Add/modify a Discord slash command | `commands/user.py`, `commands/admin.py`, or `commands/group_admin.py` |
| Change notification embed format | `utils/embeds.py` + `db/models/embed.py` (GroupEmbed) |
| Change leaderboard ranking logic | `services/redis_updates.py` |
| Change lootboard image layout | `lootboard/generator.py` or `lootboard/flexible_generator.py` |
| Add/change DB schema | Create Alembic migration in `alembic/versions/` |
| Change how players are authenticated | `data/submissions/common.py → ensure_player_and_auth()` |
| Change group configuration options | `db/models/group_configuration.py` + wherever config is read |
| Points/premium features | `services/points.py`, `data/submissions/point_awards.py` |
| Video upload flow | `api/routes/video.py`, `services/video_worker.py`, `utils/b2_storage.py` |
| XenForo integration | `db/xf/`, `services/xf_services.py` |
