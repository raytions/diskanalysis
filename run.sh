#!/usr/bin/env bash
# One-click startup for DiskAnalysis
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Docker mode (default) ──────────────────────────────────────────────
if command -v docker &>/dev/null && [[ "${MODE:-docker}" == "docker" ]]; then
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "[run.sh] Created .env from .env.example — edit it if needed."
  fi
  mkdir -p data
  echo "[run.sh] NOTE: Edit docker-compose.yml to add your scan directories as :ro volumes."
  echo "[run.sh] Starting with Docker Compose..."
  docker compose up -d --build
  echo "[run.sh] Web panel: http://localhost:${APP_PORT:-8000}"
  echo "[run.sh] Logs: docker compose logs -f"
  exit 0
fi

# ── Direct Python mode (MODE=python) ──────────────────────────────────
if [[ "${MODE:-docker}" == "python" ]]; then
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "[run.sh] Created .env from .env.example."
  fi
  # Source .env
  set -a; source .env; set +a

  # Use local ./data directory instead of /data
  export DB_PATH="${DB_PATH:-$SCRIPT_DIR/data/diskanalysis.db}"
  mkdir -p "$(dirname "$DB_PATH")"

  if ! python3 -c "import uvicorn" 2>/dev/null; then
    echo "[run.sh] Installing dependencies..."
    pip3 install -r requirements.txt --break-system-packages -q
  fi

  # Default to dual-stack. 0.0.0.0 is IPv4-only, and this host's
  # campus name (yanglab5.bg.ic.ac.uk) has only an AAAA record.
  if [[ "${APP_HOST:-::}" == "0.0.0.0" ]]; then
    APP_HOST="::"
  fi
  echo "[run.sh] Starting DiskAnalysis on http://[${APP_HOST}]:${APP_PORT:-8000}"
  DB_PATH="$DB_PATH" APP_HOST="$APP_HOST" python3 -m app.serve
  exit 0
fi

echo "Usage: MODE=docker ./run.sh   # Docker Compose (default)"
echo "       MODE=python ./run.sh   # Direct Python"
