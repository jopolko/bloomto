# BloomTO &mdash; external cross-check audit

_Generated 2026-05-05 22:01 UTC &middot; sample size 200 (seed 1729) from 3,757 elite + 11,757 broader-only rows_

## Summary

| Check | Checked | Agree | Disagree | % disagree |
|---|---:|---:|---:|---:|
| ✅ zoneClass | 200 | 200 | 0 | 0.0% |
| ✅ sixplexEligible | 200 | 200 | 0 | 0.0% |
| ✅ inRegulatedArea | 200 | 200 | 0 | 0.0% |
| ✅ inFloodingStudyArea | 200 | 200 | 0 | 0.0% |
| ❌ distSubwayM | 200 | 169 | 31 | 15.5% |
| ❌ distStreetcarM | 200 | 187 | 13 | 6.5% |
| ✅ distSubwayStreetcarM == min(subway,streetcar) | 200 | 200 | 0 | 0.0% |


## distSubwayM &mdash; 31 disagreements out of 200

| parcelId | wire | derived from source |
|---|---|---|
| 5385048 | `2472` | `2248` |
| 5493114 | `2282` | `2193` |
| 5383499 | `2504` | `2359` |
| 5350401 | `1480` | `1265` |
| 5358402 | `1693` | `1578` |
| 5131552 | `2181` | `1898` |
| 5125925 | `1981` | `1959` |
| 5494047 | `2376` | `2310` |
| 5395720 | `2090` | `1982` |
| 5460733 | `1538` | `1433` |
| 5457628 | `932` | `874` |
| 5457575 | `927` | `891` |
| 5493143 | `2443` | `2423` |
| 5493178 | `2438` | `2390` |
| 5335757 | `1105` | `897` |

_…and 16 more not shown_


## distStreetcarM &mdash; 13 disagreements out of 200

| parcelId | wire | derived from source |
|---|---|---|
| 5382085 | `827` | `804` |
| 5460668 | `1779` | `1535` |
| 5461566 | `1734` | `1519` |
| 5339610 | `1447` | `1280` |
| 5395570 | `1153` | `967` |
| 5398245 | `161` | `141` |
| 5453069 | `754` | `681` |
| 5345794 | `1290` | `1175` |
| 5462737 | `1966` | `1928` |
| 5461821 | `1709` | `1457` |
| 5328091 | `738` | `692` |
| 5464385 | `1597` | `1433` |
| 5464914 | `1856` | `1815` |
