#!/bin/bash
#
# setup.sh — UPDATED (Section 20 — logging): added log_line() helper,
# writing timestamped checkpoints to logs/devmesh_hooks.log alongside the
# existing terminal output, so a re-run's history is reviewable (e.g. "did
# the webhook listener actually start last time, or silently fail?").
#
# (Full original header/behavior otherwise unchanged — see prior version
# for the complete two-bugs-found-and-fixed writeup: post-commit's
# port-collision fix, and Expo's port-8081-silent-hang fix.)

set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs"
LOG_FILE="$SCRIPT_DIR/logs/devmesh_hooks.log"

log_line() {
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "$1"
    echo "$ts [setup.sh] $1" >> "$LOG_FILE" 2>/dev/null
}

log_line "=== DevMesh Setup started ==="
echo

# --- 0. Sanity checks -------------------------------------------------
if [ ! -d "backend" ]; then
    log_line "ERROR: backend/ not found. Run this script from the repo root."
    exit 1
fi

if [ ! -d ".git" ]; then
    log_line "WARNING: no .git found in $(pwd) — post-commit hook cannot be installed."
    echo
    echo "  ############################################################"
    echo "  #  WARNING: no .git found in $(pwd)"
    echo "  #  The post-commit hook CANNOT be installed, which means"
    echo "  #  NO commit will EVER trigger a review — silently, with no"
    echo "  #  further warning once setup finishes."
    echo "  #"
    echo "  #  Fix: run 'git init' (or clone the real repo) here, then"
    echo "  #  re-run this script."
    echo "  ############################################################"
    echo
    IS_GIT_REPO=0
else
    IS_GIT_REPO=1
fi

# --- 1. Install Python dependencies ------------------------------------
log_line "[1/4] Installing Python dependencies from backend/requirements.txt..."
if command -v pip3 &> /dev/null; then
    PIP_CMD=pip3
else
    PIP_CMD=pip
fi
if $PIP_CMD install -r backend/requirements.txt; then
    log_line "      Done."
else
    log_line "      WARNING: pip install failed. Continuing with hook install anyway."
    echo "      You may need to install deps manually, e.g.:"
    echo "        $PIP_CMD install -r backend/requirements.txt --break-system-packages"
    echo "      or from a virtualenv."
fi
echo

# --- 2. Install the post-commit hook -----------------------------------
if [ "$IS_GIT_REPO" -eq 1 ]; then
    log_line "[2/4] Installing post-commit git hook..."
    if [ ! -f "hooks/post-commit" ]; then
        log_line "      WARNING: hooks/post-commit not found — skipping hook install."
    else
        cp hooks/post-commit .git/hooks/post-commit
        chmod +x .git/hooks/post-commit
        log_line "      Installed to .git/hooks/post-commit (and made executable)."
    fi
else
    log_line "[2/4] SKIPPED — not a git repo. Run 'git init' and re-run setup.sh."
fi
echo

# --- 3. Start the webhook listener --------------------------------------
log_line "[3/4] Starting FastAPI webhook listener on port 8000..."
cd backend

if curl -s -o /dev/null -m 2 http://localhost:8000/health; then
    log_line "      Something is already listening on port 8000 — leaving it running."
else
    nohup python -m uvicorn webhook_server:app --host 0.0.0.0 --port 8000 > "$SCRIPT_DIR/logs/devmesh_webhook.log" 2>&1 &
    WEBHOOK_PID=$!
    sleep 1
    if curl -s -o /dev/null -m 2 http://localhost:8000/health; then
        log_line "      Started and confirmed healthy (PID $WEBHOOK_PID). Logs: $SCRIPT_DIR/logs/devmesh_webhook.log"
    else
        log_line "      WARNING: started (PID $WEBHOOK_PID) but health check did not respond within 1s — check $SCRIPT_DIR/logs/devmesh_webhook.log for a startup error."
    fi
fi

cd "$SCRIPT_DIR"
echo

# --- 4. Start the mobile app (Expo) --------------------------------------
log_line "[4/4] Starting Expo mobile dev server..."
if [ ! -d "mobile" ]; then
    log_line "      WARNING: mobile/ not found — skipping. Run 'npx expo start' manually."
else
    cd mobile

    if [ ! -d "node_modules" ]; then
        log_line "      node_modules not found — running npm install first (one-time)..."
        if ! npm install; then
            log_line "      WARNING: npm install failed. Fix that, then run 'npx expo start' manually from mobile/."
            cd "$SCRIPT_DIR"
            echo
            log_line "=== Setup complete (mobile step skipped) ==="
            exit 0
        fi
    fi

    nohup npx expo start --port 5678 > "$SCRIPT_DIR/logs/devmesh_expo.log" 2>&1 &
    EXPO_PID=$!
    sleep 3
    log_line "      Started on port 5678 (PID $EXPO_PID). QR code/dev server URL: $SCRIPT_DIR/logs/devmesh_expo.log"
    echo "      View it now with: cat \"$SCRIPT_DIR/logs/devmesh_expo.log\""

    cd "$SCRIPT_DIR"
fi
echo

# --- Done ----------------------------------------------------------------
log_line "=== Setup complete ==="
echo
echo "Next steps:"
echo "  - Make a commit (e.g. against a file in samples/) to trigger a review"
echo "    automatically via the post-commit hook."
echo "  - Or simulate a GitHub PR event:"
echo "      curl -X POST http://localhost:8000/webhook \\"
echo "        -H \"X-GitHub-Event: pull_request\" \\"
echo "        -H \"Content-Type: application/json\" \\"
echo "        -d '{\"action\": \"opened\", \"pull_request\": {\"number\": 1}}'"
echo "  - Check webhook listener health: curl http://localhost:8000/health"
echo "  - Scan the QR code to open the mobile app: cat \"$SCRIPT_DIR/logs/devmesh_expo.log\""
echo "  - Findings stream over ws://0.0.0.0:8765 to the mobile app (if running)."
echo "  - Full structured logs (Python side): $SCRIPT_DIR/backend/logs/devmesh.log"
echo "  - Hook/setup checkpoint logs (bash side): $LOG_FILE"
echo
