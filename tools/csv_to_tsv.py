#!/usr/bin/env python3
"""
Convert the City of Toronto licences CSV to a tab-separated TSV at
/tmp/business_licences.tsv. Field values in the source CSV contain commas
(addresses), so plain `cut -d,` produces misaligned columns. The TSV has the
same rows but with tabs as the delimiter — every field value in this dataset
is comma-/space-safe but tab-safe (no embedded tabs), so standard Unix tools
work cleanly:

  cut -f13 /tmp/business_licences.tsv | sort -u          # all Conditions tags
  grep 'CHAIN'  /tmp/business_licences.tsv | wc -l        # count CHAIN rows
  awk -F'\t' '$2 ~ /EATING/ {print $4, $9}' /tmp/business_licences.tsv

  # column-by-name via awk:
  awk -F'\t' 'NR==1{for(i=1;i<=NF;i++)c[$i]=i} NR>1 && $c["Category"]~/EATING/ {print $c["Operating Name"]}' /tmp/business_licences.tsv

Column index (1-based, after conversion):
  1: _id        | 2: Category          | 3: Licence No.       | 4: Operating Name
  5: Issued     | 6: Client Name       | 7: Business Phone    | 8: Business Phone Ext.
  9: Licence Address Line 1            | 10: Licence Address Line 2
 11: Licence Address Line 3            | 12: Ward             | 13: Conditions
 14: Free Form Conditions Line 1       | 15: Free Form Conditions Line 2
 16: Plate No. | 17: Endorsements      | 18: Cancel Date      | 19: Last Record Update
"""
import csv, sys, os

DEFAULT_IN  = '/tmp/business_licences_alt.csv'
DEFAULT_OUT = '/tmp/business_licences.tsv'

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IN
    dst = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    if not os.path.exists(src):
        sys.exit(f"missing source CSV: {src}\nDownload with:\n  curl -o {src} 'https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/municipal-licensing-and-standards-business-licences-and-permits/resource/54bddc5e-92d9-4102-89c1-43e82f8f4d2d/download/business-licences-data.csv'")

    n = 0
    with open(src, encoding='utf-8', errors='replace') as f, \
         open(dst, 'w', encoding='utf-8') as out:
        reader = csv.reader(f)
        for row in reader:
            # Strip any stray tabs from values so the TSV stays clean. The
            # Toronto data doesn't typically contain tabs, but defensive.
            out.write('\t'.join((c or '').replace('\t', ' ').replace('\n', ' ') for c in row))
            out.write('\n')
            n += 1
    print(f"wrote {n} rows → {dst}")
    print()
    print("Try:")
    print(f"  cut -f13 {dst} | sort -u | head -20         # unique Conditions tags")
    print(f"  awk -F'\\t' '$2 ~ /EATING/' {dst} | wc -l    # count EATING rows")
    print(f"  grep -P '\\tCHAIN' {dst} | head -5            # rows where Conditions contains CHAIN")

if __name__ == '__main__':
    main()
