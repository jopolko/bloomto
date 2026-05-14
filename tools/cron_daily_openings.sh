#!/usr/bin/env bash
#
# Daily cron: refresh the "Now open" feed.
#
#   1. Pull fresh Toronto Open Data business licences CSV
#   2. Inject openings (uses LLM + Places caches; cuisine + websites where cached)
#   3. Classify any NEW openings not yet in LLM cache (Anthropic Batch API)
#   4. Re-inject so newly-tagged businesses surface
#   5. Look up websites for newly cuisine-tagged businesses (Haiku + web_search)
#   6. Re-inject one more time to merge in any new website data
#   7. Optionally rsync data/corridors.json to prod
#
# Safe for cron:
#   - flock against concurrent runs
#   - rotates its own logs to tools/logs/openings-*.log
#   - exits non-zero on hard failure so cron MAILTO catches it
#   - per-step failure is logged but doesn't abort downstream steps where possible
#
# Optional env (override at the cron line):
#   ROOTED_DIR    repo root (default: derived from this script)
#   WEB_ROOT      local prod dir for `cp` deploy (e.g. /var/www/html/nowservingto)
#   SKIP_LLM      "1" to skip the Haiku classification step
#   SKIP_WEBSITES "1" to skip the web_search website-lookup step
#
# Suggested cron line (every morning 5:17 AM Toronto):
#   17 5 * * *  WEB_ROOT=/var/www/html/nowservingto /var/www/html/nowservingto/tools/cron_daily_openings.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
ROOTED_DIR="${ROOTED_DIR:-$(cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd)}"
LOG_DIR="$ROOTED_DIR/tools/logs"
LOG_FILE="$LOG_DIR/openings-$(date +%Y%m%d).log"
LOCK_FILE="$ROOTED_DIR/tools/.openings.lock"

mkdir -p "$LOG_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -Is)] another openings refresh is in progress; exiting" >> "$LOG_FILE"
    exit 0
fi

log() { echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"; }

log "==== daily openings refresh start ===="

cd "$ROOTED_DIR"

# venv detection
if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    log "ERROR: no python available"; exit 1
fi
log "python=$PYTHON"

# Step 0: refresh the OSM chain set if older than 7 days (or missing). This is
# the authoritative source for auto-chain detection in inject_openings.py.
# Free (no API key), ~6s for the Overpass query. We refresh weekly because chain
# brand lists move slowly, and a stale set just means we miss a newly-tagged
# brand for a week (the manual CHAIN_DENYLIST is the backstop).
OSM_CACHE="$ROOTED_DIR/tools/cache/osm_chain_set.json"
if [[ ! -s "$OSM_CACHE" ]] || [[ $(find "$OSM_CACHE" -mtime +7 2>/dev/null | wc -l) -gt 0 ]]; then
    log "→ build_osm_chain_set.py (refresh authoritative chain list from OSM)"
    "$PYTHON" -u tools/build_osm_chain_set.py >> "$LOG_FILE" 2>&1 \
        || log "WARN: OSM chain refresh failed (non-fatal — using cached set)"
fi

# Step 1: fresh CSV pull from CKAN
log "→ pulling fresh business-licences CSV"
START=$SECONDS
CSV_URL="https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/municipal-licensing-and-standards-business-licences-and-permits/resource/54bddc5e-92d9-4102-89c1-43e82f8f4d2d/download/business-licences-data.csv"
if ! curl -sSf --max-time 120 -o /tmp/business_licences_alt.csv "$CSV_URL"; then
    log "ERROR: CSV download failed"; exit 1
fi
ROWS=$(wc -l < /tmp/business_licences_alt.csv)
log "  fetched $ROWS rows in $((SECONDS - START))s"
if [[ "$ROWS" -lt 100000 ]]; then
    log "ERROR: CSV looks truncated (rows=$ROWS); aborting"; exit 1
fi

# Step 2: initial inject (uses existing caches)
log "→ inject_openings.py (initial pass)"
if ! "$PYTHON" tools/inject_openings.py >> "$LOG_FILE" 2>&1; then
    log "ERROR: initial inject failed"; exit 1
fi

# Step 3: cuisine classification via Anthropic Message Batches (async, 50% off).
# Walks the CSV, picks up new entries + previous errors, submits one batch, polls
# until done. Exits cleanly with no spend if nothing is missing.
if [[ "${SKIP_LLM:-0}" != "1" ]]; then
    log "→ llm_classify_batch.py (batch / async / Haiku — 50% off)"
    if ! "$PYTHON" -u tools/llm_classify_batch.py >> "$LOG_FILE" 2>&1; then
        log "WARN: batch classification failed (non-fatal, will keep existing tags)"
    fi
    log "→ inject_openings.py (post-classification)"
    "$PYTHON" tools/inject_openings.py >> "$LOG_FILE" 2>&1 || log "WARN: re-inject failed"
else
    log "  SKIP_LLM=1 — skipping classification"
fi

# Step 4: web_search verification via Message Batches (operating? website? cuisine?).
# Walks the CSV, picks up entries needing first-time or tier-stale re-verification,
# submits one batch, polls until done.
if [[ "${SKIP_WEBSITES:-0}" != "1" ]]; then
    log "→ llm_verify_batch.py (batch / async / Haiku + web_search — 50% off)"
    if ! "$PYTHON" -u tools/llm_verify_batch.py >> "$LOG_FILE" 2>&1; then
        log "WARN: batch web verification failed (non-fatal)"
    fi
fi

# Step 5: probe every cached restaurant website for HTTP errors so we don't show
# dead links. $0 cost, ~20s for full sweep. Each URL re-probed every 14 days.
log "→ check_link_health.py (HEAD-probe cached websites)"
if ! "$PYTHON" -u tools/check_link_health.py >> "$LOG_FILE" 2>&1; then
    log "WARN: link health check failed (non-fatal)"
fi

# Step 5a: ask Google Places about every operating-but-uncategorized entry we
# haven't queried yet (no website OR a social website). Places frequently knows
# the real restaurant URL even when our verifier only found the IG account, so
# this populates places_cache for the downstream cuisine-recovery step to use.
# Order matters: this must run BEFORE llm_recover_cuisine so Places' website
# data is available. ~$0.017 × daily-delta = pennies.
log "→ places_enrich_socials.py (upgrade social-link entries via Places)"
if ! "$PYTHON" -u tools/places_enrich_socials.py >> "$LOG_FILE" 2>&1; then
    log "WARN: social-link Places enrichment failed (non-fatal)"
fi
log "→ places_recover_cuisine.py (Places lookup for entries with no/social website)"
if ! "$PYTHON" -u tools/places_recover_cuisine.py >> "$LOG_FILE" 2>&1; then
    log "WARN: Places coverage expansion failed (non-fatal)"
fi

# Step 5b: cuisine-recovery pass — for entries still without a cuisine, fetch
# the best available website (Places' own-site preferred over verify-cache's
# social URL; social used only as last resort) and classify via Haiku.
# Order: Places-first → verify non-social → social fallback (immigrant-run
# spots that live entirely on IG). ~$0.001 per recovery × ~10/day delta.
log "→ llm_recover_cuisine.py (Places-first website fetch + reclassify)"
if ! "$PYTHON" -u tools/llm_recover_cuisine.py >> "$LOG_FILE" 2>&1; then
    log "WARN: cuisine recovery failed (non-fatal — entries stay uncategorized)"
fi

# Step 5c: Layer 4 — for entries where the website fetch failed (SPA shells,
# Cloudflare-blocked, PDF-only menus), use Haiku + web_search via the Message
# Batches API to classify from Google's already-rendered/indexed view of the
# site. Batch is 50% off ($5/1K web_search vs $10/1K sync) AND has much higher
# per-org rate limits — sync would hit web_search throttling after ~80 calls.
# Uses GHDB-style operators (filetype:pdf, site:blogto.com, intitle:menu,
# quoted exact names, -aggregator exclusions) on the second search call.
# Cost: ~$0.01/attempt × daily delta. The sync variant (llm_search_recover_cuisine.py)
# is kept in the repo for manual / debugging use but not invoked by cron.
log "→ llm_search_recover_batch.py (Haiku web_search recovery — Layer 4, batch / 50% off)"
if ! "$PYTHON" -u tools/llm_search_recover_batch.py >> "$LOG_FILE" 2>&1; then
    log "WARN: batched search-based cuisine recovery failed (non-fatal)"
fi

# Step 5b: geocode addresses for entries missing lat/lng (powers the map view).
# Uses free Nominatim @ 1 req/sec; the daily delta is ~5-15 addresses so this
# adds ~10-20s per cron. Skips any address already geocoded.
log "→ geocode_addresses.py (Nominatim — free, 1 req/sec)"
if ! "$PYTHON" -u tools/geocode_addresses.py >> "$LOG_FILE" 2>&1; then
    log "WARN: geocoding failed (non-fatal — map will still work for already-geocoded entries)"
fi

# Step 6: final inject — merges verification + health-check results into corridors.json
log "→ inject_openings.py (final, post-verify + post-health-check)"
"$PYTHON" tools/inject_openings.py >> "$LOG_FILE" 2>&1 || log "WARN: final inject failed"

# Step 5: sanity-check + deploy
DATA="$ROOTED_DIR/data/corridors.json"
if [[ ! -s "$DATA" ]]; then
    log "ERROR: $DATA missing or empty"; exit 1
fi
if ! "$PYTHON" -c "import json,sys; json.load(open(sys.argv[1]))" "$DATA" >> "$LOG_FILE" 2>&1; then
    log "ERROR: $DATA failed JSON parse"; exit 1
fi
TAGGED=$("$PYTHON" -c "import json; d=json.load(open('$DATA')); print(d['newOpenings']['totalTagged365d'])")
log "  corridors.json OK · $TAGGED tagged 12mo openings"

if [[ -n "${WEB_ROOT:-}" ]]; then
    if [[ ! -d "$WEB_ROOT" ]]; then
        log "ERROR: WEB_ROOT=$WEB_ROOT does not exist"; exit 1
    fi
    DEST_DATA="$WEB_ROOT/data"
    mkdir -p "$DEST_DATA"
    TMP="$DEST_DATA/corridors.json.tmp.$$"
    cp -f "$DATA" "$TMP"
    chmod 644 "$TMP"
    mv -f "$TMP" "$DEST_DATA/corridors.json"
    log "  deployed corridors.json → $DEST_DATA"
fi

# Rotate logs (keep 30 days)
find "$LOG_DIR" -name 'openings-*.log' -mtime +30 -delete 2>/dev/null || true

log "==== daily openings refresh done ===="
exit 0
