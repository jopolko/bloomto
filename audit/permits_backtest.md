# BloomTO &mdash; permits backtest

_Generated 2026-05-05 22:01 UTC &middot; window: ISSUED_DATE ≥ 2024-01-01, units in [3, 6]_

## Summary

- **483** approved permits matched the multiplex window
- **12** (2%) parcels appear in `parcels-top.json` (elite, top 3,757)
- **31** (6%) parcels appear in `parcels-broader.json` only
- **440** (91%) parcels are missing from both feeds &mdash; *recall gap*

**Combined recall** (elite ∪ broader): **9%**

Recall is the fraction of approved multiplex permits that BloomTO would have ranked. It's not expected to be 100% &mdash; many permits land on parcels with poor transit, heritage encumbrances, or low scores by design. But it's the most direct test of whether the gates are *too* strict.

## Recall by year

| Year | Permits | Elite | Broader | Missed | Combined recall |
|---|---:|---:|---:|---:|---:|
| 2024 | 107 | 5 | 7 | 95 | 11% |
| 2025 | 262 | 3 | 18 | 241 | 8% |
| 2026 | 114 | 4 | 6 | 104 | 9% |

## Recall by units_created

| Units | Permits | Elite | Broader | Missed | Combined recall |
|---|---:|---:|---:|---:|---:|
| 3 | 179 | 5 | 9 | 165 | 8% |
| 4 | 269 | 5 | 21 | 243 | 10% |
| 5 | 19 | 1 | 1 | 17 | 11% |
| 6 | 16 | 1 | 0 | 15 | 6% |

## Sample of missed permits (440 total)

These are parcels where a developer was approved to build 3-6 units, but BloomTO didn&rsquo;t rank the parcel in either feed. Each is a candidate for a gate that&rsquo;s too strict.

| permit | address | units | issued | category | est cost |
|---|---|---:|---|---|---:|
| `26 116088 BLD` | 45 YORKVIEW DR | 4 | 2026-05-01 | new_residential | $1,500,000 |
| `25 238605 BLD` | 33 REIDMOUNT AVE | 4 | 2026-05-01 | new_residential | $1,000,000 |
| `25 195595 BLD` | 1552 BATHURST ST | 4 | 2026-05-01 | addition_with_units | $124,500 |
| `26 131104 BLD` | 7 DEVONDALE AVE | 4 | 2026-04-30 | new_residential | $2,000,000 |
| `25 249057 BLD` | 984 CALEDONIA RD | 4 | 2026-04-27 | new_residential | $900,000 |
| `25 154264 BLD` | 3117 ST CLAIR AVE E | 4 | 2026-04-20 | new_residential | $500,000 |
| `25 266462 BLD` | 53 WINSTON PARK BLVD | 4 | 2026-04-15 | new_residential | $1,000,000 |
| `25 142936 BLD` | 70 LANSDOWNE AVE | 4 | 2026-04-13 | addition_with_units |  |
| `25 259201 BLD` | 585 SHAW ST | 3 | 2026-04-09 | addition_with_units | $700,000 |
| `26 104517 BLD` | 99 PEMBERTON AVE | 4 | 2026-04-08 | new_residential | $2,000,000 |
| `26 135802 BLD` | 1936 LAWRENCE AVE W | 6 | 2026-04-08 | new_residential | $300,000 |
| `26 117162 BLD` | 244 VIRGINIA AVE | 6 | 2026-04-08 | new_residential | $1,500,000 |
| `25 257528 BLD` | 214 CHAMBERS AVE | 3 | 2026-04-07 | new_residential | $1,700,000 |
| `26 103796 BLD` | 43 MCINTOSH ST | 4 | 2026-04-07 | new_residential | $600,000 |
| `26 111805 BLD` | 77 GILBERT AVE | 3 | 2026-04-07 | addition_with_units | $500,000 |
| `25 121412 BLD` | 45 SUMMIT AVE | 4 | 2026-04-07 | addition_with_units | $800,000 |
| `25 177834 BLD` | 36 NORTH EDGELY AVE | 3 | 2026-04-07 | addition_with_units | $60,000 |
| `26 107352 BLD` | 72 BEXHILL AVE | 4 | 2026-04-01 | new_residential | $500,000 |
| `26 118052 BLD` | 61 HOLFORD CRES | 3 | 2026-04-01 | addition_with_units | $60,000 |
| `25 264850 BLD` | 229 GAMBLE AVE | 6 | 2026-03-31 | new_residential | $1,300,000 |
| `25 228067 BLD` | 55 LONBOROUGH AVE | 4 | 2026-03-31 | new_residential | $1,500,000 |
| `26 113619 BLD` | 162 TIMES RD | 4 | 2026-03-31 | new_residential |  |
| `26 114778 BLD` | 10 DROMORE CRES | 4 | 2026-03-30 | new_residential | $2,000,000 |
| `25 200108 BLD` | 1244 ROYAL YORK RD | 4 | 2026-03-30 | addition_with_units | $69,700 |
| `25 246390 BLD` | 44 SHERWOOD AVE | 4 | 2026-03-26 | new_residential | $100,000 |

_…and 415 more not shown_

## Sample of caught permits (43 total)

Sanity check &mdash; these matched a parcel in the feed. Confirm the addresses look right.

| permit | address | units | tier | score | neighborhood |
|---|---|---:|---|---:|---|
| `24 132965 BLD` | 70 RONCESVALLES AVE | 3 | broader | 91 | High Park-Swansea |
| `15 270867 BLD` | 606-608 DUNDAS ST W | 3 | broader | 87 | Kensington-Chinatown |
| `25 190725 BLD` | 2902-2906 LAKE SHORE BLVD W | 4 | broader | 87 | New Toronto |
| `25 109838 BLD` | 733 COXWELL AVE | 3 | broader | 86 | Danforth |
| `25 256052 BLD` | 143 DOWLING AVE | 6 | elite | 85 | South Parkdale |
| `25 163437 BLD` | 1971 KEELE ST | 4 | broader | 84 | Keelesdale-Eglinton West |
| `24 225491 BLD` | 466 OSSINGTON AVE | 3 | elite | 84 | Palmerston-Little Italy |
| `25 254881 BLD` | 90 ASH CRES | 4 | elite | 83 | Long Branch |
| `24 196436 BLD` | 82 ASH CRES | 4 | elite | 83 | Long Branch |
| `25 256418 BLD` | 65 LIVINGSTONE AVE | 4 | broader | 83 | Briar Hill-Belgravia |
| `24 202350 BLD` | 21 LOUISA ST | 4 | elite | 82 | Mimico-Queensway |
| `24 202689 BLD` | 883 BLOOR ST W | 5 | elite | 82 | Palmerston-Little Italy |
| `24 122667 BLD` | 17 MAYNARD AVE | 3 | elite | 82 | South Parkdale |
| `24 161943 BLD` | 373 MANNING AVE | 4 | broader | 81 | Palmerston-Little Italy |
| `24 166489 BLD` | 375 MANNING AVE | 4 | broader | 80 | Palmerston-Little Italy |
