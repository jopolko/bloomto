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

DEFAULT_IN  = '/tmp/business_licences_alt.csv'
DEFAULT_CSV = '/tmp/business_licences_clean.csv'
DEFAULT_TSV = '/tmp/business_licences.tsv'

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IN
    if not os.path.exists(src):
        sys.exit(f"missing source CSV: {src}\nDownload with:\n  curl -o {src} 'https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/municipal-licensing-and-standards-business-licences-and-permits/resource/54bddc5e-92d9-4102-89c1-43e82f8f4d2d/download/business-licences-data.csv'")

    n = 0
    with open(src, encoding='utf-8', errors='replace') as f, \
         open(DEFAULT_CSV, 'w', encoding='utf-8') as out_csv, \
         open(DEFAULT_TSV, 'w', encoding='utf-8') as out_tsv:
        for row in csv.reader(f):
            # Replace intra-field commas, tabs, newlines with single spaces.
            # The Toronto data uses commas in addresses ("123 BLOOR ST W, UNIT 2")
            # and semicolons in Conditions ("CHAIN;SHARED ADDRESS;"). After
            # this cleanup, every comma in the output is a delimiter.
            clean = [(c or '').replace(',', ' ').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ').strip() for c in row]
            out_csv.write(','.join(clean) + '\n')
            out_tsv.write('\t'.join(clean) + '\n')
            n += 1
    print(f"wrote {n} rows → {DEFAULT_CSV}")
    print(f"                  {DEFAULT_TSV}")
    print()
    print("Cut and grep work now:")
    print(f"  cut -f13 -d, {DEFAULT_CSV} | sort -u | head -20")
    print(f"  cut -f2,4,9,13 -d, {DEFAULT_CSV} | grep ',CHAIN' | head")
    print(f"  cut -f4,9 -d, {DEFAULT_CSV} | grep ' LENA' ")

if __name__ == '__main__':
    main()
