#!/usr/bin/env python3
"""
Weekly geographic digest poster for @JoshuaOpolko.

Every Sunday rotates through Toronto's 6 districts (Downtown, East Toronto,
Etobicoke, North York, Scarborough, West Toronto) and posts a thread of
~4 newly licensed restaurants from that district. The hook is "near me"
geographic discovery — better fit for the X audience than cuisine-niche
posts (the cuisine niche is served by Google search hitting the per-cuisine
SEO landing pages).

Thread shape:
  Lead tweet:  "🆕 4 newly licensed East Toronto restaurants this week
                — thread 🧵\n\n#Toronto #TOEats #EastTorontoTO"
  Replies:     same per-listing format as the daily @nowservingto bot —
                each reply links to /r/<slug> and X auto-cards the
                listing's og:image (Places photo).

Reads OAuth creds from /var/secrets/nowservingto.env under the X_JOSH_*
prefix (separate from @nowservingto's X_* creds, so the daily bot keeps
running until those credits are burned through):
  X_JOSH_API_KEY            (Consumer Key for @JoshuaOpolko's app)
  X_JOSH_API_SECRET         (Consumer Secret)
  X_JOSH_ACCESS_TOKEN       (Access Token for @JoshuaOpolko)
  X_JOSH_ACCESS_TOKEN_SECRET

State:
  tools/cache/x_weekly_state.json   — rotation pointer (last district idx)
  tools/cache/x_weekly_posted.json  — slugs already posted via this thread

Stdlib only.
"""
import os, sys, json, time, base64, hmac, hashlib, secrets, argparse, re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import CUISINE_LABEL

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH        = ROOT / 'data'  / 'corridors.json'
STATE_PATH       = ROOT / 'tools' / 'cache' / 'x_weekly_state.json'
POSTED_PATH      = ROOT / 'tools' / 'cache' / 'x_weekly_posted.json'
SECRETS_PATH     = Path('/var/secrets/nowservingto.env')
SITE_BASE        = 'https://nowservingto.com'

# Fixed rotation order — every district gets one slot in a 6-week cycle.
# Order is alphabetical except Downtown gets the lead slot (highest-traffic
# district most weeks, so we start each cycle with the strongest material).
DISTRICTS = [
    'Downtown',
    'East Toronto',
    'West Toronto',
    'North York',
    'Scarborough',
    'Etobicoke',
]

def _load_secrets():
    out = {}
    for line in SECRETS_PATH.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, _, v = line.partition('=')
            out[k.strip()] = v.strip()
    needed = ('X_JOSH_API_KEY', 'X_JOSH_API_SECRET',
              'X_JOSH_ACCESS_TOKEN', 'X_JOSH_ACCESS_TOKEN_SECRET')
    missing = [k for k in needed if k not in out]
    if missing:
        sys.exit(f"missing in {SECRETS_PATH}: {missing}\n"
                 "Add these from developer.x.com (the new @JoshuaOpolko app's "
                 "Consumer Keys + Access Tokens). The daily @nowservingto bot's "
                 "X_* creds are unaffected.")
    return {
        'api_key':    out['X_JOSH_API_KEY'],
        'api_secret': out['X_JOSH_API_SECRET'],
        'token':      out['X_JOSH_ACCESS_TOKEN'],
        'token_secret': out['X_JOSH_ACCESS_TOKEN_SECRET'],
    }


def _pct(s):
    return quote(str(s), safe='-._~')


def _oauth1_sign(method, url, oauth_params, body_params, consumer_secret, token_secret):
    all_params = sorted(
        (_pct(k), _pct(v))
        for k, v in (list(oauth_params.items()) + list(body_params.items()))
    )
    param_str = '&'.join(f'{k}={v}' for k, v in all_params)
    base = '&'.join([method.upper(), _pct(url), _pct(param_str)])
    signing_key = f'{_pct(consumer_secret)}&{_pct(token_secret)}'
    digest = hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def post_tweet(text, creds, reply_to=None):
    """POST to api.x.com/2/tweets. If reply_to is set, posts as a thread reply."""
    url = 'https://api.x.com/2/tweets'
    oauth = {
        'oauth_consumer_key': creds['api_key'],
        'oauth_nonce':        secrets.token_hex(16),
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp':    str(int(time.time())),
        'oauth_token':        creds['token'],
        'oauth_version':      '1.0',
    }
    oauth['oauth_signature'] = _oauth1_sign(
        'POST', url, oauth, {}, creds['api_secret'], creds['token_secret'],
    )
    auth_header = 'OAuth ' + ', '.join(
        f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items())
    )
    body_obj = {'text': text}
    if reply_to:
        body_obj['reply'] = {'in_reply_to_tweet_id': str(reply_to)}
    body = json.dumps(body_obj).encode('utf-8')
    req = Request(url, data=body, method='POST', headers={
        'Authorization': auth_header,
        'Content-Type':  'application/json',
        'User-Agent':    'nowservingto-weekly/1.0 (+https://nowservingto.com)',
    })
    try:
        with urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
        try:
            from usage_log import log_usage
            log_usage('x.tweet', meta={'tweet_id': result.get('data', {}).get('id'),
                                       'script': 'weekly_digest'})
        except Exception: pass
        return result
    except HTTPError as e:
        raise RuntimeError(f'HTTP {e.code}: {e.read().decode(errors="replace")[:500]}')


def _licensed_line(days):
    if days is None or days <= 1: return 'Licensed today'
    if days <= 30: return f'Licensed {days}d ago'
    if days <= 60: return f'Licensed {days // 7}w ago'
    return f'Licensed {days // 30}mo ago'


def build_lead_tweet(district, count):
    return (f"🆕 {count} newly licensed {district} restaurants this week "
            f"— thread 🧵\n\n#Toronto #TOEats")


def build_reply_tweet(entry, idx, total):
    """Per-listing reply. URL last so X auto-cards it from the listing's
    og:image (Places photo); the whole card becomes the click-target."""
    name = entry['operatingName']
    keys = entry.get('cuisines') or ([entry['cuisine']] if entry.get('cuisine') else [])
    primary_key = keys[0] if keys else ''
    primary_lbl = CUISINE_LABEL.get(primary_key, primary_key.title()) if primary_key else ''
    if primary_lbl:
        m = re.search(r'\b(cuisine|kitchen|restaurant)\b', name, re.I)
        suffix = m.group(1).title() if m else 'Cuisine'
        name_line = f"{name} · {primary_lbl} {suffix}"
    else:
        name_line = name
    licensed = _licensed_line(entry.get('daysOpen'))
    listing_url = f"{SITE_BASE}/r/{entry['slug']}"
    lines = [
        f"{idx}/{total} {name_line}",
        licensed,
        listing_url,
    ]
    text = '\n'.join(lines)
    if len(text) > 280:
        text = text[:279] + '…'
    return text


def pick_district(state, candidates_by_district, min_count):
    """Returns (district_name, advance_pointer_to) — the district to post and
    the index to record in state. If the natural-rotation district has >=
    min_count candidates we use it; otherwise we scan forward for one that
    does and use that, leaving the rotation pointer where the scan started
    so the natural rotation continues next week."""
    last = state.get('last_district_idx', -1)
    natural = (last + 1) % len(DISTRICTS)
    # Try natural first
    if len(candidates_by_district.get(DISTRICTS[natural], [])) >= min_count:
        return DISTRICTS[natural], natural
    # Scan forward; use the first district with enough candidates but DON'T
    # advance the rotation past the natural slot (so next week resumes from
    # natural+1, not skip-natural+1).
    for offset in range(1, len(DISTRICTS)):
        idx = (natural + offset) % len(DISTRICTS)
        if len(candidates_by_district.get(DISTRICTS[idx], [])) >= min_count:
            return DISTRICTS[idx], natural   # advance to natural, not idx
    return None, natural


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', type=int, default=4,
                    help='target number of listings in the thread (default 4)')
    ap.add_argument('--min',    type=int, default=3,
                    help='minimum listings to post — skip if district has fewer (default 3)')
    ap.add_argument('--since-days', type=int, default=30,
                    help='only consider entries licensed in last N days (default 30)')
    ap.add_argument('--district', help='force a specific district instead of rotation')
    ap.add_argument('--dry-run', action='store_true', help='print tweets, do not POST')
    args = ap.parse_args()

    corr   = json.loads(DATA_PATH.read_text())
    state  = json.loads(STATE_PATH.read_text())  if STATE_PATH.exists()  else {}
    posted = json.loads(POSTED_PATH.read_text()) if POSTED_PATH.exists() else {}

    # Bucket eligible entries by district
    by_district = {d: [] for d in DISTRICTS}
    for e in corr['newOpenings']['recent']:
        slug = e.get('slug')
        if not slug or slug in posted: continue
        if e.get('daysOpen', 999) > args.since_days: continue
        d = e.get('district')
        if d in by_district:
            by_district[d].append(e)
    for d in by_district:
        by_district[d].sort(key=lambda x: x.get('issuedDate', ''), reverse=True)

    print('candidates per district (last {}d, unposted):'.format(args.since_days))
    for d in DISTRICTS:
        marker = ' ← natural rotation' if d == DISTRICTS[(state.get('last_district_idx', -1) + 1) % len(DISTRICTS)] else ''
        print(f"  {d:14s} {len(by_district[d])}{marker}")

    # Pick district
    if args.district:
        if args.district not in DISTRICTS:
            sys.exit(f"unknown district: {args.district!r}  (valid: {DISTRICTS})")
        district = args.district
        advance_to = DISTRICTS.index(args.district)
        if len(by_district[district]) < args.min:
            print(f"\n--district {district} forced but only {len(by_district[district])} candidates "
                  f"(min={args.min}). Posting anyway.")
    else:
        district, advance_to = pick_district(state, by_district, args.min)
        if not district:
            print(f"\nno district has >= {args.min} unposted candidates. Skipping this week.")
            return

    selected = by_district[district][:args.target]
    total = len(selected)
    print(f"\n→ district: {district}  ({total} listings)")
    for i, e in enumerate(selected, 1):
        print(f"   {i}/{total} [{e.get('daysOpen','?')}d] {e['operatingName']}")

    # Build the thread
    lead_text = build_lead_tweet(district, total)
    reply_texts = [build_reply_tweet(e, i, total) for i, e in enumerate(selected, 1)]

    if args.dry_run:
        print('\n--- DRY RUN — lead tweet ---')
        print(lead_text)
        for i, t in enumerate(reply_texts, 1):
            print(f'\n--- DRY RUN — reply {i}/{total} ({len(t)} chars) ---')
            print(t)
        return

    creds = _load_secrets()

    # Post lead
    print(f"\nposting lead ({len(lead_text)} chars)…")
    try:
        lead = post_tweet(lead_text, creds)
    except Exception as ex:
        sys.exit(f"FAIL lead: {ex}")
    lead_id = lead.get('data', {}).get('id')
    print(f"  → lead_id={lead_id}  https://x.com/JoshuaOpolko/status/{lead_id}")

    # Post replies — chain each reply to the lead (not to previous reply), so
    # all reply cards appear at the same depth under the lead in X's UI.
    reply_ids = []
    for i, (e, text) in enumerate(zip(selected, reply_texts), 1):
        print(f"posting {i}/{total} {e['slug']} ({len(text)} chars)…")
        try:
            r = post_tweet(text, creds, reply_to=lead_id)
            rid = r.get('data', {}).get('id')
            reply_ids.append(rid)
            posted[e['slug']] = {
                'tweet_id': rid,
                'lead_id':  lead_id,
                'district': district,
                'posted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }
            print(f"  → {rid}")
            # short politeness gap between replies
            time.sleep(2)
        except Exception as ex:
            print(f"  FAIL reply {i}: {ex}")

    # Persist state + posted
    state['last_district_idx'] = advance_to
    state['last_run_at']       = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    state['last_district']     = district
    state['last_lead_id']      = lead_id
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    POSTED_PATH.write_text(json.dumps(posted, indent=2, sort_keys=True))
    print(f"\ndone. lead + {len(reply_ids)}/{total} replies posted.")
    print(f"  thread: https://x.com/JoshuaOpolko/status/{lead_id}")


if __name__ == '__main__':
    main()
