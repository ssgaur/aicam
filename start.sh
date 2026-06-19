#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AiCam — One-command setup & launch
#
# Usage:
#   ./start.sh          # interactive setup (first time) + launch backend
#   ./start.sh --run    # skip setup checks, just start backend
#
# Works on: macOS, Linux, Windows (Git Bash / WSL)
# Prereqs:  git clone of this repo, Python 3.10+, USB-C cable + Android phone
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/data/startup.log"
mkdir -p data
exec > >(tee -a "$LOG_FILE") 2>&1

# ───────── Helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; echo -e "${RED}    → See troubleshooting below or check $LOG_FILE${NC}"; }

troubleshoot() {
  echo ""
  echo -e "${YELLOW}──── Troubleshooting ────${NC}"
  echo "  1. Python not found?     brew install python@3.12 (Mac) | sudo apt install python3 (Linux)"
  echo "  2. ffmpeg not found?     brew install ffmpeg (Mac) | sudo apt install ffmpeg (Linux)"
  echo "  3. Postgres not running? brew services start postgresql@16"
  echo "  4. Phone not detected?   Enable USB Debugging: Settings → Developer Options → USB Debugging"
  echo "  5. Backend won't start?  Check port 8100: lsof -nP -iTCP:8100 -sTCP:LISTEN"
  echo "  6. Phone can't reach?    Same Wi-Fi? Try curl http://<LAN_IP>:8100/healthz from phone browser"
  echo "  7. Full log:             $LOG_FILE"
  echo ""
}

detect_os() {
  case "$(uname -s)" in
    Darwin*) echo "mac" ;;
    Linux*)  echo "linux" ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *) echo "unknown" ;;
  esac
}

check_cmd() {
  if command -v "$1" &>/dev/null; then
    ok "$1 found ($(command -v "$1"))"
    return 0
  else
    fail "$1 not found"
    return 1
  fi
}

# ───────── OS detection ─────────────────────────────────────────────────────
OS=$(detect_os)
log "AiCam start.sh — OS detected: $OS"
echo "─────────────────────────────────────────────────────"

# ───────── Skip-to-run mode ─────────────────────────────────────────────────
if [[ "${1:-}" == "--run" ]]; then
  log "Fast mode — skipping setup checks"
  exec bash -c "cd '$SCRIPT_DIR/backend' && source ../venv/bin/activate && exec uvicorn main:app --host 0.0.0.0 --port 8100"
fi

# ───────── Step 1: Check prerequisites ──────────────────────────────────────
log "Step 1/6 — Checking prerequisites"
MISSING=0

# Python
PYTHON=""
for p in python3.12 python3.11 python3.10 python3; do
  if command -v "$p" &>/dev/null; then
    PYTHON="$p"; break
  fi
done
if [[ -n "$PYTHON" ]]; then
  PY_VER=$($PYTHON --version 2>&1)
  ok "Python: $PY_VER ($PYTHON)"
else
  fail "Python 3.10+ required"
  MISSING=1
fi

check_cmd ffmpeg || MISSING=1
check_cmd git || MISSING=1

# ADB (optional but needed for wired phone connection)
if check_cmd adb; then
  PHONE=$(adb devices 2>/dev/null | grep -w device | head -1 | awk '{print $1}')
  if [[ -n "$PHONE" ]]; then
    ok "Android phone connected: $PHONE"
  else
    warn "ADB found but no phone detected — plug in USB-C cable with USB Debugging enabled"
  fi
else
  warn "adb not found — install Android Platform Tools for phone management"
fi

# Postgres (required for durable analytics)
if check_cmd psql; then
  if psql -d aicam -c "SELECT 1" &>/dev/null; then
    ok "PostgreSQL 'aicam' database accessible"
  else
    warn "psql found but 'aicam' database missing — will create"
    createdb aicam 2>/dev/null && ok "Created 'aicam' database" || warn "Could not create DB (non-fatal, create manually)"
  fi
else
  warn "psql not found — Postgres is optional but recommended for durable analytics"
fi

if [[ $MISSING -eq 1 ]]; then
  troubleshoot
  exit 1
fi

echo ""

# ───────── Step 2: Python venv + deps ──────────────────────────────────────
log "Step 2/6 — Python environment"
if [[ ! -d venv ]]; then
  log "Creating virtual environment..."
  $PYTHON -m venv venv
  ok "venv created"
fi
source venv/bin/activate
ok "venv activated ($(python --version))"

if [[ ! -f venv/.deps_installed ]] || [[ requirements.txt -nt venv/.deps_installed ]]; then
  log "Installing dependencies (may take 1-2 minutes first time)..."
  pip install --upgrade pip --quiet 2>/dev/null
  pip install -r requirements.txt --quiet
  touch venv/.deps_installed
  ok "Dependencies installed"
else
  ok "Dependencies up to date"
fi
echo ""

# ───────── Step 3: Database schema ─────────────────────────────────────────
log "Step 3/6 — Database setup"
python postgres_store.py schema 2>/dev/null && ok "Postgres schema ready" || warn "Postgres schema skipped (non-fatal)"
echo ""

# ───────── Step 4: Find LAN IP ─────────────────────────────────────────────
log "Step 4/6 — Network"
LAN_IP=""
case "$OS" in
  mac)
    for iface in en0 en1 en8; do
      ip=$(ipconfig getifaddr "$iface" 2>/dev/null)
      if [[ -n "$ip" && "$ip" != 169.* ]]; then LAN_IP="$ip"; break; fi
    done
    # fallback: check all
    if [[ -z "$LAN_IP" ]]; then
      LAN_IP=$(ifconfig 2>/dev/null | grep "inet " | grep -v "127.0.0.1\|169.254" | head -1 | awk '{print $2}')
    fi
    ;;
  linux)
    LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    ;;
  windows)
    LAN_IP=$(ipconfig 2>/dev/null | grep "IPv4" | head -1 | awk -F: '{print $2}' | tr -d ' ')
    ;;
esac

if [[ -n "$LAN_IP" ]]; then
  ok "LAN IP: $LAN_IP"
  echo -e "  ${GREEN}Phone backend URL: http://$LAN_IP:8100${NC}"
else
  warn "Could not auto-detect LAN IP. Phone and Mac must be on same network."
  warn "Find it manually: ifconfig | grep 'inet ' (Mac/Linux)"
fi
echo ""

# ───────── Step 5: Kill existing backend if running ────────────────────────
log "Step 5/6 — Stopping old backend (if any)"
OLD_PID=$(lsof -nP -iTCP:8100 -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "$OLD_PID" ]]; then
  kill "$OLD_PID" 2>/dev/null && sleep 2
  ok "Killed old backend (pid $OLD_PID)"
else
  ok "Port 8100 is free"
fi
echo ""

# ───────── Step 6: Launch backend ──────────────────────────────────────────
log "Step 6/6 — Starting backend"
cd backend
nohup uvicorn main:app --host 0.0.0.0 --port 8100 > ../data/server.log 2>&1 &
BACKEND_PID=$!
disown $BACKEND_PID 2>/dev/null
cd ..

sleep 5
if curl -s --max-time 4 "http://localhost:8100/healthz" &>/dev/null; then
  ok "Backend running (pid $BACKEND_PID) on port 8100"
else
  fail "Backend failed to start — check data/server.log"
  tail -20 data/server.log 2>/dev/null
  troubleshoot
  exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo -e "${GREEN} AiCam is ready!${NC}"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Backend:  http://localhost:8100"
if [[ -n "$LAN_IP" ]]; then
echo "  Phone:    Set backend URL to → http://$LAN_IP:8100"
fi
echo "  Viewer:   http://localhost:8100/viewer"
echo "  Status:   http://localhost:8100/api/native/status"
echo "  Logs:     tail -f data/server.log"
echo "  Process:  tail -f data/processing.log"
echo ""
echo "  Next steps:"
echo "    1. On your Android phone → open AiCameraX app"
echo "    2. Set Backend URL → http://$LAN_IP:8100"
echo "    3. Tap 'Test' → should show ✓"
echo "    4. Tap 'Start' → recording begins"
echo "    5. Open http://localhost:8100/viewer to browse recordings"
echo ""
echo "  To stop:  kill $BACKEND_PID"
echo "═══════════════════════════════════════════════════════════════"
