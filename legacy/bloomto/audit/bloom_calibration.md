# `bloom` calibration — why it's False on every row

## Current gate

```
bloom = (heritageStatus is null)
        AND (solarScore != null)
        AND (solarScore > 80)
        AND (distSubwayM < 800)
```
— from `tools/parcel_scoring.py:80-83`. `BLOOM_SOLAR_THRESHOLD = 80` at line 45.

## What the wire actually contains

Distribution of `solarScore` (shadow-adjusted) and `solarScoreRaw` (P95-normalized SolarTO) across both feeds:

| | n | median raw | p90 raw | max raw | median adj | p90 adj | max adj |
|---|---:|---:|---:|---:|---:|---:|---:|
| elite | 3,743 | 31 | 100 | 100 | 16 | 35 | **81** |
| broader | 15,509 | 25 | 61 | 100 | 12 | 25 | **87** |

**Shadow factor** (`adj / raw` per parcel): median 0.54, p10 0.25, p90 0.70. The 3D Massing shadow analysis is removing ~46% of solar potential on the median parcel. At p10 the parcel keeps just 25% of its raw rooftop yield — i.e. heavily shadowed by neighbours.

The arithmetic: even the **brightest** rooftop on the **least-shadowed** lot in the elite set tops out at 81. The threshold is 80. So the gate is firing on at most 1 parcel — and that parcel apparently doesn't clear the `distSubwayM < 800` half of the gate, hence 0 bloom-true.

## Bloom counts vs alternative thresholds

| Gate variant | elite bloom-true | broader bloom-true |
|---|---:|---:|
| current: `solarScore > 80` | 0 | 0 |
| `solarScore > 75` | 1 | 1 |
| `solarScore > 70` | 3 | 4 |
| `solarScore > 60` | 9 | 13 |
| `solarScoreRaw > 95` (ignore shadow) | 140 | 240 |
| `solarScoreRaw > 80` (ignore shadow) | 181 | 338 |

## Recommendation

The bloom gate is broken in a "0 ÷ 0" way: a calibration mismatch where the threshold (80) was set before the shadow analysis was bolted onto `solarScore`, and never got rescaled.

Three options, ranked by my preference:

**B — lower the threshold to ~60.** Keeps the shadow-adjusted semantics (which match what the page displays) and gives ~22 bloom-true parcels citywide. That's a defensibly-rare "premium" set — about the size you'd expect for a "gold mine" indicator. One-line change to `BLOOM_SOLAR_THRESHOLD` in `tools/parcel_scoring.py:45`.

**A — switch to `solarScoreRaw`.** Sidesteps the shadow analysis for the bloom gate. ~181 elite rows would qualify, which is too many for a "premium" badge. Also inconsistent: the page would show the shadow-adjusted score in the detail panel and use the raw score for the bloom badge — a quiet contradiction.

**C — accept it.** Only if the spec author *wanted* bloom to be near-impossible. Memory and CLAUDE.md don't suggest that.

If you go with B, I'd also bump `BLOOM_SUBWAY_M` from 800m to 600m to keep "premium" feeling tight — at 60 + 800m you get 22 hits; at 60 + 600m you'd get fewer, but they'd genuinely be the cream of the crop.

## Caveat on A — semantic alignment

If you go with A, also consider showing `solarScoreRaw` (not `solarScore`) on the detail panel, so the badge and the displayed number track each other. Otherwise: developer sees "bloom!" + "solarScore: 47" and is confused.
