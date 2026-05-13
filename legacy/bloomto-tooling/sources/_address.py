"""Shared address normalizer for cross-source spatial joins.

Lifted byte-equivalent from `tools/sources/heritage.py` 2026-05-04 so both the
Heritage Register loader and the Building Permits loader can canonicalize
addresses through the same closed-set logic. A future heritage-only fix to
one of the abbreviation entries would otherwise silently change permit-join
behavior — the shared module makes both consumers move together by design.

Scope is intentionally minimal (only the normalizer + the abbreviation set);
heritage-domain helpers (KNOWN_STATUSES, more_restrictive, etc.) stay in
heritage.py because they have no analog in permits.
"""

# Closed-set street-type abbreviations. Keys are the expanded forms found in
# Toronto city sources; values are the abbreviated forms used in the Property
# Boundaries `LINEAR_NAME_FULL` field. Tokens outside this set pass through
# unchanged after uppercasing — the join can still match anything that's
# already in the canonical abbreviated form on both sides.
STREET_TYPE_ABBREVIATIONS = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "ROAD": "RD",
    "BOULEVARD": "BLVD",
    "DRIVE": "DR",
    "CRESCENT": "CRES",
    "COURT": "CRT",
    "PLACE": "PL",
    "LANE": "LANE",
    "WAY": "WAY",
    "TRAIL": "TR",
    "TERRACE": "TER",
    "CIRCLE": "CIR",
    "PARKWAY": "PKWY",
}


def normalize_address(text: str) -> str:
    """Canonicalize an address string for cross-source comparison.

    Uppercases, collapses runs of whitespace into single spaces, and replaces
    each whole-word street-type token from `STREET_TYPE_ABBREVIATIONS` with its
    abbreviated form. Returns `""` for falsy input.

    No regex (no backtracking risk; per NFR Performance budget). The closed
    abbreviation set means tokens that are already canonical (`ST`, `AVE`) and
    tokens we don't know about (`RAMP`, `MEWS`) all pass through after
    uppercasing — the join still matches when both sides agree.
    """
    if not text:
        return ""
    tokens = text.upper().split()
    return " ".join(STREET_TYPE_ABBREVIATIONS.get(t, t) for t in tokens)
