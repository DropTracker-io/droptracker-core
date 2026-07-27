# systemd units

Canonical copies of the production systemd units. The live files are installed
at `/etc/systemd/system/` — if you change one here, reinstall it:

```bash
sudo cp deploy/systemd/<unit>.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart <unit>
```

| Unit | Entry point | Port |
|---|---|---|
| `droptracker-api` | `api:create_app()` via Hypercorn (6 workers) | 31323 |
| `droptracker-api-dev` | same, dev instance (disabled by default) | 31324 |
| `droptracker-webapi` | `web_api/app.py` | 31325 |
| `droptracker-node-blue` | Next.js server, blue (separate `droptracker-web` repo) | 31380 |
| `droptracker-node-green` | Next.js server, green (blue-green standby) | 31381 |
| `droptracker-node` | **Deploy trigger** (oneshot → web `scripts/deploy.sh`); `systemctl restart` = zero-downtime deploy. Not a server. | — |
| `droptracker-core` | `bots/main.py` | — |
| `droptracker-webhooks` | `bots/webhook_bot.py` | — |
| `droptracker-hof` | `bots/hall_of_fame.py` | — |
| `droptracker-heartbeat` | `bots/heartbeat.py` | — |
| `droptracker-lootboards` | `lootboard/_board_generator.py` | — |
| `droptracker-player-updates` | `data/player_total_updater.py` | — |
| `droptracker-video-worker` | `services/video_worker.py` | — |
| `droptracker-events` | `workers/event_consumer.py` | — |
| `droptracker-webhook-consumer` | `workers/webhook_consumer.py` | — |
| `droptracker-prune-images` | `scripts/prune_drop_images.py` (timer-driven oneshot) | — |

Notes:

- All services run as `User=user` with `WorkingDirectory=/store/droptracker/disc`
  and read config from `.env` (python-dotenv).
- The bots use `Type=notify` with a 30s systemd watchdog (see
  `monitor/sdnotifier.py`); the queue consumers and workers are `Type=simple`.
- `droptracker-prune-images` is timer-driven (`.timer`, ~04:00 UTC), not a
  long-running service — enable the **timer**, never the service. It keeps a
  drop screenshot only while the drop is worth >= 1,000,000 gp or is under 30
  days old. It is scheduled ahead of `droptracker-db-backup` (08:30 UTC) on
  purpose: the backup aborts below 25 GiB free, so the prune clears space for
  it. Dry-run it any time with
  `venv/bin/python -m scripts.prune_drop_images` (no `--apply` = no changes).
- `droptracker-webhook-consumer` pairs with `WEBHOOK_QUEUE_MODE=true` in `.env`
  (fast-accept intake). It idles harmlessly when the flag is off. Because both
  it and the API run with `PrivateTmp=true`, `WEBHOOK_TEMP_DIR` must point at a
  shared path outside `/tmp` (see `.env.example`).
