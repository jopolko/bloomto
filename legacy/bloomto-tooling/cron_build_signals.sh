#!/usr/bin/env bash
#
# Nightly cron wrapper for `tools/build_signals.py`.
#
# Pulls the three CKAN-fresh feeds (severance / demo permits / violations),
# address-joins them to the existing parcels-top.json + parcels-broader.json,
# writes a fresh data/signals.json, and (optionally) deploys the file into
# the live web root.
#
# Designed to be safe to run as cron:
#   - locks against concurrent runs (flock)
#   - never re-runs the heavy ETL — only the ~30s overlay
#   - logs to a rotating file under tools/logs/
#   - atomic deploy via cp + chmod (no half-copies for visitors mid-write)
#   - exits non-zero on failure so cron's MAILTO catches it
#
# Required environment (override on the cron line if needed):
#   BLOOMTO_DIR    — repo root (default: directory containing this script's parent)
#   WEB_ROOT       — local destination dir for the live site's data/ folder.
#                    If set, signals.json is cp'd to WEB_ROOT/data/signals.json.
#                    Use when this script runs on the same machine as Apache.
#   REMOTE_TARGET  — remote rsync target for the live site's data/ folder.
#                    Format: user@host:/path/to/data/  (trailing slash matters)
#                    If set, signals.json is scp'd via rsync (atomic via
#                    --partial + temp-name). Requires passwordless SSH key
#                    set up between this box and the prod host.
#                    Use when this script runs on a separate dev box.
#   SSH_KEY        — optional path to a specific SSH private key.
#                    Defaults to ssh-agent / ~/.ssh/id_rsa default.
#
# Suggested crontab entries on the dev box:
#   # Nightly (3:17 AM Toronto): refresh signals.json + push to prod
#   17 3 * * *  REMOTE_TARGET=jopolko@prod-host:/var/www/html/bloomto/data/  /home/josh/bloomto_work/tools/cron_build_signals.sh
#   # Weekly (Sun 2:17 AM Toronto): full ETL rebuild + push all 4 data files
#   17 2 * * 0  REMOTE_TARGET=jopolko@prod-host:/var/www/html/bloomto/data/  /home/josh/bloomto_work/tools/cron_build_full.sh

set -euo pipefail

# ── Resolve paths (cron strips $PWD, so derive everything from $0) ─────────
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
BLOOMTO_DIR="${BLOOMTO_DIR:-$(cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd)}"

LOG_DIR="$BLOOMTO_DIR/tools/logs"
LOG_FILE="$LOG_DIR/signals-$(date +%Y%m%d).log"
LOCK_FILE="$BLOOMTO_DIR/tools/.signals.lock"

mkdir -p "$LOG_DIR"

# ── Lock against concurrent runs ───────────────────────────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -Is)] another signals run is in progress; exiting" >> "$LOG_FILE"
    exit 0
fi

log() { echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"; }

log "==== signals refresh start ===="
log "BLOOMTO_DIR=$BLOOMTO_DIR"
log "WEB_ROOT=${WEB_ROOT:-(unset — writing to $BLOOMTO_DIR/data only)}"

cd "$BLOOMTO_DIR"

# ── venv detection (prefer .venv, fall back to system python3) ─────────────
if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    log "ERROR: no .venv/bin/python and no python3 on PATH"
    exit 1
fi
log "python=$PYTHON ($($PYTHON --version 2>&1))"

# ── Run the build ──────────────────────────────────────────────────────────
log "running tools/build_signals.py …"
if ! "$PYTHON" tools/build_signals.py >> "$LOG_FILE" 2>&1; then
    log "ERROR: build_signals.py failed (see log above)"
    exit 1
fi

# Sanity check: signals.json must exist and parse cleanly
SIGNALS_PATH="$BLOOMTO_DIR/data/signals.json"
if [[ ! -s "$SIGNALS_PATH" ]]; then
    log "ERROR: $SIGNALS_PATH missing or empty"
    exit 1
fi
if ! "$PYTHON" -c "import json,sys; json.load(open(sys.argv[1]))" "$SIGNALS_PATH" >> "$LOG_FILE" 2>&1; then
    log "ERROR: signals.json failed JSON parse"
    exit 1
fi
SIZE_KB=$(( $(stat -c%s "$SIGNALS_PATH" 2>/dev/null || stat -f%z "$SIGNALS_PATH") / 1024 ))
log "signals.json OK · ${SIZE_KB} KB"

# ── Optional deploy: local cp (same-machine prod) ──────────────────────────
if [[ -n "${WEB_ROOT:-}" ]]; then
    if [[ ! -d "$WEB_ROOT" ]]; then
        log "ERROR: WEB_ROOT=$WEB_ROOT does not exist"
        exit 1
    fi
    DEST_DATA="$WEB_ROOT/data"
    mkdir -p "$DEST_DATA"
    DEST="$DEST_DATA/signals.json"
    TMP="$DEST.tmp.$$"
    cp -f "$SIGNALS_PATH" "$TMP"
    chmod 644 "$TMP"
    mv -f "$TMP" "$DEST"   # atomic on same filesystem
    log "local-deployed → $DEST"
fi

# ── Optional deploy: rsync to remote prod host ─────────────────────────────
if [[ -n "${REMOTE_TARGET:-}" ]]; then
    SSH_OPTS=()
    if [[ -n "${SSH_KEY:-}" ]]; then
        SSH_OPTS=(-e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    else
        SSH_OPTS=(-e "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    fi
    # rsync --partial + .signals.json.NNN temp name = atomic from the
    # browser's perspective. --inplace is intentionally omitted; the default
    # rename-on-complete is what gives us atomicity over Apache.
    if rsync -az --chmod=F644 "${SSH_OPTS[@]}" \
            "$SIGNALS_PATH" "$REMOTE_TARGET" >> "$LOG_FILE" 2>&1; then
        log "remote-deployed → $REMOTE_TARGET"
    else
        log "ERROR: rsync to $REMOTE_TARGET failed"
        exit 1
    fi
fi

# ── Rotate logs older than 14 days ─────────────────────────────────────────
find "$LOG_DIR" -name 'signals-*.log' -mtime +14 -delete 2>/dev/null || true

log "==== signals refresh done ===="
exit 0
