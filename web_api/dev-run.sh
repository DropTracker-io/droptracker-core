#!/usr/bin/env bash
#
# Control script for the DropTracker Web API v1 (Task 04 read surface).
# Runs as a SEPARATE process on :31325 with the project venv, independent of the
# RuneLite intake API on :31323.
#
# Usage:
#   web_api/dev-run.sh {start|stop|restart|status|logs}
#
# Env overrides:
#   WEB_API_PORT (default 31325)
#   WEB_API_HOST (default 127.0.0.1)   # BFF reaches it over localhost
#   WORKERS      (default 2)           # hypercorn workers

set -euo pipefail

REPO_ROOT="/store/droptracker/disc"
VENV_PY="$REPO_ROOT/venv/bin/python"
HYPERCORN="$REPO_ROOT/venv/bin/hypercorn"
PID_FILE="/tmp/dt-web-api.pid"
LOG_FILE="/tmp/dt-web-api.log"

PORT="${WEB_API_PORT:-31325}"
HOST="${WEB_API_HOST:-127.0.0.1}"
WORKERS="${WORKERS:-2}"

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

do_start() {
  if running; then echo ">> Already running (pid $(cat "$PID_FILE")) on $HOST:$PORT."; return 0; fi
  echo ">> Starting Web API v1 on $HOST:$PORT ($WORKERS workers) ..."
  cd "$REPO_ROOT"
  setsid "$HYPERCORN" \
      --workers "$WORKERS" --worker-class asyncio \
      --keep-alive 5 --graceful-timeout 10 \
      --bind "$HOST:$PORT" "web_api:create_app()" \
      >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 3
  if running; then
    echo ">> Up. pid $(cat "$PID_FILE"). Logs: $LOG_FILE"
  else
    echo "!! Failed to start. Last log lines:"; tail -20 "$LOG_FILE"; exit 1
  fi
}

do_stop() {
  if running; then
    local pid; pid="$(cat "$PID_FILE")"
    echo ">> Stopping pid $pid (and its process group) ..."
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    sleep 2
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo ">> Stopped."
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  if running; then echo "running (pid $(cat "$PID_FILE"), $HOST:$PORT)"; else echo "stopped"; fi ;;
  logs)    tail -f "$LOG_FILE" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs}"; exit 1 ;;
esac
