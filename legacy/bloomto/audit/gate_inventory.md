# Gate inventory — what filters a parcel out of the BloomTO wire

Every condition that affects whether a parcel of Toronto's 528K-parcel canonical universe makes it onto `data/parcels-top.json` or `data/parcels-broader.json`. Generated from a code read of the v1.2 ETL on 2026-05-05.

## TL;DR

The dominant filter is **transit proximity (< 500m to subway∪streetcar)** at `parcel_scoring.py:74,131`. The permits backtest (`audit/permits_backtest.md`) shows this gate alone excludes **91% of approved 3-6 unit residential permits since 2024-01-01** &mdash; a recall problem if BloomTO is supposed to surface "where developers will build multiplexes," but defensible if BloomTO is positioned as "transit-served multiplex candidates only."

## The full gate stack, in execution order

### Hard-exclusion gates (parcel never enters the wire, regardless of score)

| # | Gate | Threshold | Source | Effect |
|---|---|---|---|---|
| 1 | Neighborhood lookup | parcel must lie in a Toronto neighborhood polygon | `build_parcels.py:347-350` | Drops parcels outside the official 158-neighborhood overlay (very rare; ravines / utility ROW residue). |
| 2 | Institutional-point overlay | parcel intersects schools, places of worship, parks, libraries, fire/police/ambulance, LTC, child-care, community-recreation | `build_parcels.py:359-363` (calls `institutions.py:is_institutional`) | Whole-parcel exclusion; cheap STRtree query before expensive stages. |
| 3 | Buildable-polygon | no address AND no building footprint | `build_parcels.py:468-470` | Drops residual ROW slivers, common-element strips, easements, laneway segments. ETL stats called this ~6,200 polygons. |
| 4 | Score-zero exclusion | `base_score == 0 AND soft_score == 0` (unless `--include-non-eligible`) | `build_parcels.py:436-437` | Default behaviour. The `--include-non-eligible` flag overrides for diagnostic runs. |

### Score-forcing gates (parcel gets `score=0` and falls into gate #4)

These live inside `parcel_scoring.score()` (`parcel_scoring.py:99-134`).

| # | Gate | Threshold | Constant | What it filters |
|---|---|---|---|---|
| 5 | Residential | `max_units > 0` (i.e., zone is in `zoning_multipliers.json`'s residential set) | derived | Non-residential zones (`U`, `OS`, etc.) get score 0. |
| 6 | Part-IV heritage | `heritage_status != "part_iv"` | `_HERITAGE_FACTORS["part_iv"] = 0.0` (line 64) | Designated parcels can't demolish; multiplex conversion blocked. |
| 7 | Transit buffer | `distSubwayStreetcarM < 500` | `TRANSIT_BUFFER_M = 500` (line 19) | **The dominant filter. 91% of approved multiplex permits since 2024 fail this.** |
| 8 | Sliver gate | `area_m2 >= 100` | `MIN_BUILDABLE_AREA_M2 = 100` (line 41) | Excludes residual polygons; a typical narrow Toronto lot is 130-180 m². |

### Soft-score path (`softScore > 0` saves the parcel even when `score == 0`)

The soft-score relaxes gate #7 to a wider 1500m radius. Parcels in 500-1500m of major transit get `softScore > 0` and survive the score-zero exclusion in gate #4. They land on the wire with `outsideTransitBuffer=true`.

But the *frontend* (`goldmines.html`) re-filters with `if (r.outsideTransitBuffer === true) return false;`, so these parcels are loaded into the page and immediately hidden. Belt-and-suspenders pattern; the wire-degeneracy audit found `outsideTransitBuffer` is constant False on all 19K rows because the score-zero gate (4) already filtered out most of them, and the few that survived are filtered again by the frontend.

### Bloom (premium badge) gates &mdash; not exclusion gates, just badge gates

These don't exclude the parcel; they just decide the visual `bloom` flag. From `bloom_flag()` at `parcel_scoring.py:209-230`.

| # | Gate | Threshold | Constant |
|---|---|---|---|
| 9 | No heritage of any tier | `heritage_status is None` | hardcoded |
| 10 | Solar above threshold | `solarScore > 80` | `BLOOM_SOLAR_THRESHOLD = 80` (line 45) |
| 11 | Subway-only proximity | `distSubwayM < 800` | `BLOOM_SUBWAY_M = 800` (line 46) |

### Frontend-only filters (don't affect the wire, but affect what the user sees)

`goldmines.html`:
- `r.outsideTransitBuffer === true` → hidden (covered above)
- `looksInstitutional(r)` → hidden, even though gate #2 already removed institutional overlays. Catches institutional parcels that didn't match a point overlay (e.g., a private school with no city-services listing).

## Inconsistencies worth deciding on

**A. Two different transit thresholds with two different stop sets.**

| Gate | Distance | Stop set | File:line |
|---|---|---|---|
| Score (gate #7) | 500m | subway ∪ streetcar | `parcel_scoring.py:19,74` |
| Bloom (gate #11) | 800m | **subway only** | `parcel_scoring.py:46` |

Possible interpretations:
- *Bloom is intentionally subway-strict because subway is a stronger long-term signal than streetcar.* If so, document this in the BLOOM_FORMULA_TEXT.
- *Bloom's 800m is leftover from when there was no per-mode split (added 2026-05-03)*; should now be 500m to align with the score gate.
- *800m is the "8-minute walk" rule of thumb that gets used in some planning contexts* and was deliberately picked to match.

I don't know which. The CLAUDE.md and steering docs don't say. **Worth a one-line product-decision comment in the code so the next reader doesn't have to reverse-engineer.**

**B. The 500m transit gate looks tighter than reality.**

Bill 185 (Jan 2025: parking minimums eliminated citywide) and the June 2025 sixplex carve-out (T&EY + Ward 23) **do not require transit proximity**. They permit multiplex on parcels regardless of bus/subway/streetcar distance. The permits backtest shows real developers are building multiplexes in transit-poor neighborhoods (Reidmount Ave, North York side streets, etc.).

If BloomTO's positioning is *"the best multiplex sites for net-zero outcomes"* (transit-friendly + walkable), then 500m is correct and the 91% miss rate is fine. If positioning is *"all multiplex-legal sites, ranked"*, then 500m is wildly tight and the wire is missing most of the action.

The page provenance currently says "528K parcels ranked for as-of-right multiplex development" — implying the second framing. The actual gate enforces the first. **Mismatch worth resolving.**

**C. softScore extends the buffer to 1500m but is then re-filtered out by the frontend.**

If the intent is "soft-include suburban parcels for downstream specs," the wire should keep them and the frontend should expose a toggle (it doesn't today). If the intent is "exclude them," the score path should set them to `score=0 AND softScore=0` in the ETL and skip the soft-score work entirely. Right now the work is done and the result is hidden.

## What this gate stack excludes from a 528K-parcel universe

Approximate funnel based on a typical ETL run (numbers will shift on rebuild):

```
528,000 canonical parcels
   - 50,000  no neighborhood / hard skips        (gate 1)
   - 30,000  institutional overlay               (gate 2)
   - 50,000  non-buildable polygons              (gate 3)
=  ~400,000 candidate parcels
   - ~390,000  fail residential, heritage Part IV,
              transit, OR sliver gates           (gates 5-8)
   = ~10,000 score-positive parcels (≈ wire size)
```

So **97% of Toronto's parcels are excluded by the score-zero gates**, and within that 97%, the transit-buffer gate (#7) is the dominant single filter.

## Suggested follow-up

If you want to tighten the gate audit further:

1. **Per-gate exclusion counts** &mdash; instrument the ETL to log how many parcels each gate excluded individually (today most are aggregated into "score == 0"). Tells you which gate is doing the most work.
2. **Soft-score audit** &mdash; verify the 1500m soft buffer is actually used somewhere downstream, not just computed and discarded.
3. **Bloom subway threshold review** &mdash; either align `BLOOM_SUBWAY_M` to 500m (matching score gate) or document why it's 800m.
4. **Page-positioning copy review** &mdash; align the page's "528K parcels ranked" provenance with the actual gate stack so the user understands the wire is "transit-served multiplex candidates," not "all multiplex parcels."
