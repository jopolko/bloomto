#!/usr/bin/env python3
"""
One-shot: run review_site() against every URL in url_health_cache.json that's
currently `ok=True` but hasn't been Haiku-reviewed yet. Marks `reviewed_at`
on every checked entry; flips `ok=False` for any that come back spam/off_topic.

Idempotent — re-running skips entries that already have a `reviewed_at`.

Cost: ~$0.0018/URL on sync Haiku 4.5 pricing. ~$1.26 for the initial ~700 URLs.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from check_link_health import review_site, HEALTH_CACHE, SKIP_PROBE_DOMAINS, ANTHROPIC_KEY

WORKERS = 12

def needs_review(url, entry):
    if not entry or not entry.get('ok'): return False
    if entry.get('reviewed_at'): return False
    if any(d in url.lower() for d in SKIP_PROBE_DOMAINS): return False
    return True

def main():
    if not ANTHROPIC_KEY:
        sys.exit("ANTHROPIC_API_KEY not loaded — /var/secrets/nowservingto.env missing or unreadable")
    cache = json.loads(HEALTH_CACHE.read_text())
    targets = [u for u, e in cache.items() if needs_review(u, e)]
    print(f"cache: {len(cache)} URLs total")
    print(f"to review: {len(targets)}  (skipping social platforms + already-reviewed)")
    if not targets:
        return

    now_iso = lambda: datetime.now(timezone.utc).isoformat()
    n_legit = n_spam = n_off = n_err = 0
    t0 = time.time()
    done = 0

    def work(u):
        try:
            return u, review_site(u)
        except Exception as e:
            return u, f"error: {type(e).__name__}: {str(e)[:80]}"

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(work, u) for u in targets]):
            u, verdict = fut.result()
            entry = cache[u]
            entry['reviewed_at'] = now_iso()
            if verdict is None:
                entry['review_verdict'] = 'legit'
                n_legit += 1
            elif verdict.startswith('error:'):
                # Network/parse error — leave ok untouched, but don't mark legit either.
                # No reviewed_at update so next run retries.
                del entry['reviewed_at']
                n_err += 1
            else:
                # spam: ... or off_topic: ...
                verdict_kind = verdict.split(':', 1)[0]
                entry['review_verdict'] = verdict_kind
                entry['ok'] = False
                entry['reason'] = verdict
                if verdict_kind == 'spam': n_spam += 1
                else: n_off += 1
            done += 1
            if done % 25 == 0 or done == len(targets):
                el = time.time() - t0
                rate = done / el if el > 0 else 0
                print(f"  [{done}/{len(targets)}] {el:.0f}s  ({rate:.1f}/s)  legit={n_legit} spam={n_spam} off_topic={n_off} err={n_err}")
            # Persist every 50 in case we get interrupted
            if done % 50 == 0:
                HEALTH_CACHE.write_text(json.dumps(cache, separators=(',', ':')))

    HEALTH_CACHE.write_text(json.dumps(cache, separators=(',', ':')))
    el = time.time() - t0
    print(f"\nDone in {el:.0f}s: legit={n_legit} spam={n_spam} off_topic={n_off} err={n_err}")

if __name__ == '__main__':
    main()
