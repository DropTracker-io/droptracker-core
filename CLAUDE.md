# DropTracker — Agent Reference

This file is auto-loaded by Claude Code. It gives a full orientation to the codebase so agents can work efficiently without repeating broad exploration.

Active development branch: **`new-api`** (branch from it, target it in PRs). The frontend is a **separate repo** at `/store/droptracker/web` (Next.js; see its own CLAUDE.md).

---

## What Is This?

DropTracker is an Old School RuneScape (OSRS) loot and achievement tracking platform for clans and individual players. A custom RuneLite plugin (separate repo) sends submissions to this backend, which processes them, stores results in MySQL, updates Redis leaderboards, generates lootboard images, and posts Discord notifications.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | **Python 3.11** (`venv/` is 3.11.2 — see the version gotcha below) |
| Web / API | Quart 0.19.6 + Hypercorn 0.17 (ASGI) |
| Discord | discord-py-interactions 5.16 |
| Database | MySQL/MariaDB (two schemas: `data` + `xenforo`) via SQLAlchemy 2.0 + PyMySQL |
| Cache / Leaderboards / queues | Redis (sorted sets, key-value, lists, pub/sub) |
| Migrations | Alembic 1.13 (`alembic/versions/` is **gitignored**) |
| Image generation | Pillow 12 (+ `services/boardgen/` SVG canvas for board-game maps) |
| External APIs | Wise Old Man (WOM), OSRS Wiki/GE, OpenAI (semantic NPC checks) |
| Billing | **Stripe** (primary) + legacy PayPal IPN (see Billing below) |
| Video storage | Backblaze B2 (prod) or local filesystem |
| Auth / crypto | Fernet (cryptography), PyJWT |
| Forum | XenForo (separate DB, integrated via API) |
| Local embeddings | fastembed (ONNX/CPU) for the knowledgebase |
| Process management | systemd units + watchdog (unit files in `deploy/systemd/`) |
| Config | python-dotenv (`.env`) |

> **Python version gotcha.** CI (`.github/workflows/ci.yml`) installs **3.12**, but the production `venv/` this box actually runs is **3.11.2**. Anything 3.12-only (PEP 695 `type` aliases, new generic syntax) passes CI and then crashes the services. Target 3.11.

---

## Running Processes

Production runs as **systemd units** (`/etc/systemd/system/droptracker-*.service`; canonical copies in `deploy/systemd/`).

> **Two service accounts.** The bots, intake API and workers run as `User=user`; **`droptracker-webapi` and the node units run as `User=debian`**. Any directory both sides write (shared `clans/` asset trees, generated images) must be group-writable/0777 or one process silently fails to write the other's files.

| Systemd Unit | Entry Point | Purpose | Port |
|---|---|---|---|
| `droptracker-api` | `api/app.py` (`api:create_app()`, 6 hypercorn workers) | Submission intake + plugin endpoints + public read API | 31323 |
| `droptracker-webapi` | `web_api/app.py` | Website backend `/api/v1` (auth, profiles, config, events, admin, SSE) | 31325 |
| `droptracker-node-blue` / `droptracker-node-green` | Next.js servers (separate repo at `/store/droptracker/web`); zero-downtime blue-green pair behind nginx `upstream droptracker_node` | Website frontend / BFF | 31380 / 31381 |
| `droptracker-node` | **Deploy trigger** — oneshot that runs web `scripts/deploy.sh`; `systemctl restart droptracker-node` = full zero-downtime deploy. NOT a server. | Web deploy | — |
| `droptracker-core` | `bots/main.py` | Primary Discord bot (slash commands, notifications, lootboard + event-board updates, outbox drain) | — |
| `droptracker-webhooks` | `bots/webhook_bot.py` | Webhook-channel reader bot (legacy fallback) + suggestion-forum sync | — |
| `droptracker-lootboards` | `lootboard/_board_generator.py` | Generates lootboard images every 2 min | — |
| `droptracker-hof` | `bots/hall_of_fame.py` | Hall of Fame image bot | — |
| `droptracker-player-updates` | `data/player_total_updater.py` | Background WOM sync + Redis leaderboard maintenance | — |
| `droptracker-video-worker` | `services/video_worker.py` | MJPEG→MP4 conversion via FFmpeg + Backblaze B2 | — |
| `droptracker-heartbeat` | `bots/heartbeat.py` | Uptime heartbeat bot | — |
| `droptracker-events` | `workers/event_consumer.py` | Events v2: drains `events:submissions`, applies task/bingo/team progress | — |
| `droptracker-webhook-consumer` | `workers/webhook_consumer.py` | Drains `webhook:queue` — **live** (`WEBHOOK_QUEUE_MODE=true` in prod `.env`) | — |

**Timers:** `droptracker-db-backup.timer` (08:30 UTC → `scripts/db_backup.sh`, MariaDB + Redis to local + B2) and `droptracker-prune-images.timer` (04:00 UTC → `scripts.prune_drop_images --apply`, low-value screenshots past 30d).

Canonical unit files live in `deploy/systemd/` (install with `cp` + `daemon-reload`, see its README). Dev API runs on port **31324** via `droptracker-api-dev` (disabled by default, same `.env` as prod).

---

## Directory Map

```
droptracker/
├── api/                    # Quart intake API (port 31323)
│   ├── app.py              # Entry point: starts Hypercorn
│   ├── __init__.py         # create_app() – registers all blueprints
│   ├── core.py             # DB session factory, metrics, logger
│   ├── routes/
│   │   ├── webhook.py      # POST /webhook  ← primary submission intake
│   │   ├── notifications.py# Plugin event endpoints: /notifications (long-poll
│   │   │                   #   inbox), /event_state (HUD), /events/<id>/board.png
│   │   ├── players.py      # /top_players, /player_search, /player, etc.
│   │   ├── groups.py       # /top_groups, /group_search, /groups/…
│   │   ├── group_create.py # /groups/create (web wizard flow)
│   │   ├── group_export.py # /groups/<id>/export/… (key-authed group data API)
│   │   ├── video.py        # /presigned_upload_url, /video/…
│   │   ├── utils.py        # /debug_logs, /check, /metrics
│   │   ├── health.py       # /ping, /health
│   │   └── helpers.py      # assemble_submission_data()
│   └── services/metrics.py # Per-request metrics tracker
│
├── web_api/                # Quart website API (port 31325, systemd droptracker-webapi)
│   ├── app.py              # Entry point
│   ├── __init__.py         # create_app() – registers 38 blueprints under /api/v1
│   ├── session.py          # JWT mint/verify (HS256, JWT_TOKEN_KEY), Redis deny-list
│   ├── deps.py             # current_user_id(), resolve_group_role()
│   ├── config_registry.py  # Group config schema (~59 keys): defs, coercion, limits
│   ├── entitlements*.py    # Premium feature gates by subscription tier
│   ├── admin_registry.py   # Superadmin data-browser table registry
│   ├── billing.py          # Stripe checkout/portal/webhook (falls back to `manual`)
│   ├── payments.py         # subscription_payments ledger (Stripe + PayPal + backfill)
│   ├── event_*.py          # Event read-model helpers (breakdown, leadership, loot, players, prizes)
│   └── routes/             # 41 modules. Site: auth, me, profiles, search, leaderboards,
│                           #   config, group_admin, badges, announcements, docs (CMS),
│                           #   submissions, manual_submissions, subscriptions, paypal_ipn,
│                           #   lootboard, realtime (SSE), npcs, items, item_values,
│                           #   personal_bests, points, player_claims, embeds, redirects,
│                           #   resolve, meta, suggestions, tickets, admin, health
│                           # Events v2: events, event_admin, event_audit, event_board,
│                           #   event_discord, event_layouts, event_participants,
│                           #   event_prizes, event_task_validation, event_templates
│
├── workers/                # Redis queue consumers
│   ├── event_consumer.py   # Events v2: BRPOP events:submissions → engine apply
│   └── webhook_consumer.py # Fast-accept intake: drains webhook:queue (LIVE in prod)
│
├── bots/
│   ├── main.py             # Primary Discord bot (core)
│   ├── webhook_bot.py      # Webhook-channel reader + suggestion-forum sync
│   ├── hall_of_fame.py     # HOF image bot
│   └── heartbeat.py        # Heartbeat bot
│
├── commands/               # Discord slash command handlers
│   ├── user.py             # /help, /accounts, /settings, /claim-rsn, etc.
│   ├── admin.py            # /create-group, /webhooks, etc.
│   ├── group_admin.py      # Point adjustments, audit log
│   ├── submissions.py      # /submit drop|clog|pb|ca|pet — manual submissions
│   │                       #   (item/NPC autocomplete; forwards to /manual-submit)
│   └── utils.py            # try_create_user, is_admin, is_user_authorized
│
├── data/
│   ├── player_total_updater.py  # WOM sync + leaderboard maintenance loop
│   └── submissions/        # All submission processor functions
│       ├── __init__.py     # Re-exports all processors
│       ├── common.py       # Shared helpers: auth, player/NPC lookup, notifications
│       ├── drop.py         # drop_processor()       ← most complex, most important
│       ├── pb.py  clog.py  ca.py  pet.py  quest.py  experience.py
│       ├── death.py  diary.py  adventure_log.py
│       ├── manual_policy.py     # Rules for web/manual (non-plugin) submissions
│       ├── manual_discord.py    # Pure helpers for the Discord /submit commands
│       └── point_awards.py      # check_and_award_points()
│
├── db/
│   ├── models/             # ORM models (see Database Overview)
│   ├── ops.py              # DatabaseOperations (create_drop_object, etc.)
│   ├── clan_sync.py        # WOM group ↔ Group membership reconciliation
│   ├── group_creation.py   # Shared group-creation service (bot + web wizard)
│   ├── player_claims.py    # Shared RSN claim/unclaim service (bot + web)
│   ├── entitlements.py     # Tier lookups used outside web_api
│   ├── event_rate_limits.py# Per-tier event frequency caps
│   ├── app_logger.py       # Structured JSON logger
│   └── xf/                 # XenForo DB operations
│
├── services/
│   ├── notification_service.py  # Polls notification_queue (3s), sends Discord embeds
│   ├── discord_outbox.py        # Web API enqueues → core bot drains (web_api never
│   │                            #   opens a Discord connection — hard architectural rule)
│   ├── redis_updates.py         # RedisLootTracker – incremental leaderboard updates
│   ├── realtime.py              # Publishes rt:* pub/sub + feed:recent (web SSE)
│   ├── points.py                # Premium point system (award/debit ledger)
│   ├── badges.py                # Badge award engine
│   ├── event_engine.py          # Events v2: producer / matcher / apply layers
│   ├── event_lifecycle.py       # Events v2 state machine (draft → active → past) + sweep
│   ├── event_notifications.py   # Events v2 Discord routing / embed specs
│   ├── event_message_layouts.py # DB-seeded message layouts (V2 line tokens)
│   ├── event_board.py           # Live standings message, edited in place (2-min sweep)
│   ├── event_board_image.py     # Board → PNG via page_screenshot of web /board-image/[id]
│   ├── event_signup*.py         # Sign-up window, Discord prompt, pool
│   ├── event_team_discord*.py   # Per-team channels/categories
│   ├── event_scheduled_events.py, event_types.py, event_wom_reconciler.py
│   ├── loot_sweep.py            # `loot_sweep` event kind scoring (nested groups, decay)
│   ├── boardgame_*.py, boardgen/ # Board-game event kind: engine, effects, shop, map render
│   ├── plugin_notifications.py  # Per-player Redis inbox drained by GET /notifications
│   ├── activity_launch*.py      # Discord Activity entry-point handler + launch card
│   ├── ticket_system.py, ticket_transcripts.py  # Support tickets (Discord + web)
│   ├── suggestion_sync.py       # Discord forum → web suggestions mirror
│   ├── channel_cache.py, channel_names.py, components.py, message_handler.py
│   ├── drop_moderation.py, submission_status.py, item_totals.py, npc_totals.py
│   ├── kb/                      # Knowledgebase (see its section below)
│   ├── hall_of_fame.py, video_worker.py, page_screenshot.py, wiki.py, xf_services.py
│   └── ...
│
├── lootboard/              # Lootboard image generation
│   ├── _board_generator.py # Watchdog loop (subprocess every 2 min)
│   ├── board_generator.py  # Generates boards for all groups
│   ├── generator.py        # generate_server_board()
│   ├── flexible_generator.py, player_board.py, timeframe.py
│   └── themes/             # PNG asset sets (gitignored)
│
├── utils/
│   ├── redis.py            # RedisClient singleton
│   ├── wiseoldman.py       # WOM API helpers
│   ├── ge_value.py         # get_true_item_value() from GE API
│   ├── value_overrides.py  # item_value_overrides table application
│   ├── npc_names.py        # npc_match_key/NPC_ALIASES/ENCOUNTER_NAME_ALIASES
│   │                       #   ← the ONLY correct way to compare two NPC names
│   ├── embeds.py           # Discord embed builders
│   ├── format.py           # format_number(), replace_placeholders(), etc.
│   ├── encrypter.py        # Fernet webhook URL encryption
│   ├── b2_storage.py       # Backblaze B2 presigned URLs
│   ├── item_images.py, pb_rank.py, pb_blocklist.py, partitions.py, group_config.py
│   └── ...
│
├── osrs_api/               # External clients: WOM, GE pricing, semantic drop verification
├── monitor/                # systemd watchdog integration (sdnotifier)
├── games/events/task_store/# Seed data for the events task library (only default.json is tracked)
├── alembic/                # DB migration env (versions/ NOT committed — see CONTRIBUTING.md)
├── docs/                   # ARCHITECTURE, SUBMISSION_PIPELINE, GROUP_EXPORT_API, LOOT_SWEEP,
│                           #   REFACTOR_PLAN + event plans; archive/ holds legacy-events backup
├── scripts/                # Maintenance + backfill + seed scripts (dry-run by default,
│                           #   `--apply`/`--commit` to write; see the idiom note below)
├── tests/                  # pytest: unit/ (89 modules, ~2300 tests, CI) + integration/ (DB+Redis)
├── deploy/systemd/         # Canonical unit + timer files
├── .env.example            # All environment variable keys
└── requirements.txt
```

**Repo hygiene — do not be misled by files on disk.** The working tree carries gitignored clutter that greps will hit but git does not track: `*.bak`/`*.bak2` copies beside live modules (e.g. `data/submissions/drop.py.bak`, `api/routes/webhook.py.bak2`), `missing_images.json`, `.trash-20260706/`, `__pycache__/`, and **stale Claude worktrees under `.claude/worktrees/`** holding full older copies of the repo. Always confirm you are editing the tracked path, not a copy. `git ls-files <path>` settles it.

**Legacy:** dead entry points (screen launchers, events v1 `eventBot.py`/`worker.py`, root duplicates, `games/gielinor_race/`, `api/backup/`) were **deleted in the 2026-07-06 audit** — recover from git history or `docs/archive/legacy-events/`. Still present but not for new work: `web/` + `templates/` (old Jinja2 site, still registered as blueprints in the core bot for backward compat), and the `board_cli.py`/`timeframe_board_cli.py` subprocess helpers.

---

## API Endpoints (Quick Reference)

**Submission intake:**
- `POST /webhook` — Primary RuneLite plugin submission (rate: 100/s)
- `POST /submit` — Alias (rate: 10/s)
- `POST /manual-submit` — Web frontend submission (rate: 10/s)

**Plugin event endpoints** (identity = `player_name` + `acc_hash`, hash-first):
- `GET /notifications` — Drains the per-player Redis inbox; `?wait=N` long-polls
- `GET /event_state` — Enhanced Display HUD / Events-tab state
- `GET /events/<id>/board.png` — Server-rendered board, team-scoped, roster-gated

**Players / Groups:** `/top_players`, `/player_search`, `/player`, `/top_groups`, `/group_search`, `POST /groups/create` (`XF_KEY` auth), `/groups/guild-status/<guild_id>`, `POST /generate-timeframe-board`

**Group export** (key-authed via `export_api_key` group config; see `docs/GROUP_EXPORT_API.md`): `/groups/<id>/export/{top-players,drops,members}`

**Video:** `GET /presigned_upload_url`, `POST /video/upload-complete`

**Health:** `GET /ping`, `GET /health`

---

## Submission Processing (Summary)

A full walkthrough is in `docs/SUBMISSION_PIPELINE.md`. Short version:

1. RuneLite plugin posts `multipart/form-data` to `POST /webhook` with `payload_json` + optional screenshot
2. `api/routes/webhook.py` validates, stashes the image, and (queue mode, see below) `RPUSH`es to `webhook:queue`
3. `workers/webhook_consumer.py` routes by `type` to the processor in `data/submissions/`
4. Each processor: deduplicates → validates player/item/NPC → resolves identity → writes DB row → updates Redis leaderboard → creates a `NotificationQueue` entry → queues the submission for Events v2
5. `services/notification_service.py` (polls every 3s) builds Discord embeds and sends them to the group's configured channel

**Submission types:** `drop`, `pb`, `clog`, `ca`, `pet`, `quest`, `experience`, `death`, `diary`, `adventure_log` (webhook.py accepts aliases — `npc`/`other`→drop, `player_death`→death, `achievement_diary`→diary, …)

---

## Key Architectural Rules

**WOM is the identity source of truth.** A `Player` row is only created once WOM confirms the account exists; never trust a submitted `player_name` alone. **But the intake hot path does not call WOM per submission** — resolution is cached/deferred and `droptracker-player-updates` handles refresh on its own cadence.

**Compare NPC names only via `utils/npc_names.py`.** `npc_match_key()` folds spelling, articles and aliases ("The Gauntlet" / "Crystalline Hunllef" / "gauntlet" → `gauntlet`). Chest/collective encounters attribute ALL loot to one canonical NPC row (Barrows brothers → `Barrows`, the Moons → `Lunar Chest`), so a raw display name is frequently not a real drop source.

**High-value drop verification.** Drops > 1M GP trigger a semantic check via the OSRS Wiki API (backed by OpenAI) to confirm the item can actually drop from the stated NPC. Fail-open by design. See `data/submissions/drop.py`.

**Deduplication is multi-layered.** Each submission includes a `unique_id` (GUID), checked against an in-memory cache (up to 1000 entries per type), then against the DB for duplicates within the past hour.

**Seasonal / League worlds are mirrored.** All submission types have `seasonal_*` DB tables, and group configs use `seasonal_`-prefixed keys. The payload's `world_type` selects the tables.

**Notification flow is async.** Processors write to `notification_queue`; they never send Discord messages directly.

**The Web API never opens a Discord connection.** `web_api` enqueues into `discord_outbox`; the core bot drains it. Anything needing the gateway belongs in a bot, not a route.

**Fast-accept intake is LIVE.** `WEBHOOK_QUEUE_MODE=true` in the production `.env`: `/webhook` validates + stashes + `RPUSH`es in ~50 ms and `workers/webhook_consumer.py` does the real work. (`.env.example` still ships `false` for fresh installs; `docs/REFACTOR_PLAN.md` describes the pre-queue behaviour.) Consequence: changing a processor requires restarting **`droptracker-webhook-consumer`**, not `droptracker-api`.

**Events v2 pipeline.** Processors call `services/event_engine.queue_submission()` (LPUSH `events:submissions`, gated on `events:active`); `workers/event_consumer.py` matches against active event tasks (pure `match_task()`), applies progress/bingo/team points, and routes Discord notifications via `services/event_notifications.py`. Each event's `submission_policy` gates credit by intake path (envelope `used_api` flag): `all` (default), `confirm_non_api` (non-plugin submissions land as pending completions), or `api_only`. Lifecycle transitions live in `services/event_lifecycle.py`; the admin surface is `web_api/routes/events.py` + `event_admin.py` + `event_discord.py`. Event **kinds** beyond the default task/bingo model: `loot_sweep` (`services/loot_sweep.py`, `docs/LOOT_SWEEP.md`) and the board game (`services/boardgame_*.py`, `services/boardgen/`).

**Maintenance scripts follow one idiom:** dry-run by default, `--apply`/`--commit` to write, idempotent. Run the dry-run, show the owner, then apply.

---

## Billing

**Stripe is the provider.** `web_api/billing.py` abstracts it and falls back to a `manual` provider when `STRIPE_SECRET_KEY` is unset (checkout grants the tier immediately, no portal). Subscription state is only ever changed by the provider webhook (`POST /api/v1/webhooks/billing`), never by the client. `web_api/payments.py` writes the `subscription_payments` ledger, idempotent on `external_id`.

**`web_api/routes/paypal_ipn.py` is legacy, not the current path.** XenForo-era PayPal agreements have `droptracker.io/payment_callback.php` baked in and cannot be re-pointed, so the Next.js app rewrites that exact path to this handler, which does what XF's callback + downgrade cron used to. Do not build new billing on it.

---

## Database Overview

Two MySQL/MariaDB databases: **`data`** (application) and **`xenforo`** (forum, read/write).

Core models: `Drop`, `Player`, `User`, `Group`, `Guild`, `GroupConfiguration`, `NotificationQueue`, `NotifiedSubmission`, `ItemList`, `NpcList`, `PersonalBestEntry`, `CollectionLogEntry`, `CombatAchievementEntry`, `PlayerPet`, `QuestCompletionEntry`, `PlayerDeath`, `DiaryCompletion`, `VideoUpload`, `GroupEmbed`, `Webhook`

Model families (~90 tables):
- **Events v2** (`db/models/events.py`): `Event`, `EventTask`, `EventTeam`, `EventTeamMember`, `EventBingoCell`, `EventBingoCompletion`, `EventCompletion`, `EventProgress`, `EventTaskLibraryItem`, `EventChannel`
- **Points** (`group_points.py`): `PlayerPoints`, `GroupPointConfig`, `GroupPointMods`, `GroupPointTimedEvent`, `GroupPointSeason`, `GroupPointBlacklist`
- **Web/admin** (`web.py`): `GroupAdmin`, `GroupEventManager`, `Announcement`, `DiscordOutbox`, `AuditLog`, `DocsPage`; plus `UserConfiguration`
- **Support**: `Ticket`, `TicketMessage` (`tickets.py`); suggestions mirror tables
- **Badges** (`badge.py`): `Badge`, `PlayerBadge` (unique `(badge_id, group_key, active_key)`)
- **Subscriptions** (`subscriptions.py`): `SubscriptionTier`, `GroupSubscription`, `SubscriptionPayment`
- **Moderation / valuation**: `DropGroupModeration`, `ItemValueOverride`
- **Knowledgebase** (`knowledgebase.py`): `kb_documents`, `kb_chunks`, `kb_ingest_state`
- **Split tracking**: `DropSplit`
- **Seasonal mirrors**: `Seasonal{Drop,PersonalBestEntry,CollectionLogEntry,CombatAchievementEntry,PlayerPet,QuestCompletionEntry}`
- **Analytics** (`analytics.py`): `PlayerItemHourlyTotals`, `PlayerNpcHourlyTotals`, `GroupRecentDrops`, `PlayerDailyAggregates`, `PlayerLootData`, `PlayerExperience`, `HistoricalMetrics`

See `db/models/` for ORM definitions and `docs/ARCHITECTURE.md` for the schema reference.

**Redis key patterns** (see `services/redis_updates.py`, `services/realtime.py`, `web_api/common.py`):
- `leaderboard:{YYYYMM}` — Monthly global sorted set (player_id → total loot GP)
- `leaderboard:{YYYYMM}:group:{gid}` — Per-group monthly
- `leaderboard:{YYYYMMDD}` / `leaderboard:{ISO-week}` / `leaderboard:all` — Daily / weekly / all-time
- `player:{player_id}:{YYYYMM}:total_loot` — String: total GP this month
- `rt:{scope}` where scope ∈ `global` | `feed` | `group:{id}` | `player:{id}` | `npc:{id}` | `event:{id}` — Pub/sub for web SSE (`GET /api/v1/stream`)
- `feed:recent` — Capped list backing the live drop ticker (`GET /api/v1/feed/recent`)
- `events:submissions` / `events:active` — Events v2 queue + active-event gate
- `plugin:notify:{player_id}` — Per-player in-game notification inbox
- `webhook:queue` — Fast-accept intake queue (live)

---

## Common Group Configuration Keys

Stored in `group_configurations` (key-value per group). The authoritative schema — ~59 keys with types, defaults and limits — is `web_api/config_registry.py`; add new keys there (and in the frontend's `packages/api-types`), never hardcoded in a form.

| Key | Purpose |
|---|---|
| `channel_id_to_post_loot` | Drop notification channel (per-type overrides: `channel_id_to_post_{levels,pb,ca,pets,quests,clog,deaths,diaries}`) |
| `minimum_value_to_notify` | GP threshold for drop announcements (default: 2,500,000) |
| `only_send_messages_with_images` | Require screenshots before notifying |
| `send_stacks_of_items` | Announce stackable items (e.g. runes) |
| `lootboard_channel_id` / `lootboard_message_id` | Where to post the lootboard |
| `repost_lootboard` | Delete + repost vs. edit existing message |
| `split_gp_tracking` | Enable GP split tracking for raids |
| `loot_board_type` | Visual theme for the lootboard |
| `export_api_key` | Enables the key-authed group export API |
| `activity_launch_channel` | Channel for the standing "Open DropTracker" activity card |
| `seasonal_*` prefixes | Same keys but for League/Seasonal worlds |

---

## Development Setup

```bash
# 1. Install deps  (target Python 3.11 — see the version gotcha above)
pip install -r requirements.txt -r requirements-dev.txt

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
python -m workers.webhook_consumer    # REQUIRED to process intake in queue mode
python -m workers.event_consumer      # events v2 consumer
python -m lootboard._board_generator  # lootboard generator
```

**Tests:**

```bash
./venv/bin/python -m pytest tests/unit -q          # fast, fully mocked — what CI runs
./venv/bin/python -m pytest tests/integration -q   # needs live MySQL + Redis
```

`tests/conftest.py` stubs `db` and `services` as MagicMocks — route modules must lazy-import `services.*` **inside** handlers, and enum/tuple `in` checks against stubbed values silently fail (monkeypatch real tuples in endpoint tests).

Production is managed via systemd: `systemctl status 'droptracker-*'`. `STATE=dev` in `.env` uses `DEV_TOKEN` instead of `BOT_TOKEN` (prod is `STATE="live"`). Dev API port is 31324 (`droptracker-api-dev`).

**Before assuming code or schema state on this box, check live reality** (`alembic current`, `systemctl show <unit> -p ActiveEnterTimestamp`, probe `:31325`) — the owner deploys, migrates and commits mid-session. Prod mutations need explicit approval.

---

## Where to Look for What

| Task | Start here |
|---|---|
| Change how drops are processed | `data/submissions/drop.py` (restart `droptracker-webhook-consumer`) |
| Change how any other submission type works | `data/submissions/<type>.py` |
| Change intake endpoint behavior | `api/routes/webhook.py`, `workers/webhook_consumer.py` |
| Plugin in-game notifications / HUD | `services/plugin_notifications.py`, `api/routes/notifications.py` |
| Add/modify a Discord slash command | `commands/user.py`, `commands/admin.py`, `commands/group_admin.py` |
| Change notification embed format | `utils/embeds.py` + `db/models/embed.py` (GroupEmbed) |
| Change event message wording/layout | `services/event_message_layouts.py` (DB-seeded — reseed on default change) |
| Change leaderboard ranking logic | `services/redis_updates.py` |
| Change lootboard image layout | `lootboard/generator.py` or `lootboard/flexible_generator.py` |
| Add/change DB schema | Alembic migration in `alembic/versions/` (gitignored) |
| NPC name matching / aliases | `utils/npc_names.py` |
| Item valuation | `utils/ge_value.py`, `utils/value_overrides.py`, `web_api/routes/item_values.py` |
| Change group configuration options | `web_api/config_registry.py` (+ frontend `packages/api-types`) |
| Rename a group (name lives in 4 places) | `db/group_rename.py` — every rename path must go through it |
| Points/premium features | `services/points.py`, `data/submissions/point_awards.py`, `db/models/group_points.py` |
| Video upload flow | `api/routes/video.py`, `services/video_worker.py`, `utils/b2_storage.py` |
| XenForo integration | `db/xf/`, `services/xf_services.py` |
| Events v2 (tasks, bingo, teams) | `services/event_engine.py`, `workers/event_consumer.py`, `web_api/routes/events.py`, `db/models/events.py` |
| Loot Sweep events | `services/loot_sweep.py`, `docs/LOOT_SWEEP.md` |
| Board-game events | `services/boardgame_*.py`, `services/boardgen/` |
| Event Discord surfaces | `services/event_notifications.py`, `event_board.py`, `event_team_discord.py`, `event_signup_discord.py` |
| Discord Activity backend | `services/activity_launch*.py` (frontend lives in the web repo) |
| Badges | `services/badges.py`, `db/models/badge.py`, `web_api/routes/badges.py` |
| Website API endpoint (`/api/v1/...`) | `web_api/routes/<area>.py`; auth/roles in `web_api/deps.py` |
| Website auth/session | `web_api/session.py` (JWT), `web_api/routes/auth.py` (Discord OAuth) |
| Live drop feed / SSE | `services/realtime.py` (publish), `web_api/routes/realtime.py` (stream) |
| Subscriptions / billing | `web_api/billing.py` (Stripe), `payments.py`, `routes/subscriptions.py` |
| Tickets / suggestions | `services/ticket_system.py`, `services/suggestion_sync.py`, `web_api/routes/{tickets,suggestions}.py` |
| Group creation / RSN claims | `db/group_creation.py`, `db/player_claims.py` (shared by bot + web) |
| Owner's project/task board (agents: log progress here) | `scripts/project_tracker.py` (CLI — `list`/`show`/`add-task`/`task-status`/…), `web_api/routes/dev_tracker.py`, `db/models/dev_tracker.py`; UI at `/admin/projects` |
