![DropTracker](https://www.droptracker.io/img/droptracker-small.gif)

# DropTracker — Backend

DropTracker is an all-in-one loot- and achievement-tracking platform for Old School RuneScape clans and players. A custom [RuneLite plugin](https://www.github.com/joelhalen/droptracker-plugin) submits drops, personal bests, collection log slots, combat achievements, pets, quests and XP gains in real time. This repository is the **backend**: the intake API, submission processors, Discord bots, background workers, image generators, and the API that powers the [droptracker.io](https://www.droptracker.io) website.

**The DropTracker ecosystem spans three repositories:**

| Repo | What it is |
|---|---|
| **This repo** | Python backend — APIs, Discord bots, workers, DB models |
| [droptracker-plugin](https://www.github.com/joelhalen/droptracker-plugin) | RuneLite plugin (Java) that captures and submits in-game events |
| droptracker-web | Next.js website frontend (separate repo; talks to `web_api/` here) |

## How it works (10,000-ft view)

```
RuneLite plugin
      │  POST /webhook (multipart: payload_json + screenshot)
      ▼
┌─────────────────────┐     ┌──────────────────────────────┐
│ Intake API (:31323) │────▶│ data/submissions/* processors │
│ api/                │     │ dedupe → validate → WOM check │
└─────────────────────┘     └──────┬───────────────────────┘
                                   │ writes
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
          MySQL (data)       Redis leaderboards    notification_queue
              │                    │                     │
              │                    ▼                     ▼
              │             Realtime pub/sub      Core Discord bot
              │             (rt:* channels)       (bots/main.py)
              ▼
┌──────────────────────┐    ┌──────────────────────┐
│ Web API (:31325)     │◀───│ Next.js site (:31380)│◀── users
│ web_api/  (/api/v1)  │    │ separate repo        │
└──────────────────────┘    └──────────────────────┘
```

Key architectural rules (details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

- **Wise Old Man is the identity source of truth.** A `Player` row only exists once the WOM API confirms the account. Never trust a submitted RSN alone.
- **High-value drops are verified semantically.** Drops over 1M GP are checked against the OSRS Wiki (backed by OpenAI) to confirm the item can actually drop from the claimed NPC.
- **Notifications are asynchronous.** Processors write to `notification_queue`; the core bot drains it. Processors never talk to Discord directly.
- **Seasonal/League worlds are mirrored** into `seasonal_*` tables, selected by the payload's `world_type`.

## Runtime processes

Production runs as **systemd units**; canonical copies of the unit files live in [deploy/systemd/](deploy/systemd/).

| Unit | Entry point | Port | Purpose |
|---|---|---|---|
| `droptracker-api` | [api/app.py](api/app.py) (`api:create_app()`) | 31323 | Submission intake + public read API |
| `droptracker-webapi` | [web_api/app.py](web_api/app.py) | 31325 | Website backend (`/api/v1`: auth, profiles, config, events, admin, SSE) |
| `droptracker-node` | Next.js server (separate repo) | 31380 | Website frontend / BFF |
| `droptracker-core` | [bots/main.py](bots/main.py) | — | Primary Discord bot: slash commands, notifications, lootboard posting |
| `droptracker-webhooks` | [bots/webhook_bot.py](bots/webhook_bot.py) | — | Legacy fallback: parses submissions posted to Discord webhook channels |
| `droptracker-hof` | [bots/hall_of_fame.py](bots/hall_of_fame.py) | — | Hall of Fame image bot |
| `droptracker-lootboards` | [lootboard/_board_generator.py](lootboard/_board_generator.py) | — | Regenerates group lootboard images (~2 min loop) |
| `droptracker-player-updates` | [data/player_total_updater.py](data/player_total_updater.py) | — | Background WOM sync + Redis leaderboard maintenance |
| `droptracker-video-worker` | [services/video_worker.py](services/video_worker.py) | — | Converts uploaded MJPEG clips to MP4 via FFmpeg + Backblaze B2 |
| `droptracker-events` | [workers/event_consumer.py](workers/event_consumer.py) | — | Events v2 consumer: drains `events:submissions`, applies task/bingo/team progress |
| `droptracker-webhook-consumer` | [workers/webhook_consumer.py](workers/webhook_consumer.py) | — | Drains `webhook:queue` (fast-accept intake; idles unless `WEBHOOK_QUEUE_MODE=true`) |
| `droptracker-heartbeat` | [bots/heartbeat.py](bots/heartbeat.py) | — | Uptime heartbeat bot |
| `droptracker-api-dev` | same as `droptracker-api` | 31324 | Dev instance of the intake API (disabled by default) |

## Repository tour

| Directory | Purpose |
|---|---|
| [api/](api/) | Quart intake API (:31323) — `routes/webhook.py` is the front door for all plugin submissions |
| [web_api/](web_api/) | Quart website API (:31325) — 20 blueprints under `/api/v1` (auth via JWT, group config, events, badges, subscriptions/PayPal, docs CMS, superadmin, SSE realtime) |
| [bots/](bots/) | Discord bot entry points (core, webhook reader, HOF, heartbeat) |
| [commands/](commands/) | Slash-command handlers, split by permission tier (`user.py`, `admin.py`, `group_admin.py`) |
| [data/submissions/](data/submissions/) | One processor per submission type: `drop`, `pb`, `clog`, `ca`, `pet`, `quest`, `experience`, `adventure_log` (+ `point_awards.py`) |
| [db/](db/) | SQLAlchemy 2.0 models (`db/models/`, ~80 tables) + `ops.py` operations layer; two MySQL schemas: `data` and `xenforo` |
| [services/](services/) | Business logic: notification queue drain, Redis leaderboards, realtime pub/sub, points, badges, events engine/lifecycle/notifications, XenForo integration, video worker |
| [workers/](workers/) | Redis queue consumers (events, webhook fast-accept mode) |
| [lootboard/](lootboard/) | Pillow-based lootboard image generation + PNG themes |
| [games/events/](games/events/) | Events v2 domain logic (typed events, bingo boards, teams, task library) |
| [osrs_api/](osrs_api/) | External API clients: Wise Old Man, GE pricing, semantic drop verification |
| [utils/](utils/) | Shared helpers: Redis client, embeds, formatting, Fernet encryption, B2 storage |
| [monitor/](monitor/) | Service control/monitoring CLI + systemd watchdog integration |
| [alembic/](alembic/) | DB migration environment (see setup notes below) |
| [scripts/](scripts/) | One-off maintenance/backfill scripts (run manually) |
| [tests/](tests/) | Pytest suite (`tests/unit/` runs in CI; `tests/integration/` needs DB/Redis) |
| [docs/](docs/) | Architecture and pipeline documentation (start here ↓) |

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, full DB schema, Redis keys, external services
- [docs/SUBMISSION_PIPELINE.md](docs/SUBMISSION_PIPELINE.md) — end-to-end walkthrough of a submission, per type
- [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) — intake-latency async refactor (Phase 1 implemented behind `WEBHOOK_QUEUE_MODE`)
- [CLAUDE.md](CLAUDE.md) — dense codebase orientation (kept current for AI coding agents, equally useful for humans)
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, tests, migrations, conventions

## Getting started (development)

Requirements: **Python 3.12+**, MySQL/MariaDB, Redis.

```bash
# 1. Clone + install
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. Configure
cp .env.example .env
# Minimum to boot: DB_USER / DB_PASS, a Discord bot token (DEV_TOKEN with
# STATE=dev), WOM_API_KEY, ENCRYPTION_KEY. See .env.example for the full list.

# 3. Database
# Note: alembic/versions/ is not committed to this repo, so a fresh clone
# cannot `alembic upgrade head` from zero. Ask a maintainer for a schema dump,
# then use Alembic for your own incremental changes:
cp alembic.ini.template alembic.ini   # then set sqlalchemy.url

# 4. Run the pieces you're working on (each is independent)
python -m api.app                    # intake API :31323
python -m web_api.app                # website API :31325
python -m bots.main                  # core Discord bot
python -m lootboard._board_generator # lootboard image loop
```

`STATE=dev` in `.env` makes the bot use `DEV_TOKEN` instead of `BOT_TOKEN`.

### Tests

```bash
pytest tests/unit -q          # fast, no external services (this is what CI runs)
pytest tests/integration -q   # requires MySQL + Redis
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs unit tests on pushes/PRs to `new-api` — which is the active development branch.

## Legacy code (what to ignore)

The codebase has grown over several years; not everything at the repo root is live. **Do not build on these:**

- `real_startup.sh`, `run_server.sh`, `restart.sh` — pre-systemd launchers (reference files that no longer exist)
- `eventBot.py`, `worker.py`, `bingo_test.py`, `commands.py`, `test.py` — superseded (legacy events system was decommissioned 2026-07-05; archived in [docs/archive/legacy-events/](docs/archive/legacy-events/))
- `board_cli.py` — broken import; use `timeframe_board_cli.py` (still invoked by `web_api`) if you need CLI board generation
- `web/` + `templates/` + most of `static/` — the old Jinja2 site, replaced by the Next.js frontend; kept for reference
- `games/gielinor_race/` — archived mini-game, design reference only
- `oldvenv/` — dead virtualenv

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version: branch from `new-api`, keep changes async-friendly, add unit tests where practical, and run `pytest tests/unit` before opening a PR. Questions are welcome in the [DropTracker Discord](https://www.droptracker.io/).

## License

[MIT](LICENSE) © DropTracker.io
