# Database Backups

Nightly MariaDB (+ Redis) backups with local retention and Backblaze B2 offsite
copies. Built 2026-07-12; before this, production had **zero** backups.

## What runs, when

`droptracker-db-backup.timer` fires daily at **08:30 UTC** (± up to 30 min
random delay, `Persistent=true` so a missed run replays after boot). It starts
`droptracker-db-backup.service` (oneshot, root), which runs
`scripts/db_backup.sh`:

1. Prunes local sets older than **7 days** (`LOCAL_RETENTION_DAYS`).
2. Aborts loudly if less than **25 GiB** free on `/store` (`MIN_FREE_GB`).
3. Dumps with `mariadb-dump --single-transaction --quick --skip-lock-tables
   --routines --triggers --events --hex-blob` — a consistent InnoDB snapshot,
   **no table locks**, safe against live prod:
   - `data-YYYY-MM-DD.sql.gz` — full `data` schema (~165M-row `drops` table)
   - `data-schema-YYYY-MM-DD.sql.gz` — schema-only `data` dump. This matters:
     `alembic/versions/` is gitignored, so these dumps are the **only** schema
     recovery path.
   - `xenforo-YYYY-MM-DD.sql.gz` — full forum schema
4. Best-effort Redis snapshot: `BGSAVE` (Redis AUTH reuses `DB_PASS`), then
   gzips `/var/lib/redis/dump.rdb` → `redis-YYYY-MM-DD.rdb.gz`. Failures here
   only WARN — leaderboards are rebuildable
   (`scripts/reconcile_*_leaderboards.py`).
5. Uploads the whole set to B2 `dt_backups/mysql/YYYY-MM-DD/` via
   `scripts/b2_backup_sync.py` (venv boto3, same S3-compatible setup as
   `utils/b2_storage.py`), verifying each object's size after upload.
6. Prunes B2 objects older than **30 days** (`REMOTE_RETENTION_DAYS`).

Local layout: `/store/droptracker/backups/YYYY-MM-DD/` (UTC dates).

Everything logs to stdout with UTC timestamps; journald captures it. Any dump
or upload failure exits non-zero, so the unit shows up in `systemctl --failed`.

**The B2 gotcha:** the application key is namePrefix-restricted to `dt_` —
any object key not starting with `dt_` fails with 401. The helper enforces
`dt_backups/` on upload, and its `prune` is hardcoded to `dt_backups/` so it
can never touch the video objects (`dt_videos/...`) sharing the bucket.

## Install (one time)

```bash
cd /store/droptracker/disc
sudo cp deploy/systemd/droptracker-db-backup.service /etc/systemd/system/
sudo cp deploy/systemd/droptracker-db-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now droptracker-db-backup.timer

# verify
systemctl list-timers droptracker-db-backup.timer
```

No new env keys: the script reads `DB_USER`/`DB_PASS` (and optional
`DB_HOST`/`DB_PORT`) plus the existing `B2_KEY_ID`, `B2_APPLICATION_KEY`,
`B2_BUCKET_NAME`, `B2_ENDPOINT_URL` from `.env`.

## Manual backup

```bash
sudo systemctl start droptracker-db-backup.service   # logs -> journald
journalctl -u droptracker-db-backup.service -f

# or directly (root needed only for the Redis copy):
sudo /store/droptracker/disc/scripts/db_backup.sh
sudo SKIP_B2=1 /store/droptracker/disc/scripts/db_backup.sh   # local-only run
```

B2 connectivity check (read-only):

```bash
/store/droptracker/disc/venv/bin/python /store/droptracker/disc/scripts/b2_backup_sync.py check
```

## Restore test (do this quarterly)

```bash
sudo /store/droptracker/disc/scripts/backup_restore_test.sh
```

Restores the newest local `data` dump into a scratch schema
`restore_test_data`, checks table count, `drops` > 100M rows, `users`/`groups`
non-empty, prints **PASS/FAIL**, then drops the scratch schema.

- Takes **hours** (replays ~165M rows) and needs ~35–40 GiB free while running.
  Run off-peak; don't overlap the 08:30 UTC backup window.
- The scratch name is hardcoded and the script grep-guards the dump for
  `CREATE DATABASE`/`USE` statements before piping — it cannot touch the real
  `data`/`xenforo` schemas.
- `KEEP_SCRATCH=1` keeps `restore_test_data` afterwards (useful for
  single-table recovery, below). Drop it manually when done:
  `mysql -e "DROP DATABASE restore_test_data"`.

## Disaster recovery

### Full loss (rebuilt server / dead DB)

1. Get the dumps. Local first (`/store/droptracker/backups/<date>/`); if the
   disk is gone, from B2:

   ```bash
   cd /store/droptracker/disc
   venv/bin/python scripts/b2_backup_sync.py check   # see what exists
   venv/bin/python scripts/b2_backup_sync.py download \
       dt_backups/mysql/2026-07-12/data-2026-07-12.sql.gz /store/restore/data.sql.gz
   venv/bin/python scripts/b2_backup_sync.py download \
       dt_backups/mysql/2026-07-12/xenforo-2026-07-12.sql.gz /store/restore/xenforo.sql.gz
   ```

   (If the box is a total loss, grab the B2 creds from the `.env` backup /
   password manager and use any S3 client against `B2_ENDPOINT_URL`,
   bucket `droptracker-video`, prefix `dt_backups/mysql/`.)

2. Recreate and load each schema (dumps contain no `CREATE DATABASE`/`USE`,
   so you name the target explicitly — this is deliberate):

   ```bash
   mysql -u root -p -e "CREATE DATABASE data CHARACTER SET utf8mb4"
   zcat /store/restore/data.sql.gz | mysql -u root -p data

   mysql -u root -p -e "CREATE DATABASE xenforo CHARACTER SET utf8mb4"
   zcat /store/restore/xenforo.sql.gz | mysql -u root -p xenforo
   ```

   Use the **MariaDB 10.11+ client** — dumps start with the
   `/*M!999999\- enable the sandbox mode */` line, which very old clients
   reject. Expect hours for `data`.

3. Redis (optional — it's all rebuildable): stop redis, gunzip
   `redis-<date>.rdb.gz` to `/var/lib/redis/dump.rdb`, `chown redis:redis`,
   start redis. Or skip and rebuild boards with
   `scripts/reconcile_all_time_leaderboards.py` /
   `scripts/reconcile_period_leaderboards.py` + lootboard force_update.

4. Restart the stack: `sudo systemctl restart 'droptracker-*'`.

### Single table (bad migration, accidental delete)

Restore into the scratch schema, then copy rows across — never pipe a dump at
the live schema:

```bash
sudo KEEP_SCRATCH=1 /store/droptracker/disc/scripts/backup_restore_test.sh
# then, e.g. recover group_configurations:
mysql -u root -p -e "
  INSERT INTO data.group_configurations
  SELECT * FROM restore_test_data.group_configurations r
  WHERE NOT EXISTS (SELECT 1 FROM data.group_configurations d WHERE d.id = r.id);"
mysql -u root -p -e "DROP DATABASE restore_test_data"
```

For a huge table you can instead extract just its section from the dump:

```bash
zcat data-2026-07-12.sql.gz | \
  sed -n '/^-- Table structure for table `drops`/,/^-- Table structure for table /p' | \
  head -n -1 > drops-only.sql
```

### Schema only (broken migration, fresh dev clone)

`data-schema-<date>.sql.gz` is a `--no-data` dump — the canonical schema
reference since `alembic/versions/` is not in git:

```bash
mysql -u root -p -e "CREATE DATABASE data CHARACTER SET utf8mb4"
zcat data-schema-2026-07-12.sql.gz | mysql -u root -p data
```

## Sizing & caveats

- Schema sizes (2026-07-12): `data` 17.2 GiB data + 13.3 GiB indexes (118
  tables, all InnoDB); `xenforo` 7.1 GiB (2 MEMORY tables — their contents
  are transient and dump near-empty; that's fine).
- Estimated gzipped nightly set: **~5–8 GiB** (data ~3.5–5.5, xenforo ~1–2.5,
  schema-only tiny, redis ~0.3). `/store` had **81 GiB free at 80% used** when
  this was built, so 7-day local retention ≈ 35–56 GiB is *tight*. **After the
  first run, check the actual set size** (`du -sh /store/droptracker/backups/*`);
  if a set exceeds ~8 GiB, drop `LOCAL_RETENTION_DAYS` to 4–5 via
  `Environment=` in the service unit. The `MIN_FREE_GB=25` gate makes the job
  fail loudly rather than fill the prod disk.
- `--single-transaction` gives point-in-time consistency for InnoDB only (all
  tables are InnoDB). **Don't run DDL / alembic migrations during the backup
  window (~08:30–10:00 UTC)** — `ALTER TABLE` mid-dump breaks the snapshot and
  fails the dump.
- Binary logging is **OFF** on this server, so restores are nightly-snapshot
  granularity — up to 24h of data loss in the worst case. Enabling binlogs for
  point-in-time recovery is a possible future upgrade.
- A `flock` on `/store/droptracker/backups/.backup.lock` prevents overlapping
  runs; the dump job runs at `Nice=10` / IO best-effort-7 to stay out of
  prod's way.

## Monitoring

```bash
systemctl list-timers droptracker-db-backup.timer   # next/last run
journalctl -u droptracker-db-backup.service -n 100  # last run's log
systemctl --failed                                  # a failed backup shows here
ls -lh /store/droptracker/backups/*/                # local sets
venv/bin/python scripts/b2_backup_sync.py check     # offsite sets
```

There is no alerting hook yet — checking `systemctl --failed` (or adding an
`OnFailure=` unit that pings Discord) is the follow-up.
