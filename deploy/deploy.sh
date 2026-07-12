#!/usr/bin/env bash
#
# DropTracker backend deploy — one command: pull, install, migrate, test, restart, verify.
#
# Usage:
#   deploy/deploy.sh [scope flags] [options]
#
# Scope flags (restart targets; default is --all):
#   --all       everything below
#   --api       droptracker-api                 (intake API :31323)
#   --webapi    droptracker-webapi              (website API :31325)
#   --bots      droptracker-core droptracker-webhooks droptracker-hof droptracker-heartbeat
#   --workers   droptracker-events droptracker-webhook-consumer
#               droptracker-video-worker droptracker-player-updates
#   --boards    droptracker-lootboards          (lootboard image generator)
#
# Options:
#   --no-pull      skip `git pull --ff-only`
#   --skip-tests   skip the unit-test gate
#   --dry-run      print state-changing commands instead of executing them
#                  (read-only checks — alembic current/heads, curl, is-active —
#                  still run for real so you see what WOULD happen)
#   -h | --help    this text
#
# Requires: the invoking user can `sudo systemctl restart droptracker-*`.
# The migration guard exists because we once shipped code whose alembic
# migration was never applied -> 1054 "unknown column" errors in prod.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/venv"
cd "$REPO_ROOT"

# ---------------------------------------------------------------- unit groups
UNITS_API=(droptracker-api)
UNITS_WEBAPI=(droptracker-webapi)
UNITS_BOTS=(droptracker-core droptracker-webhooks droptracker-hof droptracker-heartbeat)
UNITS_WORKERS=(droptracker-events droptracker-webhook-consumer droptracker-video-worker droptracker-player-updates)
UNITS_BOARDS=(droptracker-lootboards)

# ------------------------------------------------------------------ arg parse
DRY_RUN=0
NO_PULL=0
SKIP_TESTS=0
SCOPE_API=0 SCOPE_WEBAPI=0 SCOPE_BOTS=0 SCOPE_WORKERS=0 SCOPE_BOARDS=0
ANY_SCOPE=0

usage() { sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)        SCOPE_API=1; SCOPE_WEBAPI=1; SCOPE_BOTS=1; SCOPE_WORKERS=1; SCOPE_BOARDS=1; ANY_SCOPE=1 ;;
        --api)        SCOPE_API=1;     ANY_SCOPE=1 ;;
        --webapi)     SCOPE_WEBAPI=1;  ANY_SCOPE=1 ;;
        --bots)       SCOPE_BOTS=1;    ANY_SCOPE=1 ;;
        --workers)    SCOPE_WORKERS=1; ANY_SCOPE=1 ;;
        --boards)     SCOPE_BOARDS=1;  ANY_SCOPE=1 ;;
        --no-pull)    NO_PULL=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        --dry-run)    DRY_RUN=1 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown flag: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# Default scope: everything.
if [[ $ANY_SCOPE -eq 0 ]]; then
    SCOPE_API=1; SCOPE_WEBAPI=1; SCOPE_BOTS=1; SCOPE_WORKERS=1; SCOPE_BOARDS=1
fi

UNITS=()
[[ $SCOPE_API -eq 1 ]]     && UNITS+=("${UNITS_API[@]}")
[[ $SCOPE_WEBAPI -eq 1 ]]  && UNITS+=("${UNITS_WEBAPI[@]}")
[[ $SCOPE_BOTS -eq 1 ]]    && UNITS+=("${UNITS_BOTS[@]}")
[[ $SCOPE_WORKERS -eq 1 ]] && UNITS+=("${UNITS_WORKERS[@]}")
[[ $SCOPE_BOARDS -eq 1 ]]  && UNITS+=("${UNITS_BOARDS[@]}")

# --------------------------------------------------------------------- helpers
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# run: execute a state-changing command, or just print it under --dry-run.
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[dry-run] would run: $*"
    else
        echo "+ $*"
        "$@"
    fi
}

# Summary bookkeeping.
SUMMARY_NAMES=()
SUMMARY_RESULTS=()
record() { SUMMARY_NAMES+=("$1"); SUMMARY_RESULTS+=("$2"); }

# wait_http <label> <url>: poll a health URL for up to ~60s.
wait_http() {
    local label="$1" url="$2" tries=30 i
    for ((i = 1; i <= tries; i++)); do
        if curl -fsS -o /dev/null --max-time 5 "$url"; then
            echo "  $label OK ($url)"
            record "$label" PASS
            return 0
        fi
        sleep 2
    done
    echo "  $label FAILED after ${tries} attempts ($url)" >&2
    record "$label" FAIL
    return 1
}

# ----------------------------------------------------------------- 1. git pull
if [[ $NO_PULL -eq 1 ]]; then
    step "git pull (skipped: --no-pull)"
else
    step "git pull --ff-only ($(git branch --show-current))"
    run git pull --ff-only
fi

# ------------------------------------------------------------ 2. dependencies
step "pip install -r requirements.txt"
run "$VENV/bin/pip" install -r requirements.txt -q

# -------------------------------------------------------- 3. migration guard
# Compare the DB revision against the code's head revision(s). Deploying code
# without its migration has caused live 1054 "unknown column" errors before.
# The comparison itself is read-only, so it runs even under --dry-run.
step "alembic migration guard"
# alembic/env.py prints the connection string (with password) on stdout;
# filter it out and keep only revision ids. INFO logs go to stderr.
CURRENT_REVS="$("$VENV/bin/alembic" current 2>/dev/null \
    | grep -vE '^Alembic connection string' | awk 'NF {print $1}' | sort)"
HEAD_REVS="$("$VENV/bin/alembic" heads 2>/dev/null \
    | grep -vE '^Alembic connection string' | awk 'NF {print $1}' | sort)"
if [[ -z "$HEAD_REVS" ]]; then
    echo "Could not determine alembic heads — aborting." >&2
    exit 1
fi
if [[ "$CURRENT_REVS" == "$HEAD_REVS" ]]; then
    echo "  DB is up to date at: $(echo "$HEAD_REVS" | tr '\n' ' ')"
else
    echo "  DB is BEHIND."
    echo "    db current : ${CURRENT_REVS:-<none>}" | tr '\n' ' '; echo
    echo "    code heads : $(echo "$HEAD_REVS" | tr '\n' ' ')"
    echo "  Running: alembic upgrade head"
    if ! run "$VENV/bin/alembic" upgrade head; then
        echo "alembic upgrade head FAILED — aborting deploy (nothing restarted)." >&2
        exit 1
    fi
fi

# -------------------------------------------------------------- 4. unit tests
if [[ $SKIP_TESTS -eq 1 ]]; then
    step "unit tests (skipped: --skip-tests)"
else
    step "unit tests"
    run "$VENV/bin/python" -m pytest tests/unit -q
fi

# ---------------------------------------------------------------- 5. restarts
step "restarting units: ${UNITS[*]}"
for unit in "${UNITS[@]}"; do
    run sudo systemctl restart "$unit"
done

# ------------------------------------------------------------ 6. health checks
step "health checks"
if [[ $DRY_RUN -eq 1 ]]; then
    echo "  (dry-run: nothing was restarted — checks below reflect the currently running services)"
fi

HEALTH_FAILED=0
if [[ $SCOPE_API -eq 1 ]]; then
    wait_http "http :31323/ping" "http://127.0.0.1:31323/ping" || HEALTH_FAILED=1
fi
if [[ $SCOPE_WEBAPI -eq 1 ]]; then
    wait_http "http :31325/api/v1/health" "http://127.0.0.1:31325/api/v1/health" || HEALTH_FAILED=1
fi
for unit in "${UNITS[@]}"; do
    if state="$(systemctl is-active "$unit" 2>&1)"; then
        echo "  $unit: $state"
        record "unit $unit" PASS
    else
        echo "  $unit: ${state:-unknown}" >&2
        record "unit $unit" FAIL
        HEALTH_FAILED=1
    fi
done

# ------------------------------------------------------------------ 7. summary
step "summary"
printf '  %-34s %s\n' "CHECK" "RESULT"
printf '  %-34s %s\n' "-----" "------"
for i in "${!SUMMARY_NAMES[@]}"; do
    printf '  %-34s %s\n' "${SUMMARY_NAMES[$i]}" "${SUMMARY_RESULTS[$i]}"
done
if [[ $HEALTH_FAILED -eq 1 ]]; then
    printf '\n\033[31mDEPLOY: FAIL\033[0m — one or more checks failed.\n'
    exit 1
fi
if [[ $DRY_RUN -eq 1 ]]; then
    printf '\nDRY-RUN COMPLETE — no state was changed.\n'
else
    printf '\n\033[32mDEPLOY: PASS\033[0m\n'
fi
