#!/usr/bin/env bash
# Start the Knowly web app and the Firebase emulators for local development:  ./run.sh
#
# - installs npm packages on first run and whenever package-lock.json changes
# - starts the Auth, Firestore and Storage emulators on all interfaces (UI on port 4000)
#   and keeps their data between runs in ../.emulator-data (FRESH=1 ./run.sh starts empty)
# - serves the web app on 0.0.0.0:5173, so other devices on your network can open it
#   (it proxies /api to the backend on :8000); HOST=127.0.0.1 ./run.sh for local only
# Ctrl+C stops the web app and the emulators together.
#
# Start the API with ../backend/run.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
DATA_DIR="../.emulator-data"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-5173}"

for tool in node npm java; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "'$tool' is required but was not found on PATH (Node 22+, Java 21+ for the emulators)." >&2
    exit 1
  fi
done

# --- dependencies (reinstalled only when package-lock.json changes) ---------------------
STAMP="node_modules/.deps-installed"
HASH="$(node -e "process.stdout.write(require('crypto').createHash('sha256').update(require('fs').readFileSync('package-lock.json')).digest('hex'))")"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
  echo "Installing npm packages ..."
  npm install --no-audit --no-fund
  echo "$HASH" > "$STAMP"
fi

if curl -s -m 2 "http://127.0.0.1:8000/api/health" >/dev/null 2>&1; then
  echo "Knowly API: running on :8000"
else
  echo "Knowly API: not running yet. Start it with ../backend/run.sh"
fi

# --- emulators + web app -----------------------------------------------------------------
echo "Web app: http://localhost:$PORT on this machine; listening on $HOST:$PORT (from other devices use http://<this-PC-IP>:$PORT)"

# All three emulators must answer. A leftover Firestore process alone (for example after Ctrl+C) would otherwise
# look like "running" and the web app would fail with auth/network-request-failed.
if curl -s -m 2 "http://127.0.0.1:8080" >/dev/null 2>&1 && curl -s -m 2 "http://127.0.0.1:9099" >/dev/null 2>&1; then
  echo "Firebase emulators are already running; starting only the web app."
  exec npx vite --port "$PORT" --strictPort --host "$HOST"
fi
if curl -s -m 2 "http://127.0.0.1:8080" >/dev/null 2>&1; then
  echo "A Firestore emulator is running without Auth (a leftover process). Stop it, then run this script again:" >&2
  echo "  taskkill //F //IM java.exe    (Windows Git Bash)    or    pkill -f cloud-firestore-emulator" >&2
  exit 1
fi

IMPORT=()
if [ "${FRESH:-0}" != "1" ] && [ -d "$DATA_DIR" ]; then
  IMPORT=(--import="$DATA_DIR")
fi
mkdir -p "$DATA_DIR"

echo "Starting Firebase emulators and the web app ..."
exec npx firebase emulators:exec \
  --only auth,firestore,storage \
  --project demo-knowly \
  --ui \
  "${IMPORT[@]}" --export-on-exit="$DATA_DIR" \
  "npx vite --port $PORT --strictPort --host $HOST"
