#!/usr/bin/env bash
#
# Nightly MariaDB (+ Redis) backup for DropTracker production.
#
# Produces, under /store/droptracker/backups/YYYY-MM-DD/ (UTC date):
#   data-YYYY-MM-DD.sql.gz         dump of the `data` schema, minus the tables
#                                  listed under "Credentials" below
#   data-sanitized-YYYY-MM-DD.sql.gz
#                                  those tables: structure for all of them, and
#                                  rows (with the credentials taken out) for the
#                                  ones that also hold ordinary data. Load it
#                                  AFTER data-*.sql.gz.
#   data-schema-YYYY-MM-DD.sql.gz  schema-only dump of `data` (alembic/versions
#                                  is gitignored, so this is the schema recovery path)
#   xenforo-YYYY-MM-DD.sql.gz      dump of the `xenforo` schema, same split
#   xenforo-sanitized-YYYY-MM-DD.sql.gz
#   redis-YYYY-MM-DD.rdb.gz        Redis RDB snapshot (nice-to-have; skipped with a
#                                  warning if unreadable — leaderboards are rebuildable)
#
# Then uploads the set to B2 under dt_backups/mysql/<token>/YYYY-MM-DD/ (the B2
# app key is namePrefix-restricted to "dt_") and prunes local
# (>LOCAL_RETENTION_DAYS) and remote (>REMOTE_RETENTION_DAYS) copies.
#
# THE BUCKET IS PUBLIC. video.droptracker.io serves it key for key, so a backup
# under a name anyone could guess is a backup anyone can download (found
# 2026-11-11: the old dt_backups/mysql/DATE/data-DATE.sql.gz names were exactly
# that). Two defences, both needed:
#   1. <token> is a keyed hash of the date (HMAC with the B2 application key):
#      the same every run for a given day, not guessable without the key.
#      `b2_backup_sync.py check` lists the real paths.
#   2. Credentials never leave the box. See "Credentials" below. A restore
#      therefore comes back WITHOUT them: Discord webhooks, users' auth tokens,
#      group export keys, WOM verification codes, plugin/admin sessions, forum
#      passwords and the forum's Discord login all have to be re-made.
#      deploy/BACKUPS.md lists what to redo.
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
#   LOCAL_RETENTION_DAYS=7      REMOTE_RETENTION_DAYS=3   (unit sets 1 / 3)
#   MIN_FREE_GB=25              minimum free space on the backup fs before starting
#   SKIP_B2=1                   local-only run (no upload / remote prune)
#   SKIP_REDIS=1                skip the Redis snapshot copy
#   KEEP_LOCAL_AFTER_UPLOAD=1   keep the local set after a successful B2 upload
#                               (default: delete it — see the upload step below)
#
set -euo pipefail

REPO_DIR="/store/droptracker/disc"
ENV_FILE="$REPO_DIR/.env"
PYTHON="$REPO_DIR/venv/bin/python"
B2_SYNC="$REPO_DIR/scripts/b2_backup_sync.py"

BACKUP_ROOT="${BACKUP_ROOT:-/store/droptracker/backups}"
LOCAL_RETENTION_DAYS="${LOCAL_RETENTION_DAYS:-7}"
REMOTE_RETENTION_DAYS="${REMOTE_RETENTION_DAYS:-3}"
MIN_FREE_GB="${MIN_FREE_GB:-25}"
SKIP_B2="${SKIP_B2:-0}"
SKIP_REDIS="${SKIP_REDIS:-0}"
KEEP_LOCAL_AFTER_UPLOAD="${KEEP_LOCAL_AFTER_UPLOAD:-0}"

# ---------------------------------------------------------------- Credentials
# What is kept out of every dump. If you add a table or column that stores a
# secret (a token, a webhook URL, a password, a key someone could use as-is),
# add it here in the same change.
#
# Whole tables: structure is kept (in *-sanitized-*), rows are not.
DATA_SECRET_TABLES=(
    webhooks backup_webhooks webhook_pending_deletion new_webhooks   # Discord webhook URLs/tokens
    api_key_reveals                                                  # one-time key reveals (ciphertext)
    group_verification_codes plugin_admin_sessions                   # codes and sessions
    admin_widget_tokens tester_update_keys
)
# Tables that hold ordinary data plus a secret: dumped without the secret.
#   "table|WHERE that keeps the safe rows"
DATA_FILTERED_TABLES=(
    "group_configurations|config_key NOT IN ('export_api_key','wom_verification_code')"
    "audit_log|CONCAT_WS(' ', target, \`before\`, \`after\`) NOT REGEXP 'api/webhooks/'"
)
#   "table|column to blank" (the column is NOT NULL; it becomes '')
DATA_BLANKED_COLUMNS=(
    "users|auth_token"
)
# Deliberately kept: api_keys (SHA-256 of a long random key, not usable as a
# key) and players.account_hash (an identifier the plugin sends, needed to
# recognise accounts after a restore).

XF_SECRET_TABLES=(
    xf_user_authenticate xf_user_remember xf_user_confirmation       # passwords, remember-me, reset links
    xf_user_tfa xf_user_tfa_trusted xf_passkey                       # two-factor secrets
    xf_session xf_session_admin xf_session_install                   # sessions
    xf_api_key xf_api_login_token xf_webhook                         # API keys, webhook secrets
    xf_oauth_client xf_oauth_code xf_oauth_refresh_token xf_oauth_request xf_oauth_token
    xf_connected_account_provider xf_user_connected_account          # Discord login secret + linked accounts
    xf_payment_profile                                               # payment provider keys
    xf_data_registry                                                 # cache that copies the options; rebuilt
    xf_error_log xf_admin_log                                        # logs that quote secrets (webhook URLs in traces)
)
XF_FILTERED_TABLES=(
    "xf_option|LOWER(CONVERT(option_id USING utf8mb4)) NOT REGEXP 'secret|key|passw|token|transport|captcha|oauth|stripe|paypal|api|smtp'"
)
XF_BLANKED_COLUMNS=(
    "xf_user|secret_key"
)
# Scratch schema the blanked copies are built in. Never a real schema.
readonly SCRATCH_DB="dt_backup_scratch"

# Fail-loudly floor sizes for the gzipped dumps: a "successful" dump smaller
# than this means something went badly wrong (empty schema, auth issue, etc).
DATA_MIN_BYTES=$((200 * 1024 * 1024))   # data full dump: >= 200 MB gz
XF_MIN_BYTES=$((5 * 1024 * 1024))       # xenforo dump: >= 5 MB gz (17 MB once the
                                        # error log, most of the old 215 MB, was left out)
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

dump_sanitized() {
    # dump_sanitized <schema> <outfile> <SECRET_TABLES name> <FILTERED name> <BLANKED name>
    #
    # Writes what dump_schema left out, minus the credentials:
    #   1. CREATE TABLE for every excluded table (no rows);
    #   2. rows of each filtered table that pass its WHERE;
    #   3. rows of each blanked table, read from a scratch copy whose secret
    #      column has been emptied. The live table is only ever read.
    local schema=$1 outfile=$2
    local -n secret_tables=$3 filtered=$4 blanked=$5
    local entry table clause column structure=()

    [[ "$SCRATCH_DB" == "dt_backup_scratch" ]] || die "scratch schema name was altered"
    structure=("${secret_tables[@]}")
    for entry in "${filtered[@]}" "${blanked[@]}"; do structure+=("${entry%%|*}"); done

    log "dumping ${schema} (sanitized) -> ${outfile}"
    {
        "${DUMP[@]}" --no-data --skip-triggers "$schema" "${structure[@]}"
        for entry in "${filtered[@]}"; do
            table="${entry%%|*}"; clause="${entry#*|}"
            "${DUMP[@]}" --no-create-info --skip-triggers --where="$clause" "$schema" "$table"
        done
        for entry in "${blanked[@]}"; do
            table="${entry%%|*}"; column="${entry#*|}"
            # NO_AUTO_VALUE_ON_ZERO: users has a real row with id 0, which a
            # plain INSERT would renumber.
            "${MYSQL[@]}" -e "
                SET SESSION sql_mode = CONCAT(@@sql_mode, ',NO_AUTO_VALUE_ON_ZERO');
                CREATE DATABASE IF NOT EXISTS \`${SCRATCH_DB}\`;
                DROP TABLE IF EXISTS \`${SCRATCH_DB}\`.\`${table}\`;
                CREATE TABLE \`${SCRATCH_DB}\`.\`${table}\` LIKE \`${schema}\`.\`${table}\`;
                INSERT INTO \`${SCRATCH_DB}\`.\`${table}\` SELECT * FROM \`${schema}\`.\`${table}\`;
                UPDATE \`${SCRATCH_DB}\`.\`${table}\` SET \`${column}\` = '';"
            "${DUMP[@]}" --no-create-info --skip-triggers "$SCRATCH_DB" "$table"
            "${MYSQL[@]}" -e "DROP TABLE IF EXISTS \`${SCRATCH_DB}\`.\`${table}\`;"
        done
    } | gzip > "$outfile"
    gzip -t "$outfile"
    log "dumped ${schema} (sanitized): $(du -h "$outfile" | cut -f1)"
}

ignore_args() {
    # ignore_args <schema> <SECRET_TABLES name> <FILTERED name> <BLANKED name>
    # --ignore-table arguments for everything dump_sanitized handles instead.
    local schema=$1
    local -n secret_tables=$2 filtered=$3 blanked=$4
    local entry
    for entry in "${secret_tables[@]}"; do printf -- '--ignore-table=%s.%s\n' "$schema" "$entry"; done
    for entry in "${filtered[@]}" "${blanked[@]}"; do
        printf -- '--ignore-table=%s.%s\n' "$schema" "${entry%%|*}"
    done
}

assert_no_secrets() {
    # assert_no_secrets <file>...
    # Last line of defence: a dump that still carries a Discord webhook URL is
    # not uploaded. Catches a new table or column nobody added to the lists.
    local file hit
    for file in "$@"; do
        hit="$(zcat "$file" | grep -c -m1 -E 'discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]{20,}' || true)"
        [[ "$hit" == "0" ]] || die "$file contains a Discord webhook URL; not uploading. Find the table and add it to the Credentials lists."
    done
    log "credential check: no webhook URLs in $# dump(s)"
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

mapfile -t DATA_IGNORE < <(ignore_args data    DATA_SECRET_TABLES DATA_FILTERED_TABLES DATA_BLANKED_COLUMNS)
mapfile -t XF_IGNORE   < <(ignore_args xenforo XF_SECRET_TABLES   XF_FILTERED_TABLES   XF_BLANKED_COLUMNS)

dump_schema data    "$OUT_DIR/data-$DATE_UTC.sql.gz"        "$DATA_MIN_BYTES" "${DATA_IGNORE[@]}"
dump_sanitized data "$OUT_DIR/data-sanitized-$DATE_UTC.sql.gz" \
    DATA_SECRET_TABLES DATA_FILTERED_TABLES DATA_BLANKED_COLUMNS
dump_schema data    "$OUT_DIR/data-schema-$DATE_UTC.sql.gz" "$SCHEMA_MIN_BYTES" --no-data
dump_schema xenforo "$OUT_DIR/xenforo-$DATE_UTC.sql.gz"     "$XF_MIN_BYTES" "${XF_IGNORE[@]}"
dump_sanitized xenforo "$OUT_DIR/xenforo-sanitized-$DATE_UTC.sql.gz" \
    XF_SECRET_TABLES XF_FILTERED_TABLES XF_BLANKED_COLUMNS

assert_no_secrets "$OUT_DIR/data-$DATE_UTC.sql.gz" "$OUT_DIR/data-sanitized-$DATE_UTC.sql.gz" \
    "$OUT_DIR/xenforo-$DATE_UTC.sql.gz" "$OUT_DIR/xenforo-sanitized-$DATE_UTC.sql.gz"

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
    # The bucket is public through the CDN: the path carries a keyed hash of
    # the date so it cannot be guessed (see the header).
    B2_SECRET="$(env_get B2_APPLICATION_KEY)"
    [[ -n "$B2_SECRET" ]] || die "B2_APPLICATION_KEY not set in $ENV_FILE; refusing to upload under a guessable name"
    PATH_TOKEN="$(B2_SECRET="$B2_SECRET" "$PYTHON" -c 'import hashlib, hmac, os, sys
print(hmac.new(os.environ["B2_SECRET"].encode(), sys.argv[1].encode(), hashlib.sha256).hexdigest()[:32])' "$DATE_UTC")"
    [[ "$PATH_TOKEN" =~ ^[0-9a-f]{32}$ ]] || die "could not derive the upload path token"
    log "uploading to B2 under dt_backups/mysql/<token>/$DATE_UTC/"
    "$PYTHON" "$B2_SYNC" upload-dir "$OUT_DIR" "dt_backups/mysql/$PATH_TOKEN/$DATE_UTC"
    log "pruning B2 objects older than ${REMOTE_RETENTION_DAYS} days"
    "$PYTHON" "$B2_SYNC" prune --days "$REMOTE_RETENTION_DAYS"
    # The local set is staging for the upload, not a retained copy. This box runs
    # chronically near-full, and parking ~5 GB here until the next nightly run is
    # exactly what starves Redis of the room it needs to write its RDB — a full
    # disk wedges Redis into MISCONF, which rejects every write and silently
    # freezes the whole submission queue (2026-08-18, 87 minutes of lost drops).
    # B2 holds REMOTE_RETENTION_DAYS of sets; set KEEP_LOCAL_AFTER_UPLOAD=1 to
    # keep today's for a fast local restore. Only reached when the upload and
    # remote prune both succeeded — set -e aborts the script otherwise, leaving
    # the local set in place as the only copy.
    if [[ "$KEEP_LOCAL_AFTER_UPLOAD" == "1" ]]; then
        log "KEEP_LOCAL_AFTER_UPLOAD=1: retaining local set $OUT_DIR"
    else
        log "removing local set $OUT_DIR (now offsite; KEEP_LOCAL_AFTER_UPLOAD=1 to retain)"
        rm -rf "$OUT_DIR"
    fi
fi

log "=== backup complete in $(( $(date -u +%s) - START_TS ))s ==="
