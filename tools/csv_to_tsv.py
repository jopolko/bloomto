#!/usr/bin/env python3
"""
Convert the City of Toronto licences CSV into a clean comma-delimited file
where intra-field commas (and newlines) have been replaced with spaces.
Plain `cut -d, -f<n>` and `grep` then work as expected.

Outputs:
  /tmp/business_licences_clean.csv  ← unquoted, commas-only-as-delimiter (use this)
  /tmp/business_licences.tsv        ← tab-separated (for tools like awk -F'\t')

Both contain the same rows; pick whichever feels natural.

Column index (1-based, same in both):
  1: _id        | 2: Category          | 3: Licence No.       | 4: Operating Name
  5: Issued     | 6: Client Name       | 7: Business Phone    | 8: Business Phone Ext.
  9: Licence Address Line 1            | 10: Licence Address Line 2
 11: Licence Address Line 3            | 12: Ward             | 13: Conditions
 14: Free Form Conditions Line 1       | 15: Free Form Conditions Line 2
 16: Plate No. | 17: Endorsements      | 18: Cancel Date      | 19: Last Record Update

Try:
  cut -f13 -d, /tmp/business_licences_clean.csv | sort -u | head
  cut -f2,4,9,13 -d, /tmp/business_licences_clean.csv | grep ',CHAIN'
  grep ',EATING' /tmp/business_licences_clean.csv | wc -l
"""
import csv, sys, os

DEFAULT_IN     = '/tmp/business_licences_alt.csv'
DEFAULT_CSV    = '/tmp/business_licences_clean.csv'
DEFAULT_TSV    = '/tmp/business_licences.tsv'
DEFAULT_FILT   = '/tmp/business_licences_food_365d.csv'

FOOD_CATS = {
    'EATING OR DRINKING ESTABLISHMENT',
    'TAKE-OUT OR RETAIL FOOD ESTABLISHMENT',
    'EATING ESTABLISHMENT',
    'RETAIL STORE (FOOD)',
}

def _parse_d(s):
    from datetime import datetime
    s = (s or '').strip()
    if not s: return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%m/%d/%Y', '%Y-%m-%dT%H:%M:%S'):
        try: return datetime.strptime(s.split(' ')[0], fmt).date()
        except ValueError: pass
    return None

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IN
    if not os.path.exists(src):
        sys.exit(f"missing source CSV: {src}\nDownload with:\n  curl -o {src} 'https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/municipal-licensing-and-standards-business-licences-and-permits/resource/54bddc5e-92d9-4102-89c1-43e82f8f4d2d/download/business-licences-data.csv'")

    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=365)

    n = n_food_recent = 0
    with open(src, encoding='utf-8', errors='replace') as f, \
         open(DEFAULT_CSV,  'w', encoding='utf-8') as out_csv, \
         open(DEFAULT_TSV,  'w', encoding='utf-8') as out_tsv, \
         open(DEFAULT_FILT, 'w', encoding='utf-8') as out_filt:
        reader = csv.reader(f)
        header = next(reader, None)
        if header:
            # Clean header same way and pass through to all three outputs
            h_clean = [(c or '').replace(',', ' ').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ').strip() for c in header]
            out_csv.write(','.join(h_clean) + '\n')
            out_tsv.write('\t'.join(h_clean) + '\n')
            out_filt.write(','.join(h_clean) + '\n')

        for row in reader:
            # Replace intra-field commas, tabs, newlines with single spaces so
            # every comma (or tab) in the output is an authentic delimiter.
            clean = [(c or '').replace(',', ' ').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ').strip() for c in row]
            out_csv.write(','.join(clean) + '\n')
            out_tsv.write('\t'.join(clean) + '\n')
            n += 1

            # Pre-filtered food + last 365d + not-cancelled — no shell date math needed
            if len(clean) < 18: continue
            category   = clean[1] if len(clean) > 1 else ''
            issued     = clean[4] if len(clean) > 4 else ''
            cancel     = clean[17] if len(clean) > 17 else ''
            if category not in FOOD_CATS: continue
            if cancel.strip(): continue
            d = _parse_d(issued)
            if d is None or d < cutoff: continue
            out_filt.write(','.join(clean) + '\n')
            n_food_recent += 1

    print(f"wrote {n} rows → {DEFAULT_CSV}    (full clean CSV)")
    print(f"                  {DEFAULT_TSV}    (full clean TSV)")
    print(f"        {n_food_recent} rows → {DEFAULT_FILT}    (FOOD only, last 365d, not cancelled)")
    print()
    print("Examples on the pre-filtered file (no date math needed):")
    print(f"  cut -f4,9 -d, {DEFAULT_FILT}                            # name + address")
    print(f"  cut -f4,9,13 -d, {DEFAULT_FILT} | grep ',CHAIN'         # chain-flagged rows")
    print(f"  cut -f2,4,5,6,9,10,11,17 -d, {DEFAULT_FILT}             # your 8-column slice")
    print(f"  cut -f13 -d, {DEFAULT_FILT} | sort -u                   # Conditions tags found in active food licences")

if __name__ == '__main__':
    main()
