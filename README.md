# Pacific Sea Surface Temperature

Daily sea-surface temperature, anomaly and marine-heatwave category for the Pacific basin,
from **NOAA Coral Reef Watch**, ingested into ClickHouse and served as an interactive map
plus point and region timeseries.

**0.05° resolution, 1985-01-01 onward**, one NetCDF per day in `./data/sst/`. The ingested box
is **60°S–65°N, 100°E–290°E** — 7.5 million ocean cells per day. That covers the Coral
Triangle, the full tropical Pacific, the Blob and PDO domains, the Bering Sea, and the
Antarctic Circumpolar Current at Pacific longitudes.

**Anomaly is derived, not shipped.** CoralTemp provides SST only; the anomaly is computed
against a separate 366-file **1991–2020 daily climatology** in `./data/climatology/`, one per
day-of-year including 29 February. All four Niño indices (1+2, 3, 3.4, 4) fall inside the
domain.

**Marine heatwave category is a second daily product**, NOAA CRW MHW v1.0.1, in
`./data/MHW/` — the same grid and the same days. Both publish at roughly one day's
latency, MHW landing about 90 minutes after CoralTemp.
It is an ordinal class from 1 (Moderate) to 5 (Beyond Extreme), drawn in NOAA's own
palette; land, ice and heatwave-free ocean are all transparent, and only category ≥ 1 is
stored.

## Services

| Service | Description | Port |
|---|---|---|
| `front` | Nuxt 4 + Nuxt UI + MapboxGL + ECharts | http://localhost:9020 |
| `api` | FastAPI | http://localhost:9021 |
| `db-ch` | ClickHouse | 9023 (HTTP), 9024 (native) |
| `process` | download → ingest → render pipeline | on demand |

## Quick start

```bash
cp .env.example .env.dev          # then fill in NUXT_PUBLIC_MAPBOX_TOKEN and UID/GID
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d

# schema, plus the 366-file climatology and the per-region climatology means
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m CRW.cli init

# ingest whatever is already in ./data (newest first)
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m CRW.cli backfill --reverse

# render the image cache — must run while the NetCDF is still on disk
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm --no-deps process \
  python -m CRW.cli render --workers 12
```

Then open http://localhost:9020.

> `--env-file .env.dev` is required on every compose command — without it the ports fall
> back to their in-file defaults.

### Daily updates

```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m CRW.cli run
```

Downloads, ingests and renders every day from the last ingested through yesterday, then
re-checks the recent tail for files CoralTemp has revised in place. Safe to run from cron;
a date that is not published yet is a no-op, not a failure.

### Repartitioning (one-off)

Both daily tables partition **by decade, and by year from 2024 on** — not by year
throughout. `ORDER BY (gy, gx, date)` already makes one cell's whole history a contiguous
key range, and a partition key cuts that range into a piece per partition. The cost is not
bytes (a point query reads ~8.5 MiB either way) but **file opens**, ~4 per selected part,
and a file open is ~50 us on NVMe against ~4.3 ms on a spinning disk.

Measured on the production box, one cold cell over the full archive: **196 parts, ~790 file
opens, 3.4 s**. The new key takes that to 8 parts and ~32 opens, which the same arithmetic
puts near 0.15 s — a prediction until the migration has actually run. A second query on the
same cell is ~0.18 s either way, which is why this only ever showed up as *the first click
is slow*.

Editing the DDL does nothing to a database that already holds the archive
(`ensure_schema()` is `CREATE TABLE IF NOT EXISTS`), so there is a migration. Start with the
plan — it is read-only and free:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  run --rm --no-deps process python -m CRW.cli repartition --dry-run
```

Three things to know before running it for real:

- **Take the site down first.** The migration moves partitions out of the daily tables
  before putting them back, and nothing the frontend reads can see that — `/coverage`'s
  `mhw.complete` gate is computed from the status tables, which the migration never
  touches. So the dashboard stays up and reports a confident **category 0** for every year
  currently in flight. `docker-compose.prod.yml` carries a general `maintenance` profile
  for any planned outage — a stock nginx that takes over `front`'s and `api`'s ports and
  answers **503**, never 200, which crawlers index and uptime monitors read as healthy.
- **Forward only.** Source partitions are dropped as each one lands, so the run resumes at
  any partition boundary but cannot be abandoned half-way.
- **Run it detached** — `run -d --name ...`, then `docker logs -f`. A `docker compose run`
  container outlives the client that started it, so closing a terminal does not stop the
  migration; starting a second one gives two concurrent runs, which silently duplicate rows
  that every per-partition count still agrees with. Guarded, but not worth meeting.

It never needs room for a second copy of the table: it copies one partition and drops the
source before taking the next, so occupancy stays flat with a one-partition bulge. Full
runbook and rationale in [CLAUDE.md](CLAUDE.md).

### Scale

| | |
|---|---|
| ocean cells/day | 7,477,923 (96.8% with a climatology) |
| daily archive | ~113.7 B rows, ~85 GB |
| marine heatwave | ~24.2 B rows (only category ≥ 1 is stored; 1.59 M cells/day) |
| climatology | 2.68 B rows, 2.2 GB |
| NetCDF on disk | ~153 GB SST + ~9.7 GB MHW for 15,213 days |
| images | ~54k WebPs (3 variables × 3 periods) |

## Layout

```
api/         FastAPI service — queries ClickHouse, serves the image cache
front/       Nuxt 4 frontend (everything under front/app/)
process/     CRW.cli download / ingest / render pipeline
shared/      grid geometry, NetCDF reading, rendering, schema — mounted into api and process
clickhouse/  local ClickHouse volumes and user config
deploy/maintenance/  the 503 page and nginx conf served during any planned outage
data/sst/          the daily SST NetCDF archive, pruned to a retention window (untracked)
data/MHW/          the daily marine-heatwave archive, pruned the same way (untracked)
data/climatology/  the 366-file 1991-2020 daily climatology, kept forever (untracked)
data/images/       the rendered image cache (untracked)
```

## API

```bash
curl localhost:9021/health
curl localhost:9021/coverage
curl localhost:9021/domain

# variable is sst (default), anom or mhw; period is daily / weekly / monthly
curl -X POST localhost:9021/timeseries \
  -H 'content-type: application/json' \
  -d '{"lat": 0.0, "lon": 200.0, "variable": "anom", "period": "monthly"}'

curl "localhost:9021/region/nino34?variable=anom&period=monthly"

curl -o day.webp "localhost:9021/image/2026-08-24.webp?variable=anom&period=weekly"
```

`mhw` buckets differently from the other two, and deliberately: a point and the map take
the **max** category over a week or month (a category's mean is not a category), while a
region takes the mean of its daily area means, and `/monthlyRanking` ranks a month by its
mean daily category.

Full endpoint notes, schema rationale and gotchas: [CLAUDE.md](CLAUDE.md).

> Two conventions in this codebase are load-bearing and fail silently if broken: the
> longitude roll onto a 0–360 grid, and the north-up→south-up flip of the climatology
> files. Both live in `shared/fields.py`. See CLAUDE.md before touching either.
