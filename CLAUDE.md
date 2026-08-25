# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Modelled on the `ocean-acidification-dashboard` project next door — same four-service
compose shape (`front` / `api` / `db-ch` / `process`), same ClickHouse-as-sole-database
approach, same conventions for env files and Dockerfiles. Where this project differs,
it is noted below.

## Services & Ports

Host ports come from `docker-compose.dev.yml`'s `${VAR:-default}` fallbacks, overridden by
`.env.dev`. Always start with `--env-file .env.dev` (see gotcha below) — these are the
ports you'll actually hit:

| Service | Description | Port |
|---|---|---|
| `front` | Nuxt 4 frontend | 9020 |
| `api` | FastAPI backend | 9021 |
| `db-ch` | ClickHouse | 9023 (HTTP), 9024 (native) |
| `process` | NetCDF → ClickHouse ingest | — |

Ports are deliberately offset from the ocean-acidification-dashboard's 9010–9014 so both
stacks can run at once.

## Common Commands

**Start dev environment:**
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d
```
Without `--env-file .env.dev`, compose falls back to the in-file defaults (front 3000,
api 4000) and can recreate dependent services on the wrong ports.

**Ingest CLI** (the `process` service has no long-running work; it idles on `sleep infinity`
and is driven on demand):
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m OISST.cli <command>

python -m OISST.cli init                                    # create database + tables
python -m OISST.cli scan   [--limit N]                      # disk vs. already ingested
python -m OISST.cli ingest [--date|--start|--end] [--limit N] [--force] [--batch N]
python -m OISST.cli status                                  # per-status day/row counts
```
A full-archive load is `... run --rm process python -m OISST.cli ingest` with no
filters. Measured rate: ~30 days (2.9M rows) in ~4 s, so ~16,400 days ≈ 45 min for
~1.6 billion rows.

**ClickHouse client:**
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev exec db-ch \
  clickhouse-client --database enso --query "SHOW TABLES"
```

**Frontend (outside Docker):**
```bash
cd front && npm install && npm run dev
```

## Architecture

### Data Source

**NOAA OISST v2.1** (`oisst-avhrr-v02r01.YYYYMMDD.nc`), one file per day in `./data/`,
mounted at `/opt/data/` in the `api` and `process` containers. 1981-09-01 onward.

Each file is a **regional subset** cut with `cdo -sellonlatbox,-180,-90,0,90` and reduced
to the single `anom` variable — daily SST anomaly in °C, encoded as `short` counts of
0.01 with `_FillValue = -999` over land. The box is **lon 180.125–269.875°E, lat
0.125–89.875°N**, 360×360 cells at 0.25°: the North Pacific and the Alaskan/Arctic
sector, ~96,000 valid ocean cells per day out of 129,600.

Files for the most recent ~2 weeks are named `*_preliminary.nc`; NCEI replaces them with
final versions later. `config.scan()` prefers the final file when both exist for a date,
and `status.is_current()` compares recorded size+mtime so a superseded preliminary file is
correctly seen as stale and re-ingested.

**The repo is named `enso`, but this box is clipped at the equator.** Niño 3.4 (5°S–5°N)
is only half covered and Niño 3/4/1+2 are largely outside it, so real ENSO indices are
*not* computable from the current archive — this is a North Pacific / PDO / marine-heatwave
domain as it stands. `domain.yml` carries a `nino34` region marked `partial: true` to keep
that visible rather than silently wrong. Widening means re-subsetting with a bigger
`sellonlatbox` and updating `domain.yml`'s `subset` block — **no schema change and no
re-ingest of existing days**, by design (see below).

### `shared/` — the contract between `api` and `process`

Both containers mount `./shared` at `/app/shared`. Two modules:

- **`domain.py` + `domain.yml`** — grid geometry, variable metadata (scale factor, fill
  value, colormap bounds), and named region boxes. Describes **two** grids and the
  distinction matters: `global` is the full 1440×720 OISST grid, `subset` is the box the
  files on disk actually cover.
- **`ch.py`** — the ClickHouse client factory and the **single definition of the schema**
  (`DDL`, applied idempotently by `ensure_schema()`). There is no `.sql` file; keeping the
  DDL in one Python constant is what stops `api` and `process` drifting apart.

### ClickHouse (`db-ch`, database `enso`)

**`sst_anom`** — one row per (date, ocean cell).

```sql
date      Date    CODEC(DoubleDelta, ZSTD(3))
gy        UInt16  -- global OISST row index, 0..719
gx        UInt16  -- global OISST column index, 0..1439
anom_raw  Int16   -- raw source counts, 0.01 degC
anom      Float32 ALIAS anom_raw * 0.01
lat       Float32 ALIAS -89.875 + gy * 0.25
lon       Float32 ALIAS 0.125 + gx * 0.25
PROJECTION by_date (SELECT date, gy, gx, anom_raw ORDER BY (date, gy, gx))
ENGINE = MergeTree PARTITION BY toYear(date) ORDER BY (gy, gx, date)
```

Three decisions worth not undoing:

1. **`gy`/`gx` index the global grid, not the subset.** A cell's identity therefore does
   not depend on the current `sellonlatbox`. Widen the subset later and old rows stay
   valid; the only thing that changes is `domain.yml`'s `subset` block. Converting
   lat/lon ↔ indices goes through `shared.domain.global_grid()` — never hand-roll it.
2. **`anom_raw` is the source's own Int16, with an ALIAS doing the ×0.01.** Lossless,
   2 bytes, and compresses far better than Float32. ALIAS columns cost no storage, so
   queries just say `anom` / `lat` / `lon` and read naturally.
3. **`ORDER BY (gy, gx, date)` with a `by_date` projection**, not two tables. A point
   timeseries (~16k rows out of ~1.6e9) is the query that is unaffordable any other way,
   so it owns the primary key; whole-day map reads want the opposite order and get the
   projection, which ClickHouse maintains on insert and selects automatically.

**`ingest_status`** — `ReplacingMergeTree(updated_at) ORDER BY date`, one row per day
carrying `status`, `filename`, `n_rows`, `file_mtime`, `file_size`. Reads use `FINAL`
(the table is tiny). Statuses: `pending_ingest → ingesting → success_ingest`, plus
`failed_ingest`. This is the same state-machine idea as the reference project's
`SalishSeaCast_status`, minus the download/compute/image stages that do not exist here yet.

**Re-ingesting a day is deliberately expensive.** `sst_anom` is a plain MergeTree, so
`ingest.delete_day()` issues `ALTER TABLE ... DELETE`, a mutation that rewrites the
affected parts of that day's *year* partition. That is fine because it only happens when
a preliminary file is superseded — roughly the last two weeks of the archive. Do not
"optimise" this into `ReplacingMergeTree`: dedup would then require `FINAL` on a
1.6-billion-row table for every read.

### Process pipeline (`process/`)

Entry point `process/OISST/cli.py` (`python -m OISST.cli`). Modules:
- `config.py` — filename parsing, `scan()` (final beats preliminary per date)
- `ingest.py` — NetCDF → ClickHouse; `read_day()` reads **raw shorts**
  (`set_auto_maskandscale(False)`) rather than netCDF4's masked floats
- `status.py` — the `ingest_status` table

Inserts are **batched across days** (`--batch`, default 30). ~96k rows/day means
per-file inserts would create ~16k tiny parts on a full load; ClickHouse merges are much
happier with fewer, larger ones.

**There is no `download` command yet** — the archive in `./data` is loaded as-is (a
deliberate scoping call). When NCEI fetching is added it becomes a step ahead of `ingest`
with the same per-date status rows advancing through `pending_download → ... → success_ingest`.

### API (`api/`)

FastAPI in `SERVER.py`. Reads live from ClickHouse; NetCDF files are the ingest service's
business, not the API's.

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + ClickHouse reachability |
| `GET /domain` | grid extent, image bounds, variable metadata, colour stops, region list — the frontend's bootstrap call |
| `GET /coverage` | ingested date range and row count |
| `GET /variables` | variable list |
| `POST /timeseries` | `{lat, lon, start?, end?, period?}` → full record at the nearest cell |
| `POST /regionTimeseries` | `{lat: [a,b], lon: [a,b], ...}` → area-mean over an arbitrary box |
| `GET /region/{key}` | same, for a named `domain.yml` region |
| `GET /image/{date}.png` | one day (or week/month) as a Web-Mercator PNG |

**`period` — `daily` (default) / `weekly` / `monthly`** — is accepted by every timeseries
endpoint and by `/image`, and its buckets are defined once in `api/modules/periods.py`:
weeks start on **Monday** (`toMonday`), months are calendar months, and a bucket is always
labelled by its **first day**. Both the query side (`bucket_sql()`, a GROUP BY expression)
and the render side (`span()`, a date range) come from that module, which is what makes a
chart point and the map frame for the same date cover exactly the same days. The frontend
mirrors the same arithmetic in `front/app/utils/periods.ts` — **change one and change the
other.** Buckets at the edges of the archive are simply short.

**Area means are cos(latitude)-weighted** (`sum(anom*cos(lat)) / sum(cos(lat))`). At 60°N a
0.25° cell covers half the area of one at the equator, so a plain `avg()` over-weights the
poleward end of any box tall enough to matter — which every box here is.

**Out-of-domain points** raise `OutsideDomainError`, rendered as a **400 carrying both a
plain-string `detail` and a structured `error` object** (`code: "outside_domain"`,
`requested`, `domain`). `detail` stays a sentence so callers that just print it keep
working; `error.code` is what lets the frontend show this as an informational empty state
rather than a red failure (`stores/main.ts`'s `selectPoint` catch).

### Map imagery (`api/modules/render.py`)

There is **no tile pyramid**. At 360×360 cells the whole domain is one modest PNG, served
as a Mapbox `image` source with corner coordinates from `/domain`'s `imageBounds`. A
pyramid can be added behind the same URL shape later if zoom demands it.

The source grid is linear in longitude but **not** in Mercator y, so `to_mercator()`
resamples the rows onto an evenly spaced Mercator axis. Handing Mapbox the raw array
instead would stretch the field increasingly toward the pole — in a domain reaching 90°N
that is a gross error, not a rounding one. Two consequences:
- **Web Mercator cannot represent beyond ~85.05°N**, so the top ~5° of the domain is
  clipped out of the rendered image. `render.bounds()` reports what actually made it in.
- Linear blending propagates NaN from either neighbour, which would erode a pixel of ocean
  along every coastline; `to_mercator()` falls back to whichever side is real.

Land (`_FillValue`) renders **fully transparent**, not white — the dark basemap shows
through. Rendered PNGs are cached under `OISST_IMAGE_DIR`
(`./data/images/anom/{period}/YYYY/{bucket-start}_w{width}.png`); `?nocache=true` forces a
re-render. A **partial** bucket — a week or month still filling up at the head of the
archive — is rendered but deliberately *not* cached, or it would freeze at a mean over the
handful of days that happened to be ingested first.

**Colour range lives in `domain.yml`** (`vmin`/`vmax`, currently ±3 °C with `RdBu_r`).
Daily values reach about ±5 at the extremes, but saturating at ±3 is what makes an ordinary
day readable rather than uniformly pale. Change it there and both the rendered PNGs and the
frontend legend follow.

### Frontend (`front/`)

Nuxt 4 + Nuxt UI v4 (Tailwind v4) + Pinia + MapboxGL + ECharts, dark mode pinned — the
same stack as the ocean-acidification dashboard.

```
app/app.vue                        header + coverage badge; awaits store.loadMetadata()
app/pages/index.vue                map above, point chart rail below
app/components/AnomalyMap.vue      MapboxGL + the anomaly image source
app/components/TimeControl.vue     period toggle + date stepper (±1 bucket, ±1 year)
app/components/ColorLegend.vue     gradient built from /domain's colorStops
app/components/TimeseriesChart.vue ECharts line with dataZoom
app/composables/useApi.ts          axios wrapper
app/utils/periods.ts               daily/weekly/monthly bucket maths (mirrors the API)
app/stores/main.ts                 Pinia store
```

**The chart rail is point-only, and the map click is the only selection.** There was a
`Point | Region mean` tab pair here; the region side is gone from the UI, though
`/region/{key}` and `/regionTimeseries` still exist and still take `period`.

**`store.selectedDate` is always a bucket start**, snapped through `store.setDate()` /
`store.setPeriod()` — never assign it directly. `store.period` drives the chart request and
the image URL together, so switching to Weekly or Monthly re-renders both.

**Clicking the chart sets the map date** (as in the ocean-acidification dashboard). The
line is drawn with `showSymbol: false` + `large`, so there is nothing for ECharts' own
`'click'` to hit — the handler sits on the ZRender canvas, converts the raw pixel with
`convertFromPixel`, and snaps to the nearest date *in the series*, which guarantees the
emitted value is a real bucket inside coverage. An amber `MAP` markLine shows which
bucket the map is on, and it is merged in (`setOption` without `notMerge`) rather than
re-rendered, because a full re-render would reset the `dataZoom` window on every click.
`TimeseriesChart` stays presentational: it emits `select`, and `index.vue` calls
`store.setDate()`.

**Everything lives under `app/`** — Nuxt 4's `srcDir`. A top-level `front/composables/`
is *not* picked up and `~/composables/...` will fail to resolve (the ocean-acidification
dashboard has one at the top level; do not copy that layout here).

**SSR vs. browser base URLs.** `useApi()` uses two: during SSR the Nitro server is inside
the compose network and must reach the API by service name (`API_INTERNAL_BASE_URL`,
`http://api:4000`), while anything the browser fetches — including the image URLs handed
to Mapbox — must use the published `NUXT_PUBLIC_API_BASE_URL` (`http://localhost:9021`).
Getting this wrong surfaces as `ECONNREFUSED ::1:9021` from the Nitro server.

**The map opens on lat 0–68°N, not the full domain.** In Mercator the 70–90°N strip is
taller than everything below it, so fitting the whole box opens at a near-global zoom with
the interesting mid-latitude Pacific reduced to a sliver (`INITIAL_NORTH` in
`AnomalyMap.vue`). The raster still covers the full domain when the user zooms out.

Stepping days calls `ImageSource.updateImage()` rather than removing and re-adding the
layer, so the basemap does not flash between frames.

**Charts**: always ECharts, never a hand-rolled `<canvas>`. The point series is ~16k daily
values; `dataZoom` is what makes that browsable.

## Gotchas

- **`--env-file .env.dev` is required** on every compose invocation, as above.
- **Env vars are baked in at container creation.** Editing `.env.dev` does not affect a
  running container — use `up -d --force-recreate <service>`, not `restart`.
- **`domain.yml` changes need an API restart.** `shared.domain` caches the parsed YAML with
  `lru_cache`, and uvicorn's `--reload` only watches `.py` files, so a colour-range or
  region edit is invisible until the container is restarted. Cached PNGs under
  `./data/images` also survive it — delete them or pass `?nocache=true`.
- **`UID`/`GID` in `.env.dev`** are what stop `api` writing root-owned cache files into the
  bind-mounted `./data`. Set them to your own `id -u` / `id -g`.
- **The `process` venv lives at `/opt/venv`, not `/app/.venv`** — compose bind-mounts
  `./process` over `/app`, which would hide anything installed under it.
- Rendered images are **cached by (period, bucket start, width)**; a request with a different
  `width` is a separate render and a separate file. The cache layout gained the `{period}`
  level, so any PNGs left directly under `./data/images/anom/YYYY/` are from the old layout
  and are dead — `mv`ing those year directories into `./data/images/anom/daily/` reclaims them.
- **Nuxt UI v4 renamed `UButtonGroup` to `UFieldGroup`**, and the old name resolves to an
  empty comment node instead of erroring — the control simply vanishes from the DOM.
- **The API's ClickHouse client is per-*thread*, not per-process**
  (`modules/clickhouse_helpers.py`). Every endpoint is a sync `def`, so FastAPI runs it in
  the thread pool, and a client's session refuses a second concurrent query with
  `ProgrammingError: Attempt to execute concurrent queries within the same session`. A
  shared singleton fails ~7 of 8 overlapping requests. Symptom to recognise: intermittent
  500s under no real load, and a **map that silently keeps showing the previous frame** —
  Mapbox's `ImageSource` never retries a failed image and logs the error only to the
  browser console.

## Status / not yet built

Verified working end to end: schema creation, ingest (round-trip checked cell-by-cell
against the source NetCDF), the ingest status/skip/force logic, every API endpoint at all
three periods (weekly/monthly means cross-checked against the mean of their own daily
values), PNG rendering, and SSR of the frontend page.

**Not yet verified in a real browser**: the Mapbox raster overlay, the ECharts point chart,
and the period toggle's effect on them — the dev machine used for the initial build had no
GPU, and Mapbox's style load never completes under software WebGL, so `map.on('load')` never
fires there. The code path is reached correctly up to that point.

Not built yet: NCEI downloader, region-aggregate tables (`region_daily`), climatology /
marine-heatwave analytics, tile pyramid, production compose files, PostHog analytics,
tests.
