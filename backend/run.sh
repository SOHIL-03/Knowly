#!/usr/bin/env bash
# Start the Knowly API for local development:  ./run.sh   (from anywhere)
#
# - activates the conda environment "ht" (override with CONDA_ENV=name ./run.sh)
# - installs Python packages on first run and whenever requirements change
# - checks backend/.env (GEMINI_API_KEY is the only value you must set)
# - serves on 0.0.0.0:8000 (all interfaces) with auto-reload; HOST=127.0.0.1 ./run.sh for local only
#
# Start the Firebase emulators with ../frontend/run.sh (they can start before or after this).
set -euo pipefail

ENV_NAME="${CONDA_ENV:-ht}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- conda --------------------------------------------------------------------------------
if ! command -v conda >/dev/null 2>&1; then
  echo "conda was not found on PATH. Open an Anaconda-enabled shell, or run 'conda init bash' once." >&2
  exit 1
fi
CONDA_BASE="$(conda info --base | sed 's#\\#/#g')"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "Creating conda environment '$ENV_NAME' with Python 3.12 ..."
  conda create -n "$ENV_NAME" python=3.12 -y
fi
conda activate "$ENV_NAME"

if ! python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Python 3.12 or newer is required; '$ENV_NAME' has $(python --version 2>&1)." >&2
  exit 1
fi

# --- dependencies (reinstalled only when the requirement files change) ---------------------
STAMP=".deps-installed"
HASH="$(cat requirements.txt requirements-dev.txt | python -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
  echo "Installing Python packages into '$ENV_NAME' ..."
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements-dev.txt
  echo "$HASH" > "$STAMP"
fi

# --- configuration --------------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created backend/.env from .env.example."
fi
# Count configured Gemini keys (GEMINI_API_KEY_1..10, or the single GEMINI_API_KEY) without printing them.
KEY_COUNT=0
while IFS= read -r line; do
  value="$(printf '%s' "${line#*=}" | tr -d '"' | tr -d "'" | tr -d '[:space:]')"
  [ -n "$value" ] && KEY_COUNT=$((KEY_COUNT + 1))
done < <(grep -E '^GEMINI_API_KEY(_([1-9]|10))?=' .env || true)
if [ "$KEY_COUNT" -eq 0 ]; then
  echo "Set GEMINI_API_KEY_1 (and optionally _2.._10) in backend/.env, then run this script again." >&2
  exit 1
fi
echo "Gemini API keys configured: $KEY_COUNT"

if curl -s -m 2 "http://127.0.0.1:8080" >/dev/null 2>&1; then
  echo "Firestore emulator: running"
else
  echo "Firestore emulator: not running yet. Start it with ../frontend/run.sh (the API waits for it per request)."
fi

echo "Knowly API on http://$HOST:$PORT  (this machine: http://localhost:$PORT/api/docs, ready check: /api/ready)"
exec python -m uvicorn app.main:app --reload --host "$HOST" --port "$PORT"
