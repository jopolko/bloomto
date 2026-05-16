#!/usr/bin/env python3
"""
Post new restaurant openings to X (@nowservingto), one tweet per cron pass.

Reads data/corridors.json, finds the freshest entry whose slug isn't yet in
tools/cache/x_posted.json, builds a tweet, and POSTs it to
api.x.com/2/tweets via OAuth1.0a user-context auth.

Cadence: one tweet per invocation. cron_daily_openings.sh runs once per day,
so default tempo is one tweet/day even when there's a backlog. Override with
--max N to post up to N this run, --since-days N to widen the freshness window.

Reads OAuth creds from /var/secrets/nowservingto.env:
  X_API_KEY            (Consumer Key)
  X_API_SECRET         (Consumer Secret)
  X_ACCESS_TOKEN       (Access Token for @nowservingto)
  X_ACCESS_TOKEN_SECRET

Per-post API cost is $0.010 (X 'Content: Create' billing line). At 1/day the
monthly ceiling is ~$0.30; at 10/day cap it's ~$3.

Stdlib only. No external deps.
"""
import os, sys, json, time, base64, hmac, hashlib, secrets, argparse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import CUISINE_LABEL

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / 'data' / 'corridors.json'
POSTED_PATH = ROOT / 'tools' / 'cache' / 'x_posted.json'
SECRETS_PATH = Path('/var/secrets/nowservingto.env')
SITE_BASE = 'https://nowservingto.com'

# Toronto cuisine-hashtag convention: <Label>TO with no space. A few keys
# don't render cleanly that way, so override; everything else is computed
# from the human-readable label.
CUISINE_HASHTAG_OVERRIDE = {
    'middle_east': 'MiddleEastTO',
    'south_asian': 'SouthAsianTO',
    'african_horn': 'EastAfricanTO',
    'african_west': 'WestAfricanTO',
    'eastern_eu': 'EasternEuTO',
    'irish_uk': 'IrishUKTO',
    'jewish_deli': 'JewishDeliTO',
}


def _load_secrets():
    out = {}
    for line in SECRETS_PATH.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, _, v = line.partition('=')
            out[k.strip()] = v.strip()
    missing = [k for k in ('X_API_KEY','X_API_SECRET','X_ACCESS_TOKEN','X_ACCESS_TOKEN_SECRET') if k not in out]
    if missing:
        sys.exit(f"missing in {SECRETS_PATH}: {missing}")
    return out


def _pct(s):
    """OAuth 1.0a percent-encoding — RFC 3986 unreserved chars only."""
    return quote(str(s), safe='-._~')


def _oauth1_sign(method, url, oauth_params, body_params, consumer_secret, token_secret):
    """RFC 5849 §3.4 — HMAC-SHA1 signature over (method, base URL, sorted params)."""
    all_params = sorted((_pct(k), _pct(v)) for k, v in (list(oauth_params.items()) + list(body_params.items())))
    param_str = '&'.join(f'{k}={v}' for k, v in all_params)
    base = '&'.join([method.upper(), _pct(url), _pct(param_str)])
    signing_key = f'{_pct(consumer_secret)}&{_pct(token_secret)}'
    digest = hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def post_tweet(text, creds):
    """POST to api.x.com/2/tweets with OAuth1.0a user-context."""
    url = 'https://api.x.com/2/tweets'
    oauth = {
        'oauth_consumer_key': creds['X_API_KEY'],
        'oauth_nonce': secrets.token_hex(16),
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': str(int(time.time())),
        'oauth_token': creds['X_ACCESS_TOKEN'],
        'oauth_version': '1.0',
    }
    # Body params are NOT included in the v2 JSON-body sig (signature is over
    # OAuth params only when Content-Type is application/json).
    oauth['oauth_signature'] = _oauth1_sign(
        'POST', url, oauth, {},
        creds['X_API_SECRET'], creds['X_ACCESS_TOKEN_SECRET'],
    )
    auth_header = 'OAuth ' + ', '.join(
        f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items())
    )
    body = json.dumps({'text': text}).encode('utf-8')
    req = Request(url, data=body, method='POST', headers={
        'Authorization': auth_header,
        'Content-Type': 'application/json',
        'User-Agent': 'nowservingto-bot/1.0 (+https://nowservingto.com)',
    })
    try:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except HTTPError as e:
        raise RuntimeError(f'HTTP {e.code}: {e.read().decode(errors="replace")[:500]}')


def cuisine_hashtag(key, label):
    if key in CUISINE_HASHTAG_OVERRIDE:
        return CUISINE_HASHTAG_OVERRIDE[key]
    # Strip non-alphanumerics from the human label; everything else (Italian,
    # Korean, Sri Lankan, Cape Verdean) ends up as ItalianTO, KoreanTO,
    # SriLankanTO, CapeVerdeanTO.
    import re as _re
    return _re.sub(r'[^A-Za-z0-9]', '', label) + 'TO'


def _licensed_line(days):
    """Temporal hook for the tweet's lead line. Mirrors the site's _ago()
    language but capitalized for tweet display."""
    if days is None: return '🆕 Newly licensed'
    if days <= 1: return '🆕 Licensed today'
    if days <= 7: return f'🆕 Licensed {days}d ago'
    if days <= 30: return f'🆕 Licensed {days}d ago'
    if days <= 60: return f'🆕 Licensed {days // 7}w ago'
    return f'🆕 Licensed {days // 30}mo ago'


def build_tweet(entry):
    name = entry['operatingName']
    keys = entry.get('cuisines') or ([entry['cuisine']] if entry.get('cuisine') else [])
    primary_key = keys[0] if keys else ''
    primary_lbl = CUISINE_LABEL.get(primary_key, primary_key.title()) if primary_key else ''
    addr = entry.get('address') or ''
    district = entry.get('district') or ''
    socials = entry.get('socials') or {}
    handle_at = socials.get('x')   # only true @-mention if X handle is known
    handle_ig = socials.get('instagram') if not handle_at else None
    listing_url = f"{SITE_BASE}/r/{entry['slug']}"
    licensed_lead = _licensed_line(entry.get('daysOpen'))

    name_line = f"{name} · {primary_lbl}" if primary_lbl else name
    lines = [licensed_lead, name_line]
    addr_line = addr
    if district:
        addr_line = f"{addr_line} · {district}" if addr_line else district
    if addr_line:
        lines.append(addr_line)
    if handle_at:
        lines.append(f"@{handle_at}")
    elif handle_ig:
        lines.append(f"📷 instagram.com/{handle_ig}")
    lines.append(listing_url)
    hashtag = cuisine_hashtag(primary_key, primary_lbl) if primary_lbl else ''
    tags = '#Toronto #TOEats' + (f' #{hashtag}' if hashtag else '')
    lines.append(tags)
    text = '\n'.join(lines)
    # X 280-char limit — trim address/handle lines first, keep the temporal
    # hook + name + URL + hashtags as the irreducible core.
    if len(text) > 280:
        keep = [licensed_lead, name_line, listing_url, tags]
        if handle_at: keep.insert(-2, f"@{handle_at}")
        text = '\n'.join(keep)
        if len(text) > 280:
            text = text[:279] + '…'
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max', type=int, default=1, help='max tweets to post this run (default 1)')
    ap.add_argument('--since-days', type=int, default=14, help='only consider entries opened in last N days')
    ap.add_argument('--dry-run', action='store_true', help='print the tweet, do not POST')
    args = ap.parse_args()

    corr = json.loads(DATA_PATH.read_text())
    posted = json.loads(POSTED_PATH.read_text()) if POSTED_PATH.exists() else {}

    candidates = []
    for e in corr['newOpenings']['recent']:
        slug = e.get('slug')
        if not slug or slug in posted: continue
        if e.get('daysOpen', 999) > args.since_days: continue
        candidates.append(e)
    # Freshest first.
    candidates.sort(key=lambda e: e.get('issuedDate', ''), reverse=True)

    if not candidates:
        print('no new openings to post'); return

    creds = None if args.dry_run else _load_secrets()
    n_posted = 0
    for e in candidates[:args.max]:
        text = build_tweet(e)
        if args.dry_run:
            print('--- DRY RUN ---')
            print(text)
            print('---')
            continue
        print(f"posting: {e['slug']} ({len(text)} chars)")
        try:
            result = post_tweet(text, creds)
            tweet_id = result.get('data', {}).get('id')
            posted[e['slug']] = {
                'tweet_id': tweet_id,
                'posted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }
            POSTED_PATH.parent.mkdir(parents=True, exist_ok=True)
            POSTED_PATH.write_text(json.dumps(posted, indent=2, sort_keys=True))
            print(f"  → tweet_id={tweet_id}  https://x.com/nowservingto/status/{tweet_id}")
            n_posted += 1
        except Exception as ex:
            print(f"  FAIL: {ex}")
            # Don't break — try the next candidate.
    print(f"done. {n_posted} posted.")


if __name__ == '__main__':
    main()
