# Test fixtures

`neighbourhoods.geojson` — 5-polygon subset of the City of Toronto's official
158 neighborhoods (CKAN package `neighbourhoods`, resource
`0719053b-28b7-48ea-b863-068823a93aaa`). Sampled to span a range of urban form:
high-density downtown (Yonge-Bay Corridor, Regent Park), inner-suburban
(High Park North), low-density estate (Bridle Path-Sunnybrook-York Mills),
and a Tower-in-the-Park hub (Etobicoke City Centre). Geometries copied
verbatim from `tools/cache/neighbourhoods.geojson` — no simplification.

The e2e test (`tools/tests/test_e2e.py`) loads this fixture via
`fetch_neighborhoods(..., expected_count=5)` and feeds synthesized metric dicts
into `assemble_payload()`. Source-module fixtures (XLSX, GTFS ZIP, etc.) are
not committed: each source module's I/O is exercised by the live cache during
`build_neighborhoods.py` runs, while the e2e test pins the orchestrator's
assembly contract independently.
