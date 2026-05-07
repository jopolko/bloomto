# Nightly signals refresh — VPS setup

`tools/build_signals.py` pulls three CKAN-fresh feeds (severance applications,
demolition permits, property violations), address-joins them to the existing
`data/parcels-top.json` + `data/parcels-broader.json`, and writes a fresh
`data/signals.json` (~40 KB). The frontend reads this file on top of the
parcels data, so the homepage always reflects what changed in the last 24
hours.

This guide sets up the **nightly refresh on the VPS**, leaving the heavy
weekly ETL rebuild on the workstation.

## Prerequisites

The VPS needs:
- Python 3.10+ (3.12 is what the workstation uses)
- The `requests` package — that's the only third-party dep `build_signals.py`
  pulls in. No shapely, no geopandas, no pyproj. Tiny footprint.
- Read+write access to a clone of this repo
- (Optional) Write access to the live web root if you want the script to
  deploy `signals.json` directly into the served path.

## One-time install

```bash
# 1. Pick a home for the repo on the VPS
sudo mkdir -p /opt/bloomto
sudo chown $USER:$USER /opt/bloomto
git clone https://github.com/jopolko/bloomto.git /opt/bloomto

# 2. Light virtualenv (only needs requests)
cd /opt/bloomto
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install requests

# 3. Seed the parcel JSONs from the workstation (one-time):
#    The signals build needs parcels-top.json + parcels-broader.json
#    to do the address-join.
#    From the workstation:
#       scp data/parcels-top.json data/parcels-broader.json \
#           user@vps:/opt/bloomto/data/

# 4. Smoke-test the cron wrapper without deploying
cd /opt/bloomto
./tools/cron_build_signals.sh
tail -20 tools/logs/signals-$(date +%Y%m%d).log
```

You should see `signals.json OK · ~40 KB` and `==== signals refresh done ====`.

## Wire it into cron

Edit your user crontab:

```bash
crontab -e
```

Add the line:

```cron
17 4 * * * /opt/bloomto/tools/cron_build_signals.sh
```

That fires at **04:17 server-local-time daily**. The 17-minute offset is a
small etiquette gesture toward the CKAN API (every cron in the world hits
on the hour) — feel free to change to whatever odd minute you like.

### Optional — auto-deploy to the live web root

If the web server is on the same host (Apache `/var/www/html/bloomto`,
nginx, etc.), the wrapper can `cp` the file straight into the served path:

```cron
17 4 * * * WEB_ROOT=/var/www/html/bloomto /opt/bloomto/tools/cron_build_signals.sh
```

The wrapper does an atomic write (`cp` to a temp file, then `mv` over) so
visitors mid-fetch never see a half-written JSON.

If the VPS hosting BloomTO and the web server are different machines, drop
the `WEB_ROOT=` and add an `rsync` line *after* the wrapper:

```cron
17 4 * * * /opt/bloomto/tools/cron_build_signals.sh && \
           rsync -az /opt/bloomto/data/signals.json deploy@web:/var/www/html/bloomto/data/
```

### Optional — email on failure

cron sends stdout/stderr to the user's mailbox by default. To redirect to
a specific address:

```cron
MAILTO=you@example.com
17 4 * * * /opt/bloomto/tools/cron_build_signals.sh
```

The wrapper exits non-zero on any failure, so cron will mail the log
contents — no extra alerting needed.

## Verifying it ran

After the first cron firing:

```bash
# Did it run?
ls -la /opt/bloomto/tools/logs/

# What did it say?
tail -50 /opt/bloomto/tools/logs/signals-$(date +%Y%m%d).log

# Is the live signals.json fresh?
curl -s -o /tmp/sig.json https://your.domain/bloomto/data/signals.json
python3 -c "import json; d=json.load(open('/tmp/sig.json')); print(d['generatedAt'])"
```

`generatedAt` should be < 24 h old.

## Operational notes

### The lock file

The wrapper uses `flock` against `tools/.signals.lock`. If a previous run
hung, a fresh cron firing exits cleanly with `another signals run is in
progress`. The lock auto-releases when the holding shell exits.

### Log rotation

Logs land at `tools/logs/signals-YYYYMMDD.log`, one file per day. The
wrapper deletes anything older than 14 days. If you'd rather keep more
history, delete the rotation `find` line at the bottom of the script.

### Cache TTL

Each source has a 24h cache TTL (`CACHE_TTL_S = 24 * 3600`). A run within
24h of the previous run will use the local cache and skip the CKAN fetch.
If you want every run to bypass the cache, delete:

```bash
rm -f /opt/bloomto/tools/cache/{coa_active,demo_permits,property_violations}.json
```

before invoking. The wrapper doesn't busy the cache itself — fresh-data
needs are usually `< 24 h` already.

### When the parcels JSONs change

Any time the workstation runs the heavy ETL and produces fresh
`parcels-top.json` + `parcels-broader.json`, push them to the VPS:

```bash
# from the workstation
scp data/parcels-top.json data/parcels-broader.json \
    user@vps:/opt/bloomto/data/
```

The next signals run will join against the new parcel set automatically.
No restart, no re-fetch of the source caches needed.

## Troubleshooting

**`build_signals.py: command not found`** — the wrapper `cd`s into
`BLOOMTO_DIR` first, but make sure `python3` is on the cron's PATH or
that `.venv/bin/python` exists. Cron has a minimal env (`/usr/bin:/bin`),
so don't rely on user-shell paths.

**`Could not find datastore-active …`** — Toronto Open Data sometimes
re-IDs resources without notice. The error means the resource ID our
source-module looks up by name has gone missing. Check the dataset page
on `open.toronto.ca` and update the `RESOURCE_NAME` constant in the
relevant `tools/sources/*.py` file.

**Atomic deploy failed** — `mv` is only atomic if source and destination
are on the same filesystem. If `WEB_ROOT` is on a different mount than
`BLOOMTO_DIR/data`, the wrapper still works but the move briefly becomes
copy-then-delete. Either ensure same-filesystem placement, or accept the
~50ms half-write window (signals.json is small enough that it's nearly
imperceptible).

**`flock: command not found`** — `flock` ships with util-linux. Install
it (`apt install util-linux` on Debian/Ubuntu) or remove the locking
block from the wrapper if you're certain only one run will ever overlap.
