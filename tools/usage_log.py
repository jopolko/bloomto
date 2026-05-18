#!/usr/bin/env python3
"""
Central usage ledger. Every API-touching helper in tools/ calls log_usage()
to append one row per API call to tools/cache/usage_log.jsonl. tools/
aggregate_usage.py rolls it up into data/usage.json which the /usage page
on the site renders.

Designed to be cheap to call — single open/append per call, no batching,
no concurrency control. Append-only JSONL is safe under concurrent
appends on a single host (POSIX guarantees <PIPE_BUF atomic writes).

Pricing constants are in PRICE — edit there when a provider's rate
changes. Costs are estimates, not authoritative billing — the provider
dashboards (Google Cloud Console, Anthropic Console, etc.) remain the
source of truth. We track our OWN calls to give a per-day, per-feature
picture of where the spend goes.
"""
import json, os, time
from pathlib import Path

LEDGER_PATH = Path(__file__).resolve().parent / 'cache' / 'usage_log.jsonl'
LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)

# Per-call cost in USD. Single source of truth; edit here on price changes.
PRICE = {
    # Google Maps Platform
    'places.find_place':   0.017,
    'places.details':      0.025,
    'places.photo':        0.007,
    'streetview.image':    0.007,
    'streetview.metadata': 0.0,    # free, logged for visibility
    # Anthropic — per-token costs handled via cost_usd kwarg from caller
    'anthropic.haiku.batch.in':  0.40 / 1_000_000,   # $0.40 / 1M tokens
    'anthropic.haiku.batch.out': 2.00 / 1_000_000,   # $2.00 / 1M tokens
    'anthropic.haiku.sync.in':   0.80 / 1_000_000,
    'anthropic.haiku.sync.out':  4.00 / 1_000_000,
    'anthropic.web_search.batch': 0.005,    # $5 per 1k calls (batch)
    'anthropic.web_search.sync':  0.010,    # $10 per 1k calls (sync)
    # Jina Reader (keyed tier — rough estimate from Anthropic-ish token pricing)
    'jina.reader':         0.00002,  # ~$0.02 per 1k renders, conservative
    # X / Twitter
    'x.tweet':             0.010,
    'x.media_upload':      0.0,      # bundled with tweet create
}


def log_usage(sku, units=1, cost_usd=None, meta=None):
    """Append one ledger row. `sku` is the lookup key in PRICE (or a free-
    form string if not in PRICE). `units` defaults to 1 (one API call);
    pass `units=token_count` for per-token billing. `cost_usd` overrides
    the lookup if the cost is computed by the caller (token math). `meta`
    is an arbitrary dict — entry slug, batch_id, etc. — for after-the-fact
    drill-down."""
    if cost_usd is None:
        unit_cost = PRICE.get(sku)
        cost_usd = (unit_cost or 0.0) * units
    row = {
        't':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'sku':  sku,
        'u':    units,
        'cost': round(float(cost_usd), 6),
    }
    if meta: row['meta'] = meta
    try:
        with LEDGER_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, separators=(',', ':')) + '\n')
    except Exception:
        pass   # never let logging failure break a caller
