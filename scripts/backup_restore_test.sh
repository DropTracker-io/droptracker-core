#!/usr/bin/env bash
#
# Restore-test the most recent local `data` backup into a scratch schema.
#
# What it does:
#   1. Finds the newest full data-YYYY-MM-DD.sql.gz under /store/droptracker/backups/
#   2. Streams the whole dump through a grep-guard: aborts if it contains any
#      CREATE DATABASE / USE statement (our dumps are taken per-schema without
#      --databases, so there should be none — this guard is what makes the
#      restore incapable of escaping the scratch schema).
#   3. DROP + CREATE the scratch schema `restore_test_data`, pipes the dump in.
#   4. Sanity checks: table count, `drops` > 100M rows, `users`/`groups` non-empty.
#   5. Prints PASS/FAIL and DROPs the scratch schema (KEEP_SCRATCH=1 to keep it).
#
# SAFETY: the scratch schema name is HARDCODED and readonly. This script never
# names the real `data` or `xenforo` schemas in any statement it executes.
#
# COST WARNING: this replays ~165M rows single-threaded. Expect several hours
# and ~35 GiB of extra InnoDB disk usage while the scratch schema exists.
# Run it off-peak, and not while the nightly backup is dumping.
#
# Env overrides (optional):
#   BACKUP_ROOT=/store/droptracker/backups
#   RESTORE_MIN_FREE_GB=40    free-space floor before starting
#   KEEP_SCRATCH=1            keep restore_test_data afterwards (e.g. for a
#                             single-table recovery) — drop it manually later
#
set -euo pipefail

# The ONLY schema this script is allowed to create/drop/write. Hardcoded on
# purpose — do not parameterize.
readonly SCRATCH_DB="restore_test_data"

REPO_DIR="/store/droptracker/disc"
ENV_FILE="$REPO_DIR/.env"
BACKUP_ROOT="${BACKUP_ROOT:-/store/droptracker/backups}"
RESTORE_MIN_FREE_GB="${RESTORE_MIN_FREE_GB:-40}"
KEEP_SCRATCH="${KEEP_SCRATCH:-0}"

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "FATAL: $*"; exit 1; }

# Paranoia: if anything ever tampers with the scratch name, stop immediately.
[[ "$SCRATCH_DB" == "restore_test_data" ]] || die "scratch schema name was altered"
case "$SCRATCH_DB" in
    data|xenforo|mysql|information_schema|performance_schema|sys)
        die "scratch schema name collides with a real schema" ;;
esac

env_get() {
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

MYCNF="$(mktemp /dev/shm/dt-restore-my.XXXXXX 2>/dev/null || mktemp)"
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

drop_scratch() {
    # Guarded drop of the hardcoded scratch schema only.
    [[ "$SCRATCH_DB" == "restore_test_data" ]] || { log "refusing to drop: scratch name altered"; return 1; }
    "${MYSQL[@]}" -e "DROP DATABASE IF EXISTS \`restore_test_data\`;"
}

cleanup() {
    local rc=$?
    if [[ "$KEEP_SCRATCH" == "1" ]]; then
        log "KEEP_SCRATCH=1: leaving \`$SCRATCH_DB\` in place (drop it manually when done)"
    else
        log "dropping scratch schema \`$SCRATCH_DB\`"
        drop_scratch || log "WARN: failed to drop \`$SCRATCH_DB\` — drop it manually"
    fi
    rm -f "$MYCNF"
    exit "$rc"
}
trap cleanup EXIT

# ------------------------------------------------------------- pick the dump
# Full data dumps only — the pattern excludes data-schema-*.sql.gz.
DUMP="$(ls -1 "$BACKUP_ROOT"/20??-??-??/data-20??-??-??.sql.gz 2>/dev/null | sort | tail -n 1 || true)"
[[ -n "$DUMP" ]] || die "no data-YYYY-MM-DD.sql.gz found under $BACKUP_ROOT/<date>/ — run a backup first"
log "restore-testing dump: $DUMP ($(du -h "$DUMP" | cut -f1))"
gzip -t "$DUMP" || die "dump failed gzip integrity check"

# ------------------------------------------------------------ free space gate
avail_kb="$(df -Pk "$BACKUP_ROOT" | awk 'NR==2 {print $4}')"
need_kb=$((RESTORE_MIN_FREE_GB * 1024 * 1024))
log "free space: $((avail_kb / 1024 / 1024)) GiB (require >= ${RESTORE_MIN_FREE_GB} GiB for the scratch schema)"
(( avail_kb >= need_kb )) || die "insufficient free space for a scratch restore"

# ---------------------------------------------------------------- grep guard
# The dump must not contain CREATE DATABASE or USE statements: those could
# redirect the restore at the real schemas. Full-stream scan; -m1 short-circuits.
log "grep-guard: scanning dump for CREATE DATABASE / USE statements"
# NOTE: keyed on grep's *output*, not its exit code — with pipefail, zcat's
# SIGPIPE after an early -m1 match would otherwise mask a hit.
guard_hit="$(zcat "$DUMP" | grep -m1 -nE '^[[:space:]]*(CREATE[[:space:]]+DATABASE|USE[[:space:]])' || true)"
[[ -z "$guard_hit" ]] || die "dump contains schema-switching statement — refusing to restore it: $guard_hit"
log "grep-guard: clean (no schema-switching statements)"

# -------------------------------------------------------------------- restore
"${MYSQL[@]}" -N -e "SELECT 1" >/dev/null || die "cannot connect to MariaDB"
log "recreating scratch schema \`$SCRATCH_DB\`"
drop_scratch
"${MYSQL[@]}" -e "CREATE DATABASE \`restore_test_data\` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"

log "restoring into \`$SCRATCH_DB\` (this takes hours for ~165M rows)..."
t0="$(date -u +%s)"
zcat "$DUMP" | "${MYSQL[@]}" --database="$SCRATCH_DB"
log "restore finished in $(( $(date -u +%s) - t0 ))s"

# -------------------------------------------------------------- sanity checks
PASS=1
check() {
    # check <label> <sql-returning-one-number> <min-exclusive>
    local label=$1 sql=$2 min=$3 val
    val="$("${MYSQL[@]}" -N -e "$sql")" || val=""
    [[ "$val" =~ ^[0-9]+$ ]] || val=""
    if [[ -n "$val" ]] && (( val > min )); then
        log "CHECK PASS: $label = $val (> $min)"
    else
        log "CHECK FAIL: $label = ${val:-<query failed>} (needed > $min)"
        PASS=0
    fi
}

log "running sanity checks (row counts on big tables take a few minutes)"
check "table count"   "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='restore_test_data'" 80
check "drops rows"    "SELECT COUNT(*) FROM \`restore_test_data\`.\`drops\`" 100000000
check "users rows"    "SELECT COUNT(*) FROM \`restore_test_data\`.\`users\`" 0
check "groups rows"   "SELECT COUNT(*) FROM \`restore_test_data\`.\`groups\`" 0

echo
if (( PASS == 1 )); then
    log "==================== RESTORE TEST: PASS ===================="
    log "dump $DUMP restores cleanly and passes all sanity checks"
else
    log "==================== RESTORE TEST: FAIL ===================="
    log "one or more sanity checks failed — inspect the output above"
    exit 1
fi
