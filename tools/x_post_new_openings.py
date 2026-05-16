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
import os, sys, json, time, base64, hmac, hashlib, secrets, argparse, subprocess, tempfile, re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import CUISINE_LABEL, cuisine_color

# Curated palette for seed cuisines (mirrors inject_openings.PALETTE_HEX);
# falls back to cuisine_color() hash-derived hex for novel cuisines.
PALETTE_HEX = {
    'italian':'#c83624','caribbean':'#1a8a5a','south_asian':'#d4a017','indian':'#e88e2c',
    'pakistani':'#a06030','afghan':'#7a5d3a','bangladeshi':'#b88820','chinese':'#b13e6a',
    'vietnamese':'#4a8b8b','japanese':'#2f3aa3','korean':'#6b2456','filipino':'#e08226',
    'tamil':'#8a5d20','tibetan':'#b15a25','greek':'#1f7a6a','portuguese':'#9b2538',
    'polish':'#4a5a6a','french':'#5a3a7a','irish_uk':'#2a6a40','german':'#6a5a30',
    'jewish_deli':'#4a4a8a','eastern_eu':'#7a4a4a','ukrainian':'#6a5a8a','russian':'#7a4a4a',
    'hungarian':'#8a5050','middle_east':'#b87a25','lebanese':'#c89538','turkish':'#a8662a',
    'syrian':'#9b5520','persian':'#8a4a25','latin':'#cc4a4a','mexican':'#d63d2a',
    'salvadoran':'#c8553a','peruvian':'#b35b50','colombian':'#cc6248','brazilian':'#3d8a47',
    'african_horn':'#a0522d','ethiopian':'#a0522d','eritrean':'#8a4528','somali':'#b06530',
    'african_west':'#5a8a3a','nigerian':'#4a7a30','ghanaian':'#6a8a40','moroccan':'#b87a2a',
    'jamaican':'#1f7a4a','trinidadian':'#2a9560','guyanese':'#3a8060','haitian':'#1a6855',
    'thai':'#7a8a3a','indonesian':'#7a6a40','malaysian':'#5a7a55','burmese':'#8a7050',
}

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


def post_tweet(text, creds, media_ids=None):
    """POST to api.x.com/2/tweets with OAuth1.0a user-context. Optionally
    attaches media (list of media_id_string from upload_media)."""
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
    body_obj = {'text': text}
    if media_ids:
        body_obj['media'] = {'media_ids': list(media_ids)}
    body = json.dumps(body_obj).encode('utf-8')
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


def _xml_escape(s):
    """SVG-safe text escape — must escape &, <, > at minimum."""
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
                  .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;'))


def _fit_font_size(text, base_size, max_chars):
    """Scale name font down for long names so it fits on one line."""
    n = len(text or '')
    if n <= max_chars: return base_size
    if n <= max_chars * 1.4: return int(base_size * 0.78)
    if n <= max_chars * 1.8: return int(base_size * 0.6)
    return int(base_size * 0.5)


def build_card_svg(entry):
    """1200×675 PNG card for a tweet. Branded in site colors; cuisine pill
    colored to match the palette; name + address + temporal hook."""
    name = entry.get('operatingName', '')
    keys = entry.get('cuisines') or ([entry['cuisine']] if entry.get('cuisine') else [])
    pills = []
    for k in keys[:3]:
        if not k: continue
        color = PALETTE_HEX.get(k) or cuisine_color(k)
        label = CUISINE_LABEL.get(k, k.replace('_', ' ').title())
        pills.append((label, color))
    addr = entry.get('address') or ''
    district = entry.get('district') or ''
    addr_line = addr
    if district and district not in addr:
        addr_line = f"{addr_line} · {district}" if addr_line else district
    # Truncate over-long addresses so they fit on one line.
    if len(addr_line) > 64:
        addr_line = addr_line[:61] + '…'

    days = entry.get('daysOpen')
    if days is None: tag_text = 'NEWLY LICENSED'
    elif days <= 1:  tag_text = 'LICENSED TODAY'
    elif days <= 7:  tag_text = f'LICENSED {days}D AGO'
    elif days <= 30: tag_text = f'LICENSED {days}D AGO'
    elif days <= 60: tag_text = f'LICENSED {days // 7}W AGO'
    else:            tag_text = f'LICENSED {days // 30}MO AGO'

    name_size = _fit_font_size(name, 80, 22)

    # Build pills as a horizontal row of label-rects.
    pill_x = 70
    pill_svg = []
    for label, color in pills:
        # Estimate pill width from char count (rough but consistent).
        w = max(140, 28 * len(label) + 32)
        pill_svg.append(
            f'<g transform="translate({pill_x},420)">'
            f'<rect width="{w}" height="48" rx="24" fill="{color}"/>'
            f'<text x="{w//2}" y="32" font-family="-apple-system,Helvetica,Arial,sans-serif" '
            f'font-size="22" font-weight="700" fill="#fff" text-anchor="middle" '
            f'letter-spacing="1.5">{_xml_escape(label.upper())}</text>'
            f'</g>'
        )
        pill_x += w + 12

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675" width="1200" height="675">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#faf7ee"/>
      <stop offset="100%" stop-color="#f0e8d4"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="675" fill="url(#bg)"/>
  <rect x="0" y="0" width="1200" height="8" fill="#15110d"/>

  <!-- Licensed-today tag, top-left -->
  <g transform="translate(70,80)">
    <rect width="{32 + 14 * len(tag_text)}" height="44" rx="22" fill="#c83624"/>
    <text x="{(32 + 14 * len(tag_text))//2}" y="30"
          font-family="-apple-system,Helvetica,Arial,sans-serif"
          font-size="20" font-weight="800" fill="#fff" text-anchor="middle"
          letter-spacing="2">{_xml_escape(tag_text)}</text>
  </g>

  <!-- Restaurant name, the dominant element -->
  <text x="70" y="350" font-family="Iowan Old Style, Charter, Georgia, serif"
        font-size="{name_size}" font-weight="800" fill="#15110d"
        letter-spacing="-2">{_xml_escape(name)}</text>

  <!-- Cuisine pills, just below the name -->
  {chr(10).join(pill_svg)}

  <!-- Address + district -->
  <text x="70" y="540" font-family="Iowan Old Style, Charter, Georgia, serif"
        font-size="32" font-style="italic" fill="#45403a">{_xml_escape(addr_line)}</text>

  <!-- Brand footer, bottom-left -->
  <text x="70" y="625" font-family="Iowan Old Style, Charter, Georgia, serif"
        font-size="28" font-weight="800" fill="#15110d">NowServingTO</text>
  <text x="70" y="652" font-family="ui-monospace,Menlo,Consolas,monospace"
        font-size="18" fill="#7a746a">nowservingto.com</text>
</svg>"""


def render_card_png(entry):
    """Render the SVG card to a PNG byte-string via rsvg-convert."""
    svg = build_card_svg(entry)
    with tempfile.NamedTemporaryFile(suffix='.svg', mode='w', delete=False, encoding='utf-8') as f:
        f.write(svg); svg_path = f.name
    png_path = svg_path[:-4] + '.png'
    try:
        subprocess.run(
            ['rsvg-convert', '-w', '1200', '-h', '675', svg_path, '-o', png_path],
            check=True, capture_output=True,
        )
        return Path(png_path).read_bytes()
    finally:
        for p in (svg_path, png_path):
            try: os.unlink(p)
            except OSError: pass


def upload_media(png_bytes, creds):
    """Upload a PNG to X's media endpoint and return the media_id_string.
    Uses v1.1 multipart/form-data — X v2 doesn't yet expose media upload."""
    url = 'https://upload.twitter.com/1.1/media/upload.json'
    boundary = 'NSTO' + secrets.token_hex(12)
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="media"; filename="card.png"\r\n'
        f'Content-Type: image/png\r\n\r\n'
    ).encode() + png_bytes + f'\r\n--{boundary}--\r\n'.encode()

    oauth = {
        'oauth_consumer_key': creds['X_API_KEY'],
        'oauth_nonce': secrets.token_hex(16),
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': str(int(time.time())),
        'oauth_token': creds['X_ACCESS_TOKEN'],
        'oauth_version': '1.0',
    }
    # Multipart upload: signature base string does NOT include body params.
    oauth['oauth_signature'] = _oauth1_sign(
        'POST', url, oauth, {},
        creds['X_API_SECRET'], creds['X_ACCESS_TOKEN_SECRET'],
    )
    auth_header = 'OAuth ' + ', '.join(
        f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items())
    )
    req = Request(url, data=body, method='POST', headers={
        'Authorization': auth_header,
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'User-Agent': 'nowservingto-bot/1.0',
    })
    try:
        with urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
    except HTTPError as e:
        raise RuntimeError(f'media upload HTTP {e.code}: {e.read().decode(errors="replace")[:500]}')
    return result.get('media_id_string') or str(result.get('media_id'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max', type=int, default=1, help='max tweets to post this run (default 1)')
    ap.add_argument('--since-days', type=int, default=14, help='only consider entries opened in last N days')
    ap.add_argument('--dry-run', action='store_true', help='print the tweet, do not POST')
    ap.add_argument('--no-card', action='store_true', help='skip image card, post text-only')
    ap.add_argument('--card-only', action='store_true', help='render card SVG/PNG to /tmp and exit (for design iteration)')
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

    # Card-only mode: dump the SVG + PNG for the freshest candidate and exit.
    if args.card_only:
        e = candidates[0]
        svg = build_card_svg(e)
        png = render_card_png(e)
        out_svg = Path('/tmp') / f"x-card-{e['slug']}.svg"
        out_png = Path('/tmp') / f"x-card-{e['slug']}.png"
        out_svg.write_text(svg); out_png.write_bytes(png)
        print(f"wrote {out_svg} and {out_png}")
        # Copy to Windows desktop if WSL
        wdesk = Path('/mnt/c/Users/josh/Desktop')
        if wdesk.exists():
            (wdesk / out_png.name).write_bytes(png)
            print(f"  also: {wdesk / out_png.name}")
        return

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
        media_ids = None
        if not args.no_card:
            try:
                png = render_card_png(e)
                media_id = upload_media(png, creds)
                media_ids = [media_id]
                print(f"  card uploaded: media_id={media_id}  ({len(png)} bytes)")
            except Exception as ex:
                print(f"  card render/upload failed (posting text-only): {ex}")
        try:
            result = post_tweet(text, creds, media_ids=media_ids)
            tweet_id = result.get('data', {}).get('id')
            posted[e['slug']] = {
                'tweet_id': tweet_id,
                'posted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'media_ids': media_ids,
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
