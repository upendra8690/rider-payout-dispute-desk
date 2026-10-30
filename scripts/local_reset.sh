#!/usr/bin/env bash
# Fresh-state reset for running WITHOUT Docker (local Postgres + PaySwift + uvicorn processes).
# With Docker use: docker compose down -v && docker compose up -d --wait
#
# Env: DATABASE_ADMIN_URL (postgres superuser URL), DB_NAME, DB_USER, VENV, RUN_DIR, PAYSWIFT_PY
set -euo pipefail
cd "$(dirname "$0")/.."

DATABASE_ADMIN_URL=${DATABASE_ADMIN_URL:-postgresql://postgres@localhost:5432/postgres}
DB_NAME=${DB_NAME:-dispute_desk}
DB_USER=${DB_USER:-qd}
VENV=${VENV:-.venv}
RUN_DIR=${RUN_DIR:-/tmp/dispute-desk}
PAYSWIFT_PY=${PAYSWIFT_PY:-python3.12}
mkdir -p "$RUN_DIR"

stop() {  # stop a process started by this script (pid file), wait for it to exit
  local pidfile="$RUN_DIR/$1.pid"
  if [[ -f "$pidfile" ]]; then
    local pid; pid=$(cat "$pidfile")
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$pidfile"
  fi
}
stop app
stop payswift

psql "$DATABASE_ADMIN_URL" -q -c "DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE);" </dev/null
psql "$DATABASE_ADMIN_URL" -q -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" </dev/null

( cd payswift && PORT=8081 exec "$PAYSWIFT_PY" -u server.pyc ) </dev/null >"$RUN_DIR/payswift.log" 2>&1 &
echo $! > "$RUN_DIR/payswift.pid"
"$VENV/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000 </dev/null >"$RUN_DIR/app.log" 2>&1 &
echo $! > "$RUN_DIR/app.pid"

for _ in $(seq 1 90); do
  if curl -fs localhost:8000/health >/dev/null 2>&1 && curl -fs localhost:8081/health >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
echo "services did not become healthy (logs in $RUN_DIR)" >&2
exit 1
