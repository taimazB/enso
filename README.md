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
