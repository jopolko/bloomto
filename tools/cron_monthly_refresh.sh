#!/usr/bin/env bash
#
# Monthly cron: catch newly-online restaurant websites + revalidate everything,
# then deploy. The web_verify_batch.py step has a tier-4 (no-website)
# recheck gate of 14d, so every monthly run picks up any entry whose website
# has come online since last month. The validator then jina-renders + judges
# anything new and writes its verdict; inject + rsync push the changes live.
#
# Install:
#   crontab -e
#   # 5:17 AM Toronto on the 1st of every month
#   17 5 1 * * /home/josh/nowservingto/tools/cron_monthly_refresh.sh
#
# WSL2 note: ensure cron is enabled at boot — `sudo service cron start` once,
# or use `wsl --shutdown` + WSL-startup hooks. If WSL isn't running at fire
# time the cron doesn't trigger.
#
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/tools/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/monthly-$(date +%Y%m%d).log"
exec >>"$LOG" 2>&1

LOCK="$ROOT/tools/.monthly.lock"
exec 9>"$LOCK"
flock -n 9 || { echo "[$(date -Is)] another monthly run in progress"; exit 0; }

log() { echo "[$(date -Is)] $*"; }

log "==== monthly refresh start ===="

PY="python3"
[[ -x "$ROOT/.venv/bin/python" ]] && PY="$ROOT/.venv/bin/python"

# 1. Fresh CSV pull
log "→ fresh CSV from CKAN"
if ! curl -sSf --max-time 120 -o /tmp/business_licences_alt.csv \
     'https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/municipal-licensing-and-standards-business-licences-and-permits/resource/54bddc5e-92d9-4102-89c1-43e82f8f4d2d/download/business-licences-data.csv'; then
    log "ERROR: CSV pull failed"; exit 1
fi
ROWS=$(wc -l </tmp/business_licences_alt.csv)
log "  $ROWS rows"
[[ "$ROWS" -lt 100000 ]] && { log "ERROR: truncated CSV"; exit 1; }

# 2. Cuisine classification (name-only Haiku) — picks up new licences
log "→ llm_classify_batch.py"
"$PY" -u tools/llm_classify_batch.py || log "WARN: classify failed (non-fatal)"

# 3. Web-verify — find websites for new entries + re-check tier-4 (no-website)
#    every 14d via the built-in tier gate, so every monthly run sweeps them.
log "→ llm_verify_batch.py (websites + operating status)"
"$PY" -u tools/llm_verify_batch.py || log "WARN: web_verify failed (non-fatal)"

# 4. Places enrichment + recovery pass (free name+address → Places metadata)
log "→ places enrichment"
"$PY" -u tools/places_enrich_socials.py || log "WARN: places_enrich_socials failed"
"$PY" -u tools/places_recover_cuisine.py || log "WARN: places_recover_cuisine failed"

# 5. Final cuisine recovery via website content
log "→ llm_recover_cuisine.py"
"$PY" -u tools/llm_recover_cuisine.py || log "WARN: recover_cuisine failed"

# 6. Validator — jina-render + Haiku judge on every operating entry. This is
#    where multi-location chains get caught and dead/aggregator websites get
#    stripped. Skips entries validated in last 24h; pass --force to redo all.
log "→ validate_entries_batch.py"
"$PY" -u tools/validate_entries_batch.py || log "WARN: validator failed (non-fatal)"

# 7. Geocode any new addresses (free Nominatim, ~1 req/sec)
log "→ geocode_addresses.py"
"$PY" -u tools/geocode_addresses.py || log "WARN: geocode failed"

# 8. Inject — merge all caches into data/corridors.json + index.html
log "→ inject_openings.py"
"$PY" tools/inject_openings.py || { log "ERROR: inject failed"; exit 1; }

# 9. Validate output JSON before deploy
DATA="$ROOT/data/corridors.json"
"$PY" -c "import json,sys; json.load(open(sys.argv[1]))" "$DATA" \
    || { log "ERROR: corridors.json invalid"; exit 1; }
TAGGED=$("$PY" -c "import json; d=json.load(open('$DATA')); print(d['newOpenings']['totalTagged365d'])")
log "  $TAGGED tagged 12mo openings"

# 10. Deploy to production VPS (rsync over SSH)
log "→ rsync to production VPS"
SSH_OPTS="-p 34522 -i /home/josh/.ssh/nowservingto_deploy -o StrictHostKeyChecking=no"
rsync -avz -e "ssh $SSH_OPTS" \
    "$ROOT/index.html" "$ROOT/sitemap.xml" \
    john@143.110.236.86:/var/www/html/nowservingto/ \
    || { log "ERROR: rsync index/sitemap failed"; exit 1; }
rsync -avz -e "ssh $SSH_OPTS" \
    "$ROOT/data/corridors.json" \
    john@143.110.236.86:/var/www/html/nowservingto/data/ \
    || { log "ERROR: rsync corridors.json failed"; exit 1; }

# 11. Commit + push
if [[ -n "$(git status --porcelain data/corridors.json index.html sitemap.xml 2>/dev/null)" ]]; then
    log "→ commit + push"
    source /var/secrets/nowservingto.env
    git add data/corridors.json index.html sitemap.xml
    git -c commit.gpgsign=false commit -m "Monthly refresh: $TAGGED tagged 12mo openings" || log "WARN: commit failed"
    REMOTE=$(git config remote.origin.url | sed -E 's#https?://(.*@)?github.com/##')
    git push "https://${GITHUB_TOKEN}@github.com/${REMOTE}" main || log "WARN: push failed"
fi

# Rotate logs (keep 6 months)
find "$LOG_DIR" -name 'monthly-*.log' -mtime +180 -delete 2>/dev/null || true

log "==== monthly refresh done ===="
