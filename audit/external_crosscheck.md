# BloomTO &mdash; external cross-check audit

_Generated 2026-05-06 20:47 UTC &middot; sample size 200 (seed 1729) from 3,778 elite + 11,846 broader-only rows_

## Summary

| Check | Checked | Agree | Disagree | % disagree |
|---|---:|---:|---:|---:|
| ✅ zoneClass | 200 | 200 | 0 | 0.0% |
| ✅ sixplexEligible | 200 | 200 | 0 | 0.0% |
| ✅ inRegulatedArea | 200 | 200 | 0 | 0.0% |
| ✅ inFloodingStudyArea | 200 | 200 | 0 | 0.0% |
| ✅ distSubwayM | 200 | 200 | 0 | 0.0% |
| ✅ distStreetcarM | 200 | 200 | 0 | 0.0% |
| ✅ distSubwayStreetcarM == min(subway,streetcar) | 200 | 200 | 0 | 0.0% |


All cross-checks pass on the sample. Either the wire is faithful to its source datasets, or the bugs are too rare for a sample to catch &mdash; raise `--n` to push harder.
