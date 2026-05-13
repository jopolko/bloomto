#!/usr/bin/env python3
"""
Probe every cached restaurant website URL (from Places + web-verify caches),
record HTTP status, and cache the result. Pages that 4xx/5xx or fail to load
are flagged so inject_openings.py drops the broken `website` field (and the
mapsUrl / no-link fallback takes over).

Cache: tools/cache/url_health_cache.json
   { "<url>": {"status": int|None, "checked_at": iso, "ok": bool, "reason": "..."} }

Re-checks any URL whose last check is older than CHECK_DAYS or that was
previously not OK. Stable OK results are skipped to keep this fast.
"""
import os, sys, json, time, socket
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
PLACES_CACHE = ROOT / 'tools' / 'cache' / 'places_cache.json'
WEB_CACHE = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
HEALTH_CACHE = ROOT / 'tools' / 'cache' / 'url_health_cache.json'

CHECK_DAYS = 14
TIMEOUT_SEC = 6
WORKERS = 12
UA = 'Mozilla/5.0 (compatible; rootedto-healthcheck/1.0)'

def collect_urls():
    urls = set()
    if PLACES_CACHE.exists():
        for v in json.loads(PLACES_CACHE.read_text()).values():
            if v.get('status') == 'ok' and v.get('website'):
                urls.add(v['website'])
    if WEB_CACHE.exists():
        for v in json.loads(WEB_CACHE.read_text()).values():
            if v.get('status') == 'ok' and v.get('website'):
                urls.add(v['website'])
    return urls

def needs_check(entry):
    if not entry: return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry['checked_at'])).days
    except Exception:
        return True
    # Re-check broken URLs more aggressively too — maybe they came back
    if not entry.get('ok'): return age >= 3
    return age >= CHECK_DAYS

def probe(url):
    """Try HEAD first; some servers refuse, retry GET with range. Treat 200/3xx as OK."""
    for method in ('HEAD', 'GET'):
        try:
            req = Request(url, headers={'User-Agent': UA, 'Range': 'bytes=0-0'}, method=method)
            with urlopen(req, timeout=TIMEOUT_SEC) as r:
                code = r.status
                return {'status': code, 'ok': 200 <= code < 400, 'reason': r.reason}
        except HTTPError as e:
            # Some sites 403 HEAD but 200 GET; we already retry GET
            if method == 'HEAD': continue
            return {'status': e.code, 'ok': False, 'reason': f'HTTP {e.code}'}
        except URLError as e:
            if method == 'HEAD': continue
            return {'status': None, 'ok': False, 'reason': f'URLError: {str(e.reason)[:80]}'}
        except (socket.timeout, TimeoutError):
            if method == 'HEAD': continue
            return {'status': None, 'ok': False, 'reason': 'timeout'}
        except Exception as e:
            if method == 'HEAD': continue
            return {'status': None, 'ok': False, 'reason': f'{type(e).__name__}: {str(e)[:80]}'}
    return {'status': None, 'ok': False, 'reason': 'unknown'}

def main():
    cache = json.loads(HEALTH_CACHE.read_text()) if HEALTH_CACHE.exists() else {}
    urls = collect_urls()
    targets = [u for u in urls if needs_check(cache.get(u))]
    print(f"total URLs across caches: {len(urls)}  to probe: {len(targets)}")
    if not targets:
        return

    now_iso = lambda: datetime.now(timezone.utc).isoformat()
    n_ok = n_bad = 0
    t0 = time.time()
    done = 0

    def work(u):
        r = probe(u)
        r['checked_at'] = now_iso()
        return u, r

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(work, u) for u in targets]):
            try: u, r = fut.result()
            except Exception as e: print(f'  worker err: {e}'); continue
            cache[u] = r
            done += 1
            if r['ok']: n_ok += 1
            else: n_bad += 1
            if done % 50 == 0 or done == len(targets):
                HEALTH_CACHE.write_text(json.dumps(cache, separators=(',', ':')))
                print(f"  [{done:>4}/{len(targets)}]  ok={n_ok} bad={n_bad}  ({time.time()-t0:.0f}s)")

    HEALTH_CACHE.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"\ndone: ok={n_ok}  bad={n_bad}  cache_size={len(cache)}")
    if n_bad:
        print("\nbroken URLs (sample first 15):")
        bad_items = sorted([(u, c) for u, c in cache.items() if not c.get('ok')], key=lambda x: x[1].get('checked_at',''), reverse=True)
        for u, c in bad_items[:15]:
            print(f"  [{c.get('status','-')}]  {c.get('reason','')[:50]:50s}  {u[:80]}")

if __name__ == '__main__':
    main()
