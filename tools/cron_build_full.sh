#!/usr/bin/env bash
#
# Weekly cron wrapper for the full BloomTO rebuild.
#
# Runs the heavy ETL chain:
#   1. tools/build_neighborhoods.py  — ~5 min, NPP 2021 + canopy + permit roll
#   2. tools/build_parcels.py        — ~30-50 min, per-parcel computation
#   3. tools/build_parcels_top.py    — ~5 sec, projection to elite + broader
#
# Then optionally deploys all 4 wire files to prod via cp or rsync.
#
# Designed safe for cron:
#   - locks against concurrent runs (flock) AND against the daily signals job
#   - logs to a rotating file under tools/logs/
#   - atomic deploy via rsync (or cp + mv on same machine)
#   - exits non-zero on failure so cron's MAILTO catches it
#
# Required environment (override on the cron line if needed):
#   BLOOMTO_DIR    — repo root (default: directory containing this script's parent)
#   WEB_ROOT       — local destination dir for live site's data/ folder. cp/mv mode.
#   REMOTE_TARGET  — remote rsync target. Format: user@host:/path/to/data/
#                    (trailing slash matters). scp/rsync mode.
#   SSH_KEY        — optional path to SSH private key.
#   WORKERS        — multiprocessing pool size for build_parcels.py (default 4)
#
# Suggested crontab entry (Sun 2:17 AM Toronto, weekly):
#   17 2 * * 0  REMOTE_TARGET=jopolko@prod-host:/var/www/html/bloomto/data/  /home/josh/bloomto_work/tools/cron_build_full.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
BLOOMTO_DIR="${BLOOMTO_DIR:-$(cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd)}"
WORKERS="${WORKERS:-4}"

LOG_DIR="$BLOOMTO_DIR/tools/logs"
LOG_FILE="$LOG_DIR/full-$(date +%Y%m%d).log"
# Same lock as the signals script — they share the data/ output directory
# so we never let them race. The weekly job will skip if the daily is mid-run.
LOCK_FILE="$BLOOMTO_DIR/tools/.signals.lock"

mkdir -p "$LOG_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -Is)] another build is in progress; exiting" >> "$LOG_FILE"
    exit 0
fi

log() { echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"; }

log "==== full rebuild start ===="
log "BLOOMTO_DIR=$BLOOMTO_DIR  WORKERS=$WORKERS"
log "WEB_ROOT=${WEB_ROOT:-(unset)}  REMOTE_TARGET=${REMOTE_TARGET:-(unset)}"

cd "$BLOOMTO_DIR"

# ── venv detection ────────────────────────────────────────────────────────
if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    log "ERROR: no .venv/bin/python and no python3 on PATH"
    exit 1
fi
log "python=$PYTHON ($($PYTHON --version 2>&1))"

# ── Stage 1: neighborhoods ────────────────────────────────────────────────
log "→ build_neighborhoods.py"
START=$SECONDS
if ! "$PYTHON" tools/build_neighborhoods.py >> "$LOG_FILE" 2>&1; then
    log "ERROR: build_neighborhoods.py failed"
    exit 1
fi
log "  neighborhoods done in $((SECONDS - START))s"

# ── Stage 2: per-parcel ETL ──────────────────────────────────────────────
log "→ build_parcels.py --workers $WORKERS  (this is the long stretch — 30–50 min)"
START=$SECONDS
if ! "$PYTHON" tools/build_parcels.py --workers "$WORKERS" >> "$LOG_FILE" 2>&1; then
    log "ERROR: build_parcels.py failed"
    exit 1
fi
log "  build_parcels done in $((SECONDS - START))s"

# ── Stage 3: projection ──────────────────────────────────────────────────
log "→ build_parcels_top.py"
START=$SECONDS
if ! "$PYTHON" tools/build_parcels_top.py >> "$LOG_FILE" 2>&1; then
    log "ERROR: build_parcels_top.py failed"
    exit 1
fi
log "  build_parcels_top done in $((SECONDS - START))s"

# ── Stage 4: also refresh signals (so the deploy is consistent) ──────────
log "→ build_signals.py (consistency refresh)"
START=$SECONDS
if ! "$PYTHON" tools/build_signals.py >> "$LOG_FILE" 2>&1; then
    log "WARN: build_signals.py failed — non-fatal, will retry on daily cron"
fi
log "  build_signals done in $((SECONDS - START))s"

# ── Sanity check the 4 wire files ────────────────────────────────────────
DATA_DIR="$BLOOMTO_DIR/data"
WIRE_FILES=(
    "$DATA_DIR/parcels-top.json"
    "$DATA_DIR/parcels-broader.json"
    "$DATA_DIR/neighborhoods.json"
    "$DATA_DIR/signals.json"
)
for f in "${WIRE_FILES[@]}"; do
    if [[ ! -s "$f" ]]; then
        log "ERROR: $f missing or empty"
        exit 1
    fi
    if ! "$PYTHON" -c "import json,sys; json.load(open(sys.argv[1]))" "$f" >> "$LOG_FILE" 2>&1; then
        log "ERROR: $f failed JSON parse"
        exit 1
    fi
    SIZE_KB=$(( $(stat -c%s "$f" 2>/dev/null || stat -f%z "$f") / 1024 ))
    log "  $(basename "$f") OK · ${SIZE_KB} KB"
done

# ── Optional deploy: local cp ──────────────────────────────────────────────
if [[ -n "${WEB_ROOT:-}" ]]; then
    if [[ ! -d "$WEB_ROOT" ]]; then
        log "ERROR: WEB_ROOT=$WEB_ROOT does not exist"
        exit 1
    fi
    DEST_DATA="$WEB_ROOT/data"
    mkdir -p "$DEST_DATA"
    for f in "${WIRE_FILES[@]}"; do
        DEST="$DEST_DATA/$(basename "$f")"
        TMP="$DEST.tmp.$$"
        cp -f "$f" "$TMP"
        chmod 644 "$TMP"
        mv -f "$TMP" "$DEST"
    done
    log "local-deployed 4 wire files → $DEST_DATA"
fi

# ── Optional deploy: rsync to remote prod ─────────────────────────────────
if [[ -n "${REMOTE_TARGET:-}" ]]; then
    SSH_OPTS=()
    if [[ -n "${SSH_KEY:-}" ]]; then
        SSH_OPTS=(-e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    else
        SSH_OPTS=(-e "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    fi
    if rsync -az --chmod=F644 "${SSH_OPTS[@]}" \
            "${WIRE_FILES[@]}" "$REMOTE_TARGET" >> "$LOG_FILE" 2>&1; then
        log "remote-deployed 4 wire files → $REMOTE_TARGET"
    else
        log "ERROR: rsync to $REMOTE_TARGET failed"
        exit 1
    fi
fi

# ── Rotate logs ───────────────────────────────────────────────────────────
find "$LOG_DIR" -name 'full-*.log'    -mtime +30 -delete 2>/dev/null || true
find "$LOG_DIR" -name 'signals-*.log' -mtime +14 -delete 2>/dev/null || true

log "==== full rebuild done ===="
exit 0
