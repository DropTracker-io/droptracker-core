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
| Process management | systemd units + watchdog (unit files in `deploy/systemd/`) |
| Config | python-dotenv (`.env`) |

---

## Running Processes

Production runs as **systemd units** (`/etc/systemd/system/droptracker-*.service`; canonical copies in `deploy/systemd/`).

| Systemd Unit | Entry Point | Purpose | Port |
|---|---|---|---|
| `droptracker-api` | `api/app.py` (`api:create_app()`) | Submission intake + public read API | 31323 |
| `droptracker-webapi` | `web_api/app.py` | Website backend `/api/v1` (auth, profiles, config, events, admin, SSE) | 31325 |
| `droptracker-node-blue` / `droptracker-node-green` | Next.js servers (separate `droptracker-web` repo at `/store/droptracker/web`); zero-downtime blue-green pair behind nginx `upstream droptracker_node` | Website frontend / BFF | 31380 / 31381 |
| `droptracker-node` | **Deploy trigger** — oneshot that runs web `scripts/deploy.sh`; `systemctl restart droptracker-node` = full zero-downtime deploy (build idle colour → health-check → flip nginx). NOT a server. | Web deploy | — |
| `droptracker-core` | `bots/main.py` | Primary Discord bot (slash commands, notifications, lootboard updates) | — |
| `droptracker-webhooks` | `bots/webhook_bot.py` | Discord bot that reads webhook-channel messages (legacy fallback) | — |
| `droptracker-lootboards` | `lootboard/_board_generator.py` | Generates lootboard images every 2 min | — |
| `droptracker-hof` | `bots/hall_of_fame.py` | Hall of Fame image bot | — |
| `droptracker-player-updates` | `data/player_total_updater.py` | Background WOM sync + Redis leaderboard maintenance | — |
| `droptracker-video-worker` | `services/video_worker.py` | MJPEG→MP4 conversion via FFmpeg + Backblaze B2 | — |
| `droptracker-heartbeat` | `bots/heartbeat.py` | Uptime heartbeat bot | — |
| `droptracker-events` | `workers/event_consumer.py` | Events v2: drains `events:submissions`, applies task/bingo/team progress | — |
| `droptracker-webhook-consumer` | `workers/webhook_consumer.py` | Drains `webhook:queue` (idles unless `WEBHOOK_QUEUE_MODE=true`) | — |

Canonical unit files live in `deploy/systemd/` (install with `cp` + `daemon-reload`, see its README). Dev API runs on port **31324** via `droptracker-api-dev` systemd unit (disabled by default, same `.env` as prod).

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
├── web_api/                # Quart website API (port 31325, systemd droptracker-webapi)
│   ├── app.py              # Entry point
│   ├── __init__.py         # create_app() – registers 20 blueprints under /api/v1
│   ├── session.py          # JWT mint/verify (HS256, JWT_TOKEN_KEY), Redis deny-list
│   ├── deps.py             # current_user_id(), resolve_group_role() (superadmin/owner/admin/member)
│   ├── config_registry.py  # Group config schema: field defs, coercion, limits
│   ├── entitlements*.py    # Premium feature gates by subscription tier
│   ├── billing.py          # PayPal checkout/subscription logic
│   └── routes/             # auth, me, profiles, search, leaderboards, config,
│                           # group_admin, events, event_admin, event_discord,
│                           # badges, announcements, docs (CMS), submissions,
│                           # subscriptions, paypal_ipn, lootboard, realtime (SSE),
│                           # npcs (drop table + totals), items (item pages),
│                           # admin (superadmin), health
│
├── workers/                # Redis queue consumers (systemd: droptracker-events, droptracker-webhook-consumer)
│   ├── event_consumer.py   # Events v2: BRPOP events:submissions → engine apply
│   └── webhook_consumer.py # Fast-accept intake: drains webhook:queue (WEBHOOK_QUEUE_MODE)
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
│   ├── realtime.py              # Publishes rt:* pub/sub channels + feed:recent (web SSE)
│   ├── points.py                # Premium point system (award/debit ledger)
│   ├── badges.py                # Badge award engine (daily champion, streaks, boss records)
│   ├── event_engine.py          # Events v2: producer / matcher / apply layers
│   ├── event_lifecycle.py       # Events v2 state machine (draft → active → past) + sweep
│   ├── event_notifications.py   # Events v2 per-event Discord channel routing
│   ├── message_handler.py       # Discord MessageCreate/Component event handler
│   ├── hall_of_fame.py          # HOF image generation
│   ├── video_worker.py          # MJPEG→MP4 conversion worker (systemd unit)
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
├── osrs_api/               # External clients: WOM, GE pricing, semantic drop verification
├── monitor/                # Service control CLI + systemd watchdog integration
├── games/events/task_store/ # Seed data for events v2 task library (scripts/seed_event_task_library.py)
├── alembic/                # DB migration env (versions/ NOT committed — see CONTRIBUTING.md)
├── docs/                   # Architecture + planning docs (archive/ holds legacy-events backup)
├── scripts/                # One-off maintenance scripts
├── tests/                  # pytest suite (unit/ runs in CI; integration/ needs DB+Redis)
├── .env.example            # All environment variable keys
└── requirements.txt
```

**Legacy:** dead entry points (screen launchers, events v1 `eventBot.py`/`worker.py`, root duplicates of `webhook_bot.py`/`commands.py`, `games/gielinor_race/`, `api/backup/`) were **deleted in the 2026-07-06 audit** — recover from git history or `docs/archive/legacy-events/` if ever needed. Still present but not for new work: `web/` + `templates/` (old Jinja2 site, registered as blueprints in the core bot for backward compat; superseded by Next.js + `web_api/`), and the `board_cli.py`/`timeframe_board_cli.py` subprocess helpers.

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

**Intake latency (see `docs/REFACTOR_PLAN.md`).** By default the webhook handler blocks until all processing (DB + WOM + OSRS API) finishes — 10–40s under load. Phase 1 of the fix is implemented behind a flag: with `WEBHOOK_QUEUE_MODE=true`, the handler fast-accepts (validate + stash image + RPUSH `webhook:queue`, ~50ms) and `workers/webhook_consumer.py` drains the queue in the background. Not yet the default.

**Events v2 pipeline.** Processors call `services/event_engine.queue_submission()` (LPUSH `events:submissions`, gated on `events:active`); `workers/event_consumer.py` matches submissions against active event tasks (pure `match_task()`), applies progress/bingo/team points, and routes Discord notifications via `services/event_notifications.py`. Each event's `submission_policy` gates credit by intake path (envelope `used_api` flag): `all` (default), `confirm_non_api` (non-plugin submissions always land as pending completions), or `api_only` (non-plugin submissions ignored). Lifecycle transitions (draft → active → past) live in `services/event_lifecycle.py`; the web admin surface is `web_api/routes/events.py` + `event_admin.py` + `event_discord.py`.

---

## Database Overview

Two MySQL databases:

- **`data`** — Application DB (all tables below)
- **`xenforo`** — Forum DB (read/write for XenForo integration)

Core models: `Drop`, `Player`, `User`, `Group`, `Guild`, `GroupConfiguration`, `NotificationQueue`, `NotifiedSubmission`, `ItemList`, `NpcList`, `PersonalBestEntry`, `CollectionLogEntry`, `CombatAchievementEntry`, `PlayerPet`, `QuestCompletionEntry`, `VideoUpload`, `GroupEmbed`, `Webhook`, `PlayerPoints`, `PointCredit`, `PointDebit`

Newer model families (~80 tables total):
- **Events v2** (`db/models/events.py`): `Event`, `EventTask`, `EventTeam`, `EventTeamMember`, `EventBingoCell`, `EventBingoCompletion`, `EventCompletion`, `EventProgress`, `EventTaskLibraryItem`, `EventChannel`
- **Badges** (`db/models/badge.py`): `Badge`, `PlayerBadge` (dedupe via unique `(badge_id, group_key, active_key)`)
- **Subscriptions** (`db/models/subscriptions.py`): `SubscriptionTier`, `GroupSubscription`
- **Web/admin**: `GroupAdmin`, `AuditLog`, `Announcement`, `DocsPage`, `UserConfiguration`
- **Split tracking**: `DropSplit`
- **Seasonal mirrors**: `Seasonal{Drop,PersonalBestEntry,CollectionLogEntry,CombatAchievementEntry,PlayerPet,QuestCompletionEntry}`
- **Analytics**: `PlayerItemHourlyTotals`, `PlayerNpcHourlyTotals`, `PlayerDailyAggregates`, `HistoricalMetrics`

See `db/models/` for ORM definitions and `docs/ARCHITECTURE.md` for full schema reference.

**Redis key patterns** (see `services/redis_updates.py`, `services/realtime.py`, `web_api/common.py`):
- `leaderboard:{YYYYMM}` — Monthly global sorted set (player_id → total loot GP)
- `leaderboard:{YYYYMM}:group:{gid}` — Per-group monthly
- `leaderboard:{YYYYMMDD}` / `leaderboard:{ISO-week}` / `leaderboard:all` — Daily / weekly / all-time boards
- `player:{player_id}:{YYYYMM}:total_loot` — String: total GP this month
- `rt:global`, `rt:feed`, `rt:group:{gid}`, `rt:player:{pid}`, `rt:npc:{npc_id}`, `rt:event:{eid}` — Pub/sub for web SSE (`GET /api/v1/stream`)
- `feed:recent` — Capped list backing the live drop ticker (`GET /api/v1/feed/recent`)
- `events:submissions` / `events:active` — Events v2 queue + active-event gate
- `webhook:queue` — Fast-accept intake queue (`WEBHOOK_QUEUE_MODE=true`)

---

## Common Group Configuration Keys

Stored in `group_configurations` table (key-value per group):

| Key | Purpose |
|---|---|
| `channel_id_to_post_loot` | Discord channel to post drop notifications (per-type overrides: `channel_id_to_post_{levels,pb,ca,pets,quests,clog,deaths,diaries}`) |
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

# 3. Database
cp alembic.ini.template alembic.ini
# Edit alembic.ini sqlalchemy.url with DB credentials
# NOTE: alembic/versions/ is gitignored — a fresh clone cannot
# `alembic upgrade head` from zero. Get a schema dump from a maintainer,
# then use Alembic for incremental changes.

# 4. Start individual processes (dev)
python -m api.app                     # intake API :31323
python -m web_api.app                 # website API :31325
python -m bots.main                   # core Discord bot
python -m bots.webhook_bot            # webhook channel bot (optional)
python -m lootboard._board_generator  # lootboard generator
python -m workers.event_consumer      # events v2 consumer (if testing events)
```

Production is managed via systemd: `systemctl status 'droptracker-*'`.

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
| Events v2 (tasks, bingo, teams) | `services/event_engine.py`, `workers/event_consumer.py`, `web_api/routes/events.py`, `db/models/events.py` |
| Badges | `services/badges.py`, `db/models/badge.py`, `web_api/routes/badges.py` |
| Website API endpoint (`/api/v1/...`) | `web_api/routes/<area>.py`; auth/roles in `web_api/deps.py` |
| Website auth/session | `web_api/session.py` (JWT), `web_api/routes/auth.py` (Discord OAuth) |
| Live drop feed / SSE | `services/realtime.py` (publish), `web_api/routes/realtime.py` (stream) |
| Subscriptions / PayPal | `web_api/billing.py`, `web_api/routes/subscriptions.py`, `web_api/routes/paypal_ipn.py` |
