"""Single source of truth for the per-listing cache key shared across
places_cache.json, web_verify_cache.json, llm_cache.json, and
geocode_cache.json.

Canonical format:  "<NAME>||<ADDR1> <ADDR3>"
  - uppercased
  - addr parts joined by a single space
  - leading/trailing whitespace stripped from each part and from the join

This format is LOCKED by the existing prod caches (1000+ entries on the
VPS as of 2026-05-18). Do NOT change it without rewriting every cache
file in lock-step.

Why one helper:
  Five separate scripts used to copy-paste a `cache_key(name, address)`
  function — three of them (geocode_addresses, the dead enrich_places
  main, llm_websites) built the key from a single combined `address`
  field that callers had no consistent way to pass. The result was a
  silent 100% miss rate when those scripts were given `corridors.json`
  entries (which carry only addr1, not addr3). Centralizing forces the
  caller to pass parts explicitly OR to pull `_cacheKey` straight off
  the entry inject_openings.py stashed there.
"""

def cache_key(name, *addr_parts):
    """Build the canonical per-listing cache key.

    Standard call:
        cache_key(name, addr1, addr3)
    For an already-combined "addr1 addr3" string:
        cache_key(name, address_full)
    Falsy parts (None, '') are dropped silently.
    """
    addr = ' '.join(p.strip() for p in addr_parts if p and p.strip())
    return f"{(name or '').strip().upper()}||{addr.upper()}"
