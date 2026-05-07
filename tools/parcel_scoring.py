"""Parcel scoring constants — solar methodology disclosure only.

2026-05-07 cleanup: this module previously held the BloomTO-synthesised
`score`, `softScore`, and `bloom_flag` formulas. Per the trust-erosion
discussion (every weighted formula papered over data gaps), those have
all been removed from the wire format, the ETL, and the UI. The
remaining content is the SolarTO methodology disclosure — surfaced on
the wire as `meta.solarMethodology` so any consumer can read what
city-sourced inputs feed the (city-published) `solarScore` field.

Stdlib only. Kept as a separate module purely so the disclosure string
isn't buried inside `build_parcels.py`.
"""

# SolarTO upstream rooftop screening — what passes into BloomTO's solarScore.
# Verbatim methodology text the wire ships at meta.solarMethodology so any
# consumer can read what feeds the field without copy drift.
SOLAR_METHODOLOGY_TEXT = (
    "solarScore inherits SolarTO's per-rooftop screening: a roof surface must "
    "receive >=800 kWh/m^2/yr incident solar radiation, have >=30 m^2 of clear "
    "space, slope <45 degrees, and not face north. Toronto yield factor: 1 kW "
    "installed PV generates ~1,150 kWh/yr. BloomTO's solarScore = SolarTO max "
    "rooftop kWh (P95-normalized to 0-100) shadow-adjusted by 3D Massing "
    "neighbor-building modeling."
)
