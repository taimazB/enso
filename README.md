# ENSO — North Pacific SST Anomaly Dashboard

Daily sea-surface-temperature anomalies from **NOAA OISST v2.1**, ingested into ClickHouse
and served as an interactive map plus point/region timeseries.

The archive in `./data/` is a regional subset — **lon 180–270°E, lat 0–90°N** at 0.25°,
1981-09-01 onward, one NetCDF per day. That covers the North Pacific, Gulf of Alaska and
the Bering/Chukchi sector. See [CLAUDE.md](CLAUDE.md) for why this is a North Pacific /
PDO domain rather than an ENSO-index one as currently subset.

## Services

| Service | Description | Port |
|---|---|---|
| `front` | Nuxt 4 + Nuxt UI + MapboxGL + ECharts | http://localhost:9020 |
| `api` | FastAPI | http://localhost:9021 |
| `db-ch` | ClickHouse | 9023 (HTTP), 9024 (native) |
| `process` | NetCDF → ClickHouse ingest | on demand |

## Quick start

```bash
cp .env.example .env.dev          # then fill in NUXT_PUBLIC_MAPBOX_TOKEN and UID/GID
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d

# create the schema and load data
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m OISST.cli init

docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m OISST.cli ingest --limit 365      # or omit --limit for the full archive
```

Then open http://localhost:9020.

> `--env-file .env.dev` is required on every compose command — without it the ports fall
> back to their in-file defaults.

A full-archive ingest is ~16,400 days / ~1.6 billion rows and takes roughly 45 minutes.

## Layout

```
api/        FastAPI service — queries ClickHouse, renders map PNGs
front/      Nuxt 4 frontend (everything under front/app/)
process/    OISST.cli ingest pipeline
shared/     grid geometry (domain.yml) + ClickHouse schema, mounted into api and process
clickhouse/ local ClickHouse volumes and user config
data/       the NetCDF archive (untracked)
```

## API

```bash
curl localhost:9021/health
curl localhost:9021/coverage
curl localhost:9021/domain

curl -X POST localhost:9021/timeseries \
  -H 'content-type: application/json' \
  -d '{"lat": 55.0, "lon": -145.0}'

curl localhost:9021/region/ne_pacific

curl -o day.png localhost:9021/image/1981-09-15.png
```

Full endpoint notes, schema rationale and gotchas: [CLAUDE.md](CLAUDE.md).
