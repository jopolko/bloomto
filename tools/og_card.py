#!/usr/bin/env python3
"""
Branded 1200x675 PNG card generator for a restaurant entry.

Used by:
  - inject_openings.py — writes one PNG per kept entry to /og/<slug>.png
    at inject time. The per-listing HTML at /r/<slug>.html points its
    og:image meta tag at this PNG so X / Facebook / Slack / iMessage all
    show the personalized card when the URL is shared.
  - x_post_new_openings.py — historically attached the PNG directly to
    tweets via the v1.1 /media/upload endpoint; with per-listing OG
    pages live, the bot can post text-only and X auto-cards from the
    page's og:image, giving the same visual + a click-target on the URL.

Stdlib + rsvg-convert (apt: librsvg2-bin). Card design lives in
build_card_svg() — edit there to change the look.
"""
import os, re, subprocess, tempfile, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import CUISINE_LABEL, cuisine_color


# Curated palette for seed cuisines (mirrors inject_openings.PALETTE_HEX);
# novel/dynamic cuisines fall through to cuisine_color() hash-derived hex.
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


def _xml_escape(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
                  .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;'))


def _fit_font_size(text, base_size, max_chars):
    n = len(text or '')
    if n <= max_chars: return base_size
    if n <= max_chars * 1.4: return int(base_size * 0.78)
    if n <= max_chars * 1.8: return int(base_size * 0.6)
    return int(base_size * 0.5)


def _tag_text(days):
    if days is None: return 'NEWLY LICENSED'
    if days <= 1:    return 'LICENSED TODAY'
    if days <= 30:   return f'LICENSED {days}D AGO'
    if days <= 60:   return f'LICENSED {days // 7}W AGO'
    return f'LICENSED {days // 30}MO AGO'


def build_card_svg(entry):
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
    if len(addr_line) > 64:
        addr_line = addr_line[:61] + '…'
    tag_text = _tag_text(entry.get('daysOpen'))
    name_size = _fit_font_size(name, 80, 22)

    pill_x = 70
    pill_svg = []
    for label, color in pills:
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

  <g transform="translate(70,80)">
    <rect width="{32 + 14 * len(tag_text)}" height="44" rx="22" fill="#c83624"/>
    <text x="{(32 + 14 * len(tag_text))//2}" y="30"
          font-family="-apple-system,Helvetica,Arial,sans-serif"
          font-size="20" font-weight="800" fill="#fff" text-anchor="middle"
          letter-spacing="2">{_xml_escape(tag_text)}</text>
  </g>

  <text x="70" y="350" font-family="Iowan Old Style, Charter, Georgia, serif"
        font-size="{name_size}" font-weight="800" fill="#15110d"
        letter-spacing="-2">{_xml_escape(name)}</text>

  {chr(10).join(pill_svg)}

  <text x="70" y="540" font-family="Iowan Old Style, Charter, Georgia, serif"
        font-size="32" font-style="italic" fill="#45403a">{_xml_escape(addr_line)}</text>

  <text x="70" y="625" font-family="Iowan Old Style, Charter, Georgia, serif"
        font-size="28" font-weight="800" fill="#15110d">NowServingTO</text>
  <text x="70" y="652" font-family="ui-monospace,Menlo,Consolas,monospace"
        font-size="18" fill="#7a746a">nowservingto.com</text>
</svg>"""


def render_card_png(entry, out_path=None):
    """Render the card SVG to a PNG. If out_path is given, write there;
    else return the PNG bytes."""
    svg = build_card_svg(entry)
    with tempfile.NamedTemporaryFile(suffix='.svg', mode='w', delete=False, encoding='utf-8') as f:
        f.write(svg); svg_path = f.name
    if out_path:
        png_path = str(out_path)
        return_bytes = False
    else:
        png_path = svg_path[:-4] + '.png'
        return_bytes = True
    try:
        subprocess.run(
            ['rsvg-convert', '-w', '1200', '-h', '675', svg_path, '-o', png_path],
            check=True, capture_output=True,
        )
        if return_bytes:
            return Path(png_path).read_bytes()
    finally:
        try: os.unlink(svg_path)
        except OSError: pass
        if return_bytes:
            try: os.unlink(png_path)
            except OSError: pass
