# DropTracker development box

Host `ns1004556` / `40.160.62.117`. Built 2026-08-06.

This is the development counterpart to the production box. It runs the same
branches (`disc@new-api`, `web@main`) against its own MariaDB, Redis, and a
light-scrubbed copy of production data.

---

## What runs here

| Unit | Port | State |
|---|---|---|
| `droptracker-api` | 127.0.0.1:31323 | enabled, running |
| `droptracker-webapi` | 127.0.0.1:31325 | enabled, running |
| `droptracker-node-blue` | 0.0.0.0:31380 | enabled, running |
| `droptracker-webhook-consumer` | — | enabled, running |
| `droptracker-events` | — | enabled, running |
| `droptracker-lootboards` | — | enabled, running |
| `nginx` | 0.0.0.0:80 | enabled, running |
| `mariadb` | 127.0.0.1:3306 | 10.11.18 (same as prod) |
| `redis-server` | 127.0.0.1:6379 | 7.0.15 (same as prod) |

**Installed but deliberately NOT running:**

| Unit | Why |
|---|---|
| `droptracker-core`, `droptracker-webhooks` | Discord bot tokens are blank. Each needs its own dev Discord application before starting — booting on a production token connects AS the production bot in real guilds. |
| `droptracker-hof`, `droptracker-heartbeat` | **MASKED.** `bots/hall_of_fame.py:24` and `bots/heartbeat.py:27` have no `STATE`/`STATUS` dev gate at all — they connect with whatever token is present. Masked so a stray `systemctl start` cannot reach Discord. |
| `droptracker-player-updates` | Drives Wise Old Man traffic on the shared prod API key. |
| `droptracker-video-worker` | Needs Backblaze B2 credentials, which are deliberately absent. |
| `droptracker-node-green`, `droptracker-api-dev` | Blue-green is a production uptime mechanism; one colour is enough here. |

**All timers are masked:** `recaps` (can DM thousands of users), `wom-sync`
(pushes to a GitHub repo and restarts services), `prune-images` (deletes test
screenshots and clears `image_url`), `health-watch`, `npc-ehb-rates`,
`db-backup`.

---

## Deliberate divergences from production

1. **Everything runs as `debian`.** Prod splits services across `user` and
   `debian`, which is why prod's asset tree is 0777. One account here removes
   that whole class of problem. Drop-ins live at
   `/etc/systemd/system/droptracker-*.service.d/10-dev.conf`.
2. **`/store/droptracker/disc` path is replicated exactly.** Not negotiable —
   `db/models/base.py:29` hardcodes `@localhost:3306/data`, ~40 code paths
   hardcode the asset root, and `drops.image_url` column values contain that
   literal. Replicating the path is what makes the unmodified branch run here.
3. **`/img/` is served from disk by nginx**, not proxied to `droptracker-core`'s
   :8080 Jinja app as prod does. Restarting the bot cannot break site images.
4. **Single Next.js colour** (`.next-blue`, port 31380). `scripts/deploy.sh` is
   NOT used here — it needs `sudo nginx` and a healthy idle colour. To rebuild:
   ```
   cd /store/droptracker/web && NEXT_DIST_DIR=.next-blue pnpm build \
     && sudo systemctl restart droptracker-node-blue
   ```
   The backend must be running first — the build prerenders `/announcements`,
   which fetches from 127.0.0.1:31325.
5. **HTTP Basic auth** gates the site (`user: dtdev`, password in
   `/store/droptracker-devsetup/basic-auth-password.txt`). This is a stopgap:
   the intended gate is Cloudflare Access, but the `CLOUDFLARE_API_TOKEN` in
   prod's `.env` is **expired** (returns HTTP 401), so DNS and Access could not
   be configured. Remove the two `auth_basic` lines in
   `/etc/nginx/sites-available/droptracker-dev` once Access is live.

---

## Data

Light scrub, per owner decision. Real player names, Discord IDs and group names
are **preserved**; only credentials were removed.

| | |
|---|---|
| players | 18,635 |
| users | 2,876 |
| groups | 263 |
| items | 29,457 |
| drops | 5,704,120 (7-day slice, 2026-07-30 → 2026-08-06) |
| collection | 272,522 |
| personal_best | 69,792 |

**Never copied from production:**
- `webhooks`, `backup_webhooks`, `webhook_pending_deletion`, `new_webhooks` —
  1,619 live clan webhook URLs. Excluded from the dump entirely rather than
  nulled after transfer, so they never left the prod box.
- `notification_queue` — rendered Discord payloads embedding channel targets.
- `player_item_hourly_totals`, `player_npc_hourly_totals` — 10.7 GB of pure
  derived aggregates. Rebuild with `scripts/backfill_item_hourly_totals.py`
  and `scripts/backfill_npc_hourly_totals.py` if a feature needs them.
- The entire `xenforo` schema — empty structure only.
- `static/assets/img/user-upload` (165 GB of user screenshots) and `clans`.
  Only `itemdb` + `npcdb` icons (280 MB, no PII) were carried over.

**Blanked after load:** `users.auth_token` (600 live values),
`group_configurations.export_api_key` (261 live keys), `groups.invite_url`
(one row contained a pasted Discord webhook URL — redacted in transit).

### Rebuilding the dataset

Artifacts were produced on **prod** by `/tmp/export_dev_dataset.sh` (dumps +
leak checks) and loaded here by `/tmp/load_dev_dataset.sh`. Redis is warmed
from the DB afterwards — `leaderboard:*` and `player:*` keys are derived and
start empty:

```
cd /store/droptracker/disc
venv/bin/python scripts/reconcile_period_leaderboards.py --commit
venv/bin/python scripts/reconcile_all_time_leaderboards.py --commit
venv/bin/python scripts/warm_dev_redis.py     # monthly keys - what the UI reads
```

Without the last one the leaderboards render "No ranked players yet".

**Then rebuild the frontend.** 61 pages are statically prerendered at build
time, so a site built before Redis was warmed keeps serving the empty version
no matter what is in Redis. After any data reload or Redis warm:

```
cd /store/droptracker/web && NEXT_DIST_DIR=.next-blue pnpm build \
  && sudo systemctl restart droptracker-node-blue
```

Note the ordering constraint: the build itself fetches from 127.0.0.1:31325
while prerendering `/announcements`, so `droptracker-webapi` must be running
*before* you build.

**A player with no drops in the loaded window shows 0, and that is correct.**
The slice starts 2026-07-30 — accounts that were quiet since then have no
`player:{id}:{partition}:total_loot` key and legitimately read as zero.
Widen `DAYS` in `deploy/dev/export_dev_dataset.sh` if you need more history.

---

## Bugs found in the repo while building this

These affect **any** fresh environment, not just this box:

1. **`sdnotify` was missing from `requirements.txt`.** Installed in prod's venv
   (0.3.2) but listed nowhere. Six `Type=notify` units — `core`, `webhooks`,
   `hof`, `heartbeat`, `lootboards`, `player-updates` — crash on
   `ModuleNotFoundError: No module named 'sdnotify'` in a clean install.
   **Fixed in commit `1b321ec`** (on prod, not yet pushed).
2. **`alembic.ini.template` line 37 shipped `version_num_format = %04d`.**
   configparser rejects the unescaped `%`; every alembic command died with
   `InterpolationSyntaxError`. **Fixed in commit `1b321ec`** (needs `%%04d`).
3. **`alembic upgrade head` cannot build a schema from zero**, even with all 127
   revision files present. The base revision `262385e9df48` declares
   `down_revision = None` but its docstring says it revises `8c591955ca8a`,
   which no longer exists. The chain only carries incremental changes on top of
   a schema originally created by `Base.metadata.create_all()`. This box's
   schema came from prod's nightly `data-schema-*.sql.gz` + `alembic stamp`.
4. **`CLOUDFLARE_API_TOKEN` in prod's `.env` is expired** (HTTP 401). Nothing
   noticed because `CloudflareIPUpdater` is imported at `bots/main.py:49` and
   never instantiated — it is dead code.

---

## Other workloads on this box

This machine is shared. A Palworld server, a game project in `/home/debian/game`,
`/home/debian/zombies-rip`, and `/store/compliance` were **stopped, not deleted**,
to free RAM and CPU. PostgreSQL was left running.

Restore them with:
```
sudo bash /store/droptracker-devsetup/restore-workloads.sh
```
Inventory of exactly what was running: `/store/droptracker-devsetup/quiesced-workloads.txt`.

**No host firewall was applied.** The plan called for a default-drop nftables
ruleset, but that would have killed Palworld and the node servers. The existing
iptables rules (Palworld ports only) are untouched.

---

## Outstanding

- [ ] **Discord bot tokens.** Create dev applications and set `DEV_TOKEN` +
      `DEV_WEBHOOK_TOKEN` in `/store/droptracker/disc/.env`, then
      `systemctl enable --now droptracker-core droptracker-webhooks`.
      `STATE` and `STATUS` are already `"dev"`, so `bots/main.py:178` and
      `bots/webhook_bot.py:47` will pick up the dev tokens.
- [ ] **DNS.** Add an A record `dev.droptracker.io → 40.160.62.117` (proxied).
      nginx already answers for that hostname.
- [ ] **Discord OAuth.** `DISCORD_BOT_CLIENT_ID`/`SECRET` are carried over from
      prod. Web sign-in needs `https://dev.droptracker.io/api/auth/callback`
      added to that application's redirect URIs — or a separate dev app.
- [ ] **Pull the repo fixes.** Commit `1b321ec` on the prod box fixes the
      `sdnotify` and `alembic.ini.template` bugs above but has not been pushed.
      Once it is, `git -C /store/droptracker/disc pull` here — this tree is
      deliberately left clean so that pull is conflict-free. (`sdnotify` is
      already pip-installed here and this box's `alembic.ini` is already
      patched, so nothing is broken in the meantime.)

Done: the stale 2026-03 checkout at `/store/droptracker-old-disc-20260806`
(22 GB) was deleted on 2026-08-06 after the new tree was verified healthy.
