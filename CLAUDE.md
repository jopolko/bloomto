# CLAUDE.md

Guidance for Claude Code working in this repository.

## Strict working mode

- Execute first, explain only when asked. No "want me to" or "shall I" trailers.
- One-sentence updates max while building. Show progress through actions, not paragraphs.
- "tmp.png" convention: when the user says tmp.png / screenshot / "check this", immediately Read `/mnt/c/Users/josh/Desktop/tmp.png` without confirming.
- Design references over iteration. When a visual is wrong, ask ONCE for a reference site/style — never iterate blind through 3 versions.
- Match register. No PC softening, no diplomatic vocabulary, match user intensity.
- Don't volunteer caveats unless they affect the decision in front of the user.
- Pivots are pivots. When direction changes, drop the prior thread silently.

## Project

**RootedTO** — a daily-fresh directory of restaurants newly licensed in Toronto, classified by cuisine via Claude Haiku + web_search and surfaced as a single-page, no-build, static-HTML directory. Audience-first framing: an **immigrant looking for the newest Ethiopian / Tamil / Filipino / Salvadoran spot**, not a tourist looking for "ethnic food."

The displacement-mapping framing was the previous iteration; it's been moved off the public surface (the data is still produced by `build_corridors.py` and available in `data/corridors.json`, but the page no longer leads with it).

**Pivot history:** BloomTO (multiplex parcel filtering) → DemoCalcTO (demolition cost benchmarking, never shipped) → RootedTO/corridors (cultural displacement map, 2026-05-12) → RootedTO/openings (now-open directory, 2026-05-13). Prior codebases archived under `legacy/`. Do not import from `legacy/`.

## Architecture

Python ETL → JSON wire file → Apache + vanilla HTML/JS. No backend, no DB, no build step, no React, no Node.

The pipeline:
- `tools/cron_daily_openings.sh` — daily cron entry point on the VPS
- `tools/llm_classify*.py` — name-only cuisine classifier (Haiku, cheap fallback)
- `tools/llm_verify*.py` — web_search verifier (cuisine + operating + website in one Haiku call)
- `tools/check_link_health.py` — HEAD-probes every cached URL; flags 4xx/5xx
- `tools/inject_openings.py` — applies chain denylist, gates to verified-open, writes `data/corridors.json`
- `index.html` — fetches `data/corridors.json`, renders the dropdown + opening feed

`tools/build_corridors.py` is the heavier weekly ETL that still produces corridor stats (closures, dev pressure, heritage gaps) — kept for the legacy data feed but not currently rendered on the page.

## Tagging hierarchy (most authoritative first)

1. **Chain denylist** in `inject_openings.py` (and mirrored in `build_corridors.py`). Substring match — known American/Canadian chains (Popeyes, KFC, Tim Hortons, Boston Pizza, Fat Bastard Burrito, etc.) always return `unknown`. Deterministic, no LLM call.
2. **web_search-informed cuisine** (`web_verify_cache.json`). Haiku reads search results (menus, blogTO articles, Instagram, owner bios) and returns cuisine alongside the operating/website verdict.
3. **Name-only LLM** (`llm_cuisine_cache.json`). Cheap classification when web_search hasn't run yet.
4. **Keyword pattern match**. Last resort, mostly only fires for entries with no LLM cache at all.

`get_cuisine()` in `inject_openings.py` walks these in order. Chain denylist short-circuits everything.

## Self-healing

Each verification cached with `verified_at` timestamp. Re-check intervals by link quality:
- own website (.com/.ca): 180 days
- Instagram / Facebook / TikTok: 30 days
- blogTO / Yelp / Maps / TripAdvisor: 14 days
- no-link yes verdicts: 14 days
- `unclear`: 7 days
- `no` (confirmed closed): 60 days

As Google/Bing index new places, the next cron re-check picks up the better link and upgrades the entry silently.

## Coverage policy

- Only restaurants licensed in the last **365 days** appear. Older licences fall out.
- Only entries that are **verified open** (Places `OPERATIONAL` OR web_search `operating=yes`) appear.
- Chain-denylist matches never appear, regardless of LLM verdict.

## Cuisine taxonomy (~50 keys)

Specific country buckets are preferred over umbrellas. Where a cuisine is meaningfully different from its parent region, it gets its own bucket. Umbrellas get the suffix `(other)` in the dropdown (e.g. "Caribbean (other)" = Bahamian, Bajan, multi-island; "Middle Eastern (other)" = Mediterranean Grill, generic). Full list in `CUISINE_LABEL` dicts in `tools/inject_openings.py`, `tools/build_corridors.py`, and `index.html` — all three must stay in sync.

## Cost model

- Daily steady-state: ~$0.30/day in Anthropic API credit (Haiku tokens + ~10 web_search calls for the daily delta + tier-aware re-checks)
- ~$10/month
- Zero ongoing Google API spend (Places enrichment was a one-time $6 cold-start)

## Secrets

`/var/secrets/rootedto.env` holds `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` (only used historically by `enrich_places.py`), `GITHUB_TOKEN`, plus a few rate-limit / CORS configs. **Never inline, never echo, never commit.** Mode 640, owner `john:www-data`. Local on WSL has the same file; VPS has its own copy at the same path.

## Hosting

- Apache on `joshuaopolko.com`. Prod URL: **https://joshuaopolko.com/rootedto/**
- Prod path: `/var/www/html/rootedto/` (same dir as cron working dir — `cp` deploy step is a no-op)
- VPS: DigitalOcean droplet, San Francisco. SSH `john@143.110.236.86:34522` via `~/.ssh/bloomto_deploy`
- File ownership `john:www-data`. `.htaccess` default-denies everything except the explicit allow-list.
- Crontab on VPS: `17 5 * * * /var/www/html/rootedto/tools/cron_daily_openings.sh` (UTC; runs ~1:17 AM Toronto)

## Survey reference

`data/ckan_survey.md` (CSV/JSON datasets) and `data/ckan_survey_supplement.md` (non-CSV formats) are the structural inventory of every Toronto CKAN dataset. They verify file integrity, not semantic fitness. Always spot-check a dataset before trusting it for a new use case. (Both gitignored; only relevant during ETL development.)

## What NOT to do

- Don't import from `legacy/`. Both predecessor codebases are archived for reference only.
- Don't add a backend, DB, build step, or framework.
- Don't add user-submitted content (reviews, claims, ethnicity self-tagging). The whole moat is that the source-of-truth is the City's licence feed, not user input.
- Don't break the "verified-open only" gate. A licence ≠ an operating restaurant; show only what's confirmed.
- Don't classify Toronto chains as ethnic cuisine (Popeyes is not Caribbean). When a name suggests theme-without-substance, prefer `unknown`.
- Don't hardcode `/home/josh/rootedto` paths in `tools/*.py` — derive from `Path(__file__).resolve().parent.parent` so dev (WSL) and prod (VPS) share the same code.
- Don't commit anything matching `*.env` or anything in `/var/secrets/`.
