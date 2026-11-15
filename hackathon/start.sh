#!/usr/bin/env bash
# Start the whole stack: Python simulator service -> Spring Boot API -> Vite dashboard.
# Ctrl+C stops all three.
#
#   ./start.sh
#   AGENT_DIR=/path/to/agtech_agent ./start.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="${AGENT_DIR:-$ROOT/../agtech _agent}"
SIM_API_PORT="${SIM_API_PORT:-8765}"
export SIM_SERVICE_URL="${SIM_SERVICE_URL:-http://127.0.0.1:$SIM_API_PORT}"

if [[ ! -f "$AGENT_DIR/api_server.py" ]]; then
  echo "api_server.py not found in AGENT_DIR=$AGENT_DIR" >&2
  exit 1
fi
PY="$AGENT_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

wait_for() { # url name
  for _ in $(seq 1 90); do curl -sf "$1" >/dev/null && return 0; sleep 1; done
  echo "$2 did not start; see output above" >&2
  exit 1
}

echo "▶ simulator service on :$SIM_API_PORT"
(cd "$AGENT_DIR" && SIM_API_PORT="$SIM_API_PORT" "$PY" api_server.py) &
pids+=($!)
wait_for "http://127.0.0.1:$SIM_API_PORT/health" "simulator service"

echo "▶ Spring Boot on :8080"
(cd "$ROOT/backend" && ./mvnw -q spring-boot:run) &
pids+=($!)
wait_for "http://127.0.0.1:8080/api/health" "Spring Boot"

echo "▶ dashboard on :5173"
(cd "$ROOT/frontend" && { [[ -d node_modules ]] || npm install; } && npx vite --port 5173) &
pids+=($!)
wait_for "http://127.0.0.1:5173" "Vite"

echo "✔ open http://localhost:5173"
wait
