#!/usr/bin/env bash
#
# Nightly MariaDB (+ Redis) backup for DropTracker production.
#
# Produces, under /store/droptracker/backups/YYYY-MM-DD/ (UTC date):
#   data-YYYY-MM-DD.sql.gz         full dump of the `data` schema
#   data-schema-YYYY-MM-DD.sql.gz  schema-only dump of `data` (alembic/versions
#                                  is gitignored, so this is the schema recovery path)
#   xenforo-YYYY-MM-DD.sql.gz      full dump of the `xenforo` schema
#   redis-YYYY-MM-DD.rdb.gz        Redis RDB snapshot (nice-to-have; skipped with a
#                                  warning if unreadable — leaderboards are rebuildable)
#
# Then uploads the set to B2 under dt_backups/mysql/YYYY-MM-DD/ (the B2 app key
# is namePrefix-restricted to "dt_") and prunes local (>LOCAL_RETENTION_DAYS)
# and remote (>REMOTE_RETENTION_DAYS) copies.
#
# Dumps use --single-transaction --quick: consistent InnoDB snapshot, no table
# locks, safe against live prod. Do NOT run DDL / alembic migrations while a
# dump is in flight (ALTER TABLE breaks the consistent snapshot mid-dump).
#
# Runs as root via droptracker-db-backup.timer (deploy/systemd/). Logs to
# stdout; journald captures it. Non-zero exit on any failure (Redis excepted).
#
# Env overrides (optional):
#   BACKUP_ROOT=/store/droptracker/backups
#   LOCAL_RETENTION_DAYS=7      REMOTE_RETENTION_DAYS=30
#   MIN_FREE_GB=25              minimum free space on the backup fs before starting
#   SKIP_B2=1                   local-only run (no upload / remote prune)
#   SKIP_REDIS=1                skip the Redis snapshot copy
#
set -euo pipefail

REPO_DIR="/store/droptracker/disc"
ENV_FILE="$REPO_DIR/.env"
PYTHON="$REPO_DIR/venv/bin/python"
B2_SYNC="$REPO_DIR/scripts/b2_backup_sync.py"

BACKUP_ROOT="${BACKUP_ROOT:-/store/droptracker/backups}"
LOCAL_RETENTION_DAYS="${LOCAL_RETENTION_DAYS:-7}"
REMOTE_RETENTION_DAYS="${REMOTE_RETENTION_DAYS:-30}"
MIN_FREE_GB="${MIN_FREE_GB:-25}"
SKIP_B2="${SKIP_B2:-0}"
SKIP_REDIS="${SKIP_REDIS:-0}"

# Fail-loudly floor sizes for the gzipped dumps: a "successful" dump smaller
# than this means something went badly wrong (empty schema, auth issue, etc).
DATA_MIN_BYTES=$((200 * 1024 * 1024))   # data full dump: >= 200 MB gz
XF_MIN_BYTES=$((20 * 1024 * 1024))      # xenforo full dump: >= 20 MB gz
SCHEMA_MIN_BYTES=$((10 * 1024))         # data schema-only: >= 10 KB gz

DATE_UTC="$(date -u +%F)"
OUT_DIR="$BACKUP_ROOT/$DATE_UTC"
START_TS="$(date -u +%s)"

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "FATAL: $*"; exit 1; }

# ---------------------------------------------------------------- .env access
env_get() {
    # Read KEY=VALUE from .env (last occurrence wins), strip CR and one layer
    # of surrounding quotes. Never sources the file (values may contain shell
    # metacharacters).
    local raw
    raw="$(sed -n "s/^${1}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r')"
    raw="${raw%\"}"; raw="${raw#\"}"
    raw="${raw%\'}"; raw="${raw#\'}"
    printf '%s' "$raw"
}

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE"
DB_USER="$(env_get DB_USER)"
DB_PASS="$(env_get DB_PASS)"
DB_HOST="$(env_get DB_HOST)"; DB_HOST="${DB_HOST:-localhost}"
DB_PORT="$(env_get DB_PORT)"; DB_PORT="${DB_PORT:-3306}"
[[ -n "$DB_USER" && -n "$DB_PASS" ]] || die "DB_USER / DB_PASS not set in $ENV_FILE"

command -v mariadb-dump >/dev/null || die "mariadb-dump not found"
command -v gzip >/dev/null || die "gzip not found"
[[ -x "$PYTHON" ]] || die "venv python not found at $PYTHON"
[[ -f "$B2_SYNC" ]] || die "missing $B2_SYNC"

mkdir -p "$BACKUP_ROOT"

# Single-instance lock (a slow dump must never overlap the next run).
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || die "another backup run holds $BACKUP_ROOT/.backup.lock"

# Client credentials file so the password never appears in `ps` output.
MYCNF="$(mktemp /dev/shm/dt-backup-my.XXXXXX 2>/dev/null || mktemp)"
cleanup() { rm -f "$MYCNF"; }
trap cleanup EXIT
chmod 600 "$MYCNF"
DB_PASS_CNF="${DB_PASS//\\/\\\\}"; DB_PASS_CNF="${DB_PASS_CNF//\"/\\\"}"
cat > "$MYCNF" <<EOF
[client]
user=${DB_USER}
password="${DB_PASS_CNF}"
host=${DB_HOST}
port=${DB_PORT}
EOF

MYSQL=(mysql --defaults-extra-file="$MYCNF")
DUMP=(mariadb-dump --defaults-extra-file="$MYCNF"
      --single-transaction --quick --skip-lock-tables
      --routines --triggers --events --hex-blob
      --default-character-set=utf8mb4 --max-allowed-packet=256M)

# ------------------------------------------------------------ local retention
# Prune BEFORE dumping so the freed space is available for tonight's set.
# Directories are named YYYY-MM-DD; keep the most recent LOCAL_RETENTION_DAYS
# calendar days (today inclusive), delete anything older by name.
prune_local() {
    local keep_from d name
    keep_from="$(date -u -d "-$((LOCAL_RETENTION_DAYS - 1)) days" +%F)"
    log "local prune: keeping $BACKUP_ROOT/{$keep_from..$DATE_UTC} (retention ${LOCAL_RETENTION_DAYS}d)"
    for d in "$BACKUP_ROOT"/20??-??-??; do
        [[ -d "$d" ]] || continue
        name="$(basename "$d")"
        if [[ "$name" < "$keep_from" ]]; then
            log "local prune: deleting $d"
            rm -rf "$d"
        fi
    done
}

free_space_check() {
    local avail_kb need_kb
    avail_kb="$(df -Pk "$BACKUP_ROOT" | awk 'NR==2 {print $4}')"
    need_kb=$((MIN_FREE_GB * 1024 * 1024))
    log "free space on backup fs: $((avail_kb / 1024 / 1024)) GiB (require >= ${MIN_FREE_GB} GiB)"
    (( avail_kb >= need_kb )) || die "insufficient free space; lower LOCAL_RETENTION_DAYS or clear $BACKUP_ROOT"
}

# ----------------------------------------------------------------- dump logic
assert_min_size() {
    local file=$1 min=$2 size
    size="$(stat -c %s "$file")"
    (( size >= min )) || die "$file is only ${size} bytes (< ${min}); dump is suspect, aborting"
}

dump_schema() {
    # dump_schema <schema> <outfile> <min_bytes> [extra mariadb-dump args...]
    local schema=$1 outfile=$2 min_bytes=$3; shift 3
    local t0 t1
    t0="$(date -u +%s)"
    log "dumping ${schema} -> ${outfile}"
    "${DUMP[@]}" "$@" "$schema" | gzip > "$outfile"
    gzip -t "$outfile"
    assert_min_size "$outfile" "$min_bytes"
    t1="$(date -u +%s)"
    log "dumped ${schema}: $(du -h "$outfile" | cut -f1) in $((t1 - t0))s"
}

# --------------------------------------------------------- redis nice-to-have
backup_redis() {
    # Best-effort copy of the Redis RDB snapshot. Leaderboards are rebuildable,
    # so any failure here logs a warning and the backup continues.
    local rdir rfile rdb out lastsave t deadline
    command -v redis-cli >/dev/null || { log "WARN: redis-cli not found; skipping Redis snapshot"; return 0; }

    # Prod Redis reuses DB_PASS as its AUTH password (see utils/redis.py).
    local RCLI=(redis-cli -h 127.0.0.1 -p 6379 --no-auth-warning -a "$DB_PASS")

    rdir="$("${RCLI[@]}" CONFIG GET dir 2>/dev/null | sed -n 2p)" || true
    rfile="$("${RCLI[@]}" CONFIG GET dbfilename 2>/dev/null | sed -n 2p)" || true
    rdir="${rdir:-/var/lib/redis}"
    rfile="${rfile:-dump.rdb}"
    rdb="$rdir/$rfile"

    # Ask for a fresh snapshot; tolerate failure (e.g. save in progress).
    lastsave="$("${RCLI[@]}" LASTSAVE 2>/dev/null)" || lastsave=""
    if "${RCLI[@]}" BGSAVE >/dev/null 2>&1; then
        deadline=$(( $(date -u +%s) + 180 ))
        while (( $(date -u +%s) < deadline )); do
            t="$("${RCLI[@]}" LASTSAVE 2>/dev/null)" || t=""
            [[ -n "$t" && "$t" != "$lastsave" ]] && break
            sleep 3
        done
        [[ -n "$t" && "$t" != "$lastsave" ]] \
            && log "redis BGSAVE completed" \
            || log "WARN: redis BGSAVE did not finish within 180s; copying existing snapshot"
    else
        log "WARN: redis BGSAVE failed; copying existing snapshot if present"
    fi

    if [[ ! -r "$rdb" ]]; then
        log "WARN: Redis RDB not readable at $rdb (need root); skipping Redis snapshot"
        return 0
    fi
    out="$OUT_DIR/redis-$DATE_UTC.rdb.gz"
    gzip -c "$rdb" > "$out"
    gzip -t "$out"
    log "redis snapshot copied: $(du -h "$out" | cut -f1)"
}

# ------------------------------------------------------------------ execution
log "=== DropTracker DB backup starting (UTC date $DATE_UTC) ==="
log "backup root: $BACKUP_ROOT | local retention: ${LOCAL_RETENTION_DAYS}d | remote retention: ${REMOTE_RETENTION_DAYS}d"

# Connectivity preflight (cheap, fails fast on bad creds / server down).
"${MYSQL[@]}" -N -e "SELECT 1" >/dev/null || die "cannot connect to MariaDB at ${DB_HOST}:${DB_PORT}"

prune_local
free_space_check
mkdir -p "$OUT_DIR"

dump_schema data    "$OUT_DIR/data-$DATE_UTC.sql.gz"        "$DATA_MIN_BYTES"
dump_schema data    "$OUT_DIR/data-schema-$DATE_UTC.sql.gz" "$SCHEMA_MIN_BYTES" --no-data
dump_schema xenforo "$OUT_DIR/xenforo-$DATE_UTC.sql.gz"     "$XF_MIN_BYTES"

if [[ "$SKIP_REDIS" == "1" ]]; then
    log "SKIP_REDIS=1: skipping Redis snapshot"
else
    backup_redis || log "WARN: Redis snapshot step failed; continuing (rebuildable data)"
fi

log "local backup set:"
ls -l "$OUT_DIR" | while IFS= read -r line; do log "  $line"; done

if [[ "$SKIP_B2" == "1" ]]; then
    log "SKIP_B2=1: skipping offsite upload and remote prune"
else
    log "uploading to B2 under dt_backups/mysql/$DATE_UTC/"
    "$PYTHON" "$B2_SYNC" upload-dir "$OUT_DIR" "dt_backups/mysql/$DATE_UTC"
    log "pruning B2 objects older than ${REMOTE_RETENTION_DAYS} days"
    "$PYTHON" "$B2_SYNC" prune --days "$REMOTE_RETENTION_DAYS"
fi

log "=== backup complete in $(( $(date -u +%s) - START_TS ))s ==="
