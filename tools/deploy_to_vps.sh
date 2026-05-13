#!/usr/bin/env bash
#
# One-shot deploy of NowServingTO to the VPS.
#
# Usage:
#   tools/deploy_to_vps.sh user@vps-host[:port]              # full deploy
#   tools/deploy_to_vps.sh --dry-run user@vps-host           # show what would happen
#   tools/deploy_to_vps.sh --skip-secrets user@vps-host      # don't scp /var/secrets/nowservingto.env
#
# What it does (in order):
#   1. Local cleanup: removes __pycache__, .signals/.openings lock files, stale logs
#   2. Rsync only the files needed for the openings cron (~1.2 MB) to the VPS web root.
#      Excludes legacy/, data/parcels.geojson, .venv, .git, .claude, big-data caches.
#      Includes the 4 small JSON caches (LLM cuisine + web verify + Places + URL health)
#      so first cron run is a cheap delta only.
#   3. scp /var/secrets/nowservingto.env to VPS:/var/secrets/nowservingto.env with mode 600
#      (skippable via --skip-secrets if you set it up manually)
#   4. Remote bootstrap: build a .venv on the VPS, pip-install requirements
#   5. Run the daily cron once manually as a smoke test, surface any failures
#   6. Print the crontab line for you to install (we don't touch crontab without asking)
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
LOCAL_ROOT="$(cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd)"
REMOTE_ROOT='/var/www/html/rootedto'
SECRETS_LOCAL='/var/secrets/nowservingto.env'
SECRETS_REMOTE='/var/secrets/nowservingto.env'

DRY_RUN=""
SKIP_SECRETS=0
TARGET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN="--dry-run"; shift;;
        --skip-secrets) SKIP_SECRETS=1; shift;;
        -h|--help) sed -n '3,16p' "$0"; exit 0;;
        *) TARGET="$1"; shift;;
    esac
done

if [[ -z "$TARGET" ]]; then
    echo "usage: $0 [--dry-run] [--skip-secrets] user@host[:port]" >&2
    exit 1
fi

# Parse user@host[:port]
SSH_TARGET="${TARGET%:*}"
SSH_PORT=22
if [[ "$TARGET" == *:* ]]; then
    SSH_PORT="${TARGET##*:}"
fi
RSYNC_SSH="ssh -p $SSH_PORT -o StrictHostKeyChecking=accept-new"

echo "==== NowServingTO deploy ===="
echo "  local:      $LOCAL_ROOT"
echo "  remote:     $SSH_TARGET:$REMOTE_ROOT (port $SSH_PORT)"
echo "  dry-run:    ${DRY_RUN:-no}"
echo "  scp secrets:$([[ $SKIP_SECRETS == 1 ]] && echo " skipped" || echo " yes")"
echo

# ---- 1. Local cleanup (always safe; just removes generated/temp stuff) ----
echo "→ local cleanup"
if [[ -z "$DRY_RUN" ]]; then
    find "$LOCAL_ROOT/tools" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    rm -f "$LOCAL_ROOT/tools/.signals.lock" "$LOCAL_ROOT/tools/.openings.lock" 2>/dev/null || true
fi
echo "   (pruned __pycache__ and stale lock files)"

# ---- 2. Rsync ----
echo
echo "→ rsync to VPS"
rsync -avz $DRY_RUN --human-readable --info=progress2 \
    -e "$RSYNC_SSH" \
    --exclude='.git/' \
    --exclude='.gitignore' \
    --exclude='.venv/' \
    --exclude='.claude/' \
    --exclude='.claude.backup*' \
    --exclude='legacy/' \
    --exclude='data/parcels.geojson' \
    --exclude='data/ckan_survey*.md' \
    --exclude='toronto_datasets.txt' \
    --exclude='tools/__pycache__/' \
    --exclude='tools/logs/' \
    --exclude='tools/.signals.lock' \
    --exclude='tools/.openings.lock' \
    --exclude='tools/cache/*.geojson' \
    --exclude='tools/cache/*.gdb' \
    --exclude='tools/cache/*.zip' \
    --exclude='tools/cache/*.csv' \
    --exclude='tools/cache/coa_active.json' \
    --exclude='tools/cache/demo_permits.json' \
    --exclude='tools/cache/development_applications.json' \
    --exclude='tools/cache/osm_*.json' \
    --exclude='tools/cache/preliminary_zoning_reviews.json' \
    --exclude='tools/cache/property_violations.json' \
    "$LOCAL_ROOT/" "$SSH_TARGET:$REMOTE_ROOT/"

# ---- 3. Secrets file ----
if [[ -z "$DRY_RUN" && $SKIP_SECRETS == 0 ]]; then
    echo
    echo "→ secrets file"
    if [[ ! -f "$SECRETS_LOCAL" ]]; then
        echo "   WARN: $SECRETS_LOCAL doesn't exist locally — skipping"
    else
        # scp to ~ first, then sudo-move into place with proper perms
        scp -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$SECRETS_LOCAL" "$SSH_TARGET:nowservingto.env.tmp"
        ssh -p "$SSH_PORT" "$SSH_TARGET" "
            sudo mkdir -p /var/secrets
            sudo mv ~/nowservingto.env.tmp $SECRETS_REMOTE
            sudo chown root:root $SECRETS_REMOTE
            sudo chmod 600 $SECRETS_REMOTE
            ls -la $SECRETS_REMOTE
        " || echo "   WARN: secrets move failed — you may need to do it manually with sudo"
    fi
fi

# ---- 4. Remote bootstrap ----
if [[ -z "$DRY_RUN" ]]; then
    echo
    echo "→ remote bootstrap (venv + deps)"
    ssh -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$SSH_TARGET" bash -se <<EOF
set -e
cd $REMOTE_ROOT
if [[ ! -d .venv ]]; then
    echo "  creating venv at \$(pwd)/.venv"
    python3 -m venv .venv
fi
echo "  installing requirements"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r tools/requirements.txt 2>&1 | tail -5 || true
echo "  python:    \$(.venv/bin/python --version)"
echo "  ROOT_DIR:  \$(pwd)"
echo "  files:     \$(ls -la index.html data/corridors.json 2>&1 | head -3)"
chmod +x tools/*.sh
EOF
fi

# ---- 5. Smoke test ----
if [[ -z "$DRY_RUN" ]]; then
    echo
    echo "→ smoke test (run the daily cron once to verify everything works)"
    echo "   reading the first 40 lines of its log…"
    ssh -p "$SSH_PORT" "$SSH_TARGET" "cd $REMOTE_ROOT && tools/cron_daily_openings.sh 2>&1 | head -40" \
        || echo "   WARN: smoke test reported errors — check tools/logs/openings-*.log on the VPS"
fi

# ---- 6. Crontab instructions ----
echo
echo "==== Deploy complete ===="
echo
echo "To install the daily cron on the VPS, run:"
echo "   ssh -p $SSH_PORT $SSH_TARGET 'crontab -l 2>/dev/null; echo \"17 5 * * * $REMOTE_ROOT/tools/cron_daily_openings.sh\"' | sort -u | ssh -p $SSH_PORT $SSH_TARGET 'crontab -'"
echo
echo "Or add the line manually via:  ssh -p $SSH_PORT $SSH_TARGET 'crontab -e'"
echo "   17 5 * * * $REMOTE_ROOT/tools/cron_daily_openings.sh"
echo
echo "Verify with:  ssh -p $SSH_PORT $SSH_TARGET 'crontab -l'"
