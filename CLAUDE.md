# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Modelled on the `ocean-acidification-dashboard` project next door — same four-service
compose shape (`front` / `api` / `db-ch` / `process`), same ClickHouse-as-sole-database
approach, same conventions for env files and Dockerfiles. Where this project differs,
it is noted below.

**This project used to run on NOAA OISST v2.1 (0.25°, global, SST + shipped anomaly).**
It does not any more — that source is retired, its tables dropped and its files deleted.
Anything describing a 0.25° grid, an `sst_anom` table, a `by_date` projection, or a
`_preliminary`/final download lifecycle is from that era. The OISST work is preserved on
the `wip/oisst-global` branch and is not intended to merge.

## Services & Ports

Host ports come from `docker-compose.dev.yml`'s `${VAR:-default}` fallbacks, overridden by
`.env.dev`. Always start with `--env-file .env.dev` (see gotcha below) — these are the
ports you'll actually hit:

| Service | Description | Port |
|---|---|---|
| `front` | Nuxt 4 frontend | 9020 |
| `api` | FastAPI backend | 9021 |
| `db-ch` | ClickHouse | 9023 (HTTP), 9024 (native) |
| `process` | NetCDF → ClickHouse ingest + image rendering | — |

Ports are deliberately offset from the ocean-acidification-dashboard's 9010–9014 so both
stacks can run at once.

## Common Commands

**Start dev environment:**
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d
```
Without `--env-file .env.dev`, compose falls back to the in-file defaults (front 3000,
api 4000) and can recreate dependent services on the wrong ports.

**Start prod environment:**
```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```
`docker-compose.prod.yml` carries its own header comment explaining every divergence from
the dev file; `.env.prod.example` is the template. The five that matter:

- **`name: enso-prod`.** Both compose files would otherwise take the project name `enso`
  from the directory and clobber each other's containers, network and volumes.
- **No source bind mounts, and `./shared` is mounted nowhere.** The images carry `api/`,
  `process/` and `shared/` as built, so a deploy is `--build`. Mounting `shared` would let
  the host tree diverge from the code the image was tested with, which is the one thing the
  "single definition" design of that directory exists to prevent.
- **`DATA_DIR` / `CH_DATA_DIR` / `CH_LOG_DIR` replace `./data` and `./clickhouse`.** The
  archive is ~163 GB and ClickHouse another ~100 GB; neither belongs in the checkout.
  **Create them and `chown` them to `UID:GID` before the first `up`** — Docker creates a
  missing bind source as *root*, and that surfaces as an empty archive rather than as a
  permission error. `api` gets `/opt/data` **read-only**, since only `process` writes the
  image cache.
- **`db-ch` publishes no host ports and requires `CLICKHOUSE_PASSWORD`.** Verified: the
  password is enforced, `enso` is created, the healthcheck passes, and
  `/health` reports `clickhouse: ok` through it. Do **not** mount `./clickhouse/users.d`
  here — the image's entrypoint writes `users.d/default-user.xml` itself, so a `:ro` mount
  stops the container starting, and the file it generates already grants `default` the
  `::/0` networks that dev's `allow_docker_network.xml` is there for.
- **`process` sits behind the `tools` profile**, so `up -d` starts three services and not a
  fourth idling on `sleep infinity`. Drive it the same way as dev, which is also the shape
  a cron entry wants:
  ```bash
  docker compose -f docker-compose.prod.yml --env-file .env.prod \
    run --rm process python -m CRW.cli run
  ```

`api` runs without `--reload` (it would watch source that is no longer mounted) on
`--workers ${API_WORKERS:-4}` — separate *processes*, so the per-thread ClickHouse client
gotcha below is unaffected by the count. `front` runs the built Nitro entry
(`node .output/server/index.mjs`) rather than `npm run start`, which is `nuxt preview`, a
dev wrapper that re-reads `.env` and needs the full nuxt devDependency tree at runtime.

**Pipeline CLI** (the `process` service idles on `sleep infinity` and is driven on demand):
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m CRW.cli <command>

python -m CRW.cli init                                    # tables + climatology + region means
python -m CRW.cli verify-clim                             # full-read all 366 climatology files
python -m CRW.cli scan     [--limit N]                    # disk vs. already ingested, per archive
python -m CRW.cli backfill [--start|--end] [--product sst|mhw] [--reverse] [--fresh] [--delete-nc]
python -m CRW.cli render   [--start|--end] [--variable|--period] [--workers N] [--force]
python -m CRW.cli rollup   [--start|--end] [--region KEY] [--fresh]   # region_daily
python -m CRW.cli run      [--date] [--keep-nc] [--recheck-days N]
python -m CRW.cli status                                  # per-status day/row counts, per archive
```

**There are two daily archives and every command covers both by default.** CoralTemp SST
and the Marine Heatwave category are separate products with separate URLs, separate
directories, separate tables and separate status bookkeeping — `--product` narrows
`backfill` to one, and `--fresh` then truncates only that one's tables. `run` walks both
per date and treats each independently, because MHW is published about 90 minutes after
CoralTemp and a run landing between the two sees a date as SST-ingested and MHW-pending.

`init` is not just DDL — it loads all 366 climatology files (2.68 B rows, ~20 min) and
then builds `region_clim`. It is idempotent and resumable: an interrupted load skips the
MMDD keys already present.

**ClickHouse client:**
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev exec db-ch \
  clickhouse-client --database enso --query "SHOW TABLES"
```

**Build the region rollup** (after both archives are ingested, not during):
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm --no-deps process \
  python -m CRW.cli rollup --fresh
```
`backfill` deliberately does not maintain `region_daily` per date — one pass per region
over the whole range reduces the big tables once, where a per-date hook would re-run eight
aggregations 15,212 times. Same split as `render`. `run` is the exception and appends the
single date it just ingested. **Roll up only once `mhw_daily` is complete**: `mean_mhw`
divides a sparse numerator by an `sst_daily` denominator, so a partial MHW archive freezes
confident zeros into the rollup, where they are harder to spot than in the sparse table.

**Render the image cache in bulk:**
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm --no-deps process \
  python -m CRW.cli render --workers 12
```
**This reads NetCDF, not ClickHouse**, so it has a hard prerequisite: the daily archive
must still be on disk. Run it before `backfill --delete-nc` and before the daily retention
prune has eaten the range. Afterwards the source for those frames is gone.

**This now applies to `./data/MHW/` as well, and that is a live trap rather than a
theoretical one.** `imaging.prune()` walks both archives, so a single `CRW.cli run` without
`--keep-nc` deletes the whole MHW archive back to the open week — and if the MHW render
pass has not been done, every historical MHW frame becomes unrecoverable without
re-downloading 9.7 GB. **Order is: backfill MHW → `render --variable mhw` → only then let
`run` prune.** Until that render has finished, pass `--keep-nc`.

`render` and `run` are the same code path — both go through `shared.buckets.bucket_field()`,
so a change to how a week is reduced cannot apply to one and not the other. **The API's
on-demand render goes through it too**: that used to be a second copy in
`api/modules/render.py`, and it would have kept averaging the MHW category, which is
reduced by max. `run` renders
the date it just ingested; `render` walks history in a `spawn` pool. It touches neither
ClickHouse nor the network (hence `--no-deps`), so it is safe to run against a
half-finished backfill — though the two contend for the same `./data` mount, ingest being
disk-bound and rendering CPU-bound.

Only **closed** buckets are written: a week or month whose last day is past the end of
the archive is still filling, and caching it would freeze a mean over however many days
happen to be present. `run` rewrites those daily until they close.

**Frontend (outside Docker):**
```bash
cd front && npm install && npm run dev
```

## Architecture

### Data Source

**NOAA Coral Reef Watch CoralTemp v3.1** (`coraltemp_v3.1_YYYYMMDD.nc`), one file per day
in `./data/sst/`, mounted at `/opt/data/sst/`. 1985-01-01 onward, ~10 MB each, ~153 GB for the
full archive.

Global 7200×3600 grid at **0.05°**. The variable taken is `analysed_sst` — `short` counts
of 0.01 °C with `_FillValue = -32768`. (`sea_ice_fraction` is also in the file and is not
ingested.) **There is no anomaly in the product**; it is derived, see below.

#### The second daily product: Marine Heatwave category

**NOAA CRW Marine Heatwave v1.0.1** (`noaa-crw_mhw_v1.0.1_category_YYYYMMDD.nc`), one file
per day in `./data/MHW/`, mounted at `/opt/data/MHW/`. Same 1985-01-01 onward, ~640 KB
each, 9.7 GB for the full archive.

**A different product suite, not a sibling of `sst/` under the CoralTemp tree** — there is
no `mhw/` beside it. It is derived from CoralTemp v3.1 but versioned and published
separately, at `.../crw/data/marine_heatwave/v1.0.1/category/nc/{YYYY}/`.

**Both products publish at roughly one day's latency, and MHW lands about 90 minutes after
CoralTemp** — measured by HEAD, 2026-08-27 carried `Last-Modified` 14:52 UTC for SST and
15:20 UTC for MHW. So the gap is intra-day, not a day: a `run` scheduled in that window
sees SST published and MHW not. That is why the two are tracked and processed
independently rather than paired, and why the catch-up watermark is the earlier of the
two.

The variable is `heatwave_category`. Same 7200×3600 grid, **same latitude orientation as
the dailies** (`lat[0] = -89.975`, so no flip), same longitude roll. Verified: the box's
land mask matches `sst_daily` exactly at 7,477,923 ocean cells. (NOAA's own browse PNGs for
this product *are* north-up — a trap if the palette is ever re-derived from one.)

Four states, and only one of them is drawn:

| code | meaning | stored? | drawn? |
|---|---|---|---|
| -127 | land | no | transparent |
| -1 | ice | no | transparent |
| 0 | ocean, no heatwave | no | transparent |
| 1..5 | Moderate / Strong / Severe / Extreme / Beyond extreme | yes | NOAA's palette |

**NOAA re-encoded this variable on 2024-07-01, mid-archive, without changing the filename,
the `v1.0.1` version string or the URL.** The table above is the *old* encoding; land and
ice are no longer distinguishable in the file at all:

| | dtype | `_FillValue` | `valid_min` | land | ice |
|---|---|---|---|---|---|
| …2024-06-30 | `int8` | −127 | −2 | −127 | −1 |
| 2024-07-01… | `uint8` | **251** | 0 | 251 | 251 |

**`shared/fields.py`'s `mhw_valid_mask` therefore bounds the category range at both ends —
`1 <= raw <= 5` — and must keep doing so.** The obvious `raw >= 1` is correct on the first
encoding and catastrophic on the second, because 251 is a *positive* number: every land and
ice cell passes as a heatwave four times worse than Cat 5.

**Nothing about that failure is loud**, which is the part worth remembering. The ingest ran
clean, `/coverage` reported the archive complete, `mhw_daily` filled with 1.7 billion rows
of `cat = 251` (787 days, ~46% of the table), and every rendered frame from that date drew
land at Cat 5. It surfaced only as a region area mean of **62** on a 1–5 scale — which the
chart could not draw at all, because its y-axis is pinned to 0..5.

Testing `<= 5` rather than the file's own `_FillValue` is deliberate: 1..5 is fixed by the
product definition — it is the five names in the legend — so the rule survives both
encodings and whatever NOAA ships next. The fill value is what changed; the categories are
what did not.

**The climatology is a second archive**: 366 files in `./data/climatology/`, mounted at
`/opt/data/climatology/`, one per MMDD **including `day0229`** — so there is no leap-day
mapping rule to invent. Baseline 1991–2020. 1.6 GB, static, and **kept forever**: image
rendering reads it straight off disk.

They come from a **different tree than the dailies** — not `5km/v3.1_op/`, whose
`climatology/` holds only one combined file on the older baseline:

```
.../crw/data/5km/v3.1-clim19912020-v1/climatology/nc/
    ct5km_v3.1_clim-sst-mean-daily-window-01day-01grid-source19912020_day{MMDD}.nc
```

There is no download code for them; they are fetched once by hand. **"Kept forever" is not
the same as "safe"** — see the bit-rot gotcha below.

#### Two orientation conventions, both of which fail silently

`shared/fields.py` is the only place either is applied. Both produce output that looks
entirely plausible when wrong, which is why `check_orientation()` raises rather than warns.

1. **Longitude.** Source files run −179.975…179.975. This project indexes on **0–360**
   (`domain.yml`'s `lon0: 0.025`), applied as a roll of half the grid,
   `gx_project = (gx_file + 3600) % 7200`. The reason is that the Pacific box straddles
   the antimeridian: on the native grid it is two wrapping `gx` ranges and every
   `WHERE gx BETWEEN` in the codebase would have to know. Get the roll wrong and the map
   draws the Pacific over the Atlantic, convincingly.

2. **Latitude.** The daily files are **south-up** (`lat[0] = −89.975`). The climatology
   files are **north-up** and must be flipped. Subtracting them unflipped yields an
   anomaly field spanning about ±18 °C instead of ±5 — wrong in every cell, and it renders
   as a believable map. The diagnostic that catches it: correctly oriented, climatology
   valid cells are a strict *subset* of daily valid cells (13.31 M of 17.19 M globally);
   flipped, the overlap collapses to 9.17 M.

#### The domain: a Pacific box, not the globe

**60°S–65°N, 100°E–290°E** — `gy` 600..3099 (2500 rows), `gx` 2000..5799 (3800 cols).

- **7,477,923 ocean cells/day**, of which **7,240,513 (96.8%) have a climatology**.
- 100°E rather than 120°E because CoralTemp is a coral-reef product and 120 clips the
  Coral Triangle and the Java/Banda seas.
- 65°N/60°S captures the Blob, the PDO domain, the Bering Sea, and the ACC at Pacific
  longitudes. Cutting the poles is also what lifts climatology coverage from 77.4%
  (global) to 96.8%.
- **All four Niño boxes are inside it** — 1+2, 3, 3.4, 4. The old OISST box was clipped at
  the equator and could not compute any of them; the repo is finally named for what it does.

Widening it needs no re-ingest: `gy`/`gx` index the *global* grid, so only `domain.yml`'s
`subset` block changes.

#### The third state: ocean with no anomaly

About **3.2% of the box's ocean has SST but no climatology** — the seasonal ice fringe,
which the source flags explicitly (`mask` = 4). Those cells are neither land nor
zero-anomaly, and all three have to look different:

- land → **transparent** (the dark basemap shows through)
- no climatology → **flat grey** (`render.NO_CLIM_RGBA`, surfaced as `/domain`'s `noClimColor`)
- everything else → the variable's colour scale

Transparent would read as land; any scale colour would read as a real near-zero anomaly.

### `shared/` — the contract between `api` and `process`

Both containers mount `./shared` at `/app/shared`. Six modules:

- **`domain.py` + `domain.yml`** — grid geometry, variable metadata, named region boxes.
  Describes **two** grids and the distinction matters: `global` is the full 7200×3600
  CoralTemp grid that `gy`/`gx` index; `subset` is the Pacific box actually ingested.
- **`fields.py`** — NetCDF reading, and the single home of both orientation rules above.
- **`render.py`** — field array → Web-Mercator WebP. **Takes arrays, never a DB client.**
- **`periods.py`** — daily/weekly/monthly buckets, shared by query and render.
- **`buckets.py`** — **the single definition of what a bucket's field is.** Reads the days
  on disk and reduces them: mean for `sst`/`anom`, **max for `mhw`**. It lives here rather
  than in `process` because `api/modules/render.py` renders the retention window's buckets
  on demand and had grown a second copy of it — the exact drift retiring `api/prerender.py`
  was meant to end.
- **`ch.py`** — the ClickHouse client factory and the **single definition of the schema**
  (`DDL`, applied idempotently by `ensure_schema()`). No `.sql` file; keeping the DDL in
  one Python constant is what stops `api` and `process` drifting apart.

### ClickHouse (`db-ch`, database `enso`)

**`sst_daily`** — one row per (date, ocean cell). ~113.7 B rows, ~85 GB.

```sql
date      Date    CODEC(DoubleDelta, ZSTD(3))
gy        UInt16  -- global row index, 0..3599
gx        UInt16  -- global column index, 0..7199 (0-360 convention)
sst_raw   Int16   -- raw source counts, 0.01 degC
has_clim  UInt8   -- does this cell have a climatology for this date's MMDD?
sst       Float32 ALIAS sst_raw * 0.01
lat       Float32 ALIAS -89.975 + gy * 0.05
lon       Float32 ALIAS 0.025 + gx * 0.05
ENGINE = MergeTree PARTITION BY toYear(date) ORDER BY (gy, gx, date)
```

**`sst_clim`** — the 1991–2020 daily climatology, one row per (mmdd, ocean cell).
2.68 B rows, **2.18 GiB**. `ORDER BY (gy, gx, mmdd)`.

**`mhw_daily`** — one row per (date, cell) **that is actually in a heatwave**. ~24.2 B rows.

```sql
date  Date    CODEC(DoubleDelta, ZSTD(3))
gy    UInt16
gx    UInt16
cat   UInt8   -- 1..5, the source's own ordinal class; no scale factor to undo
lat   Float32 ALIAS -89.975 + gy * 0.05
lon   Float32 ALIAS 0.025 + gx * 0.05
ENGINE = MergeTree PARTITION BY toYear(date) ORDER BY (gy, gx, date)
```

**Only `cat >= 1` is stored, and that is what makes it affordable.** Measured over 40
random days spanning the archive, the box averages **1,592,012** heatwave cells a day
against 7,477,923 ocean cells — so ~24.2 B rows rather than ~113.7 B.

**The cost is that absence is ambiguous.** A missing (date, cell) is heatwave-free ocean,
ice, land, *or a date that was never ingested*, and the table cannot say which. Two
consequences, both load-bearing:

1. Every query reads `mhw_daily` as the **right side of a LEFT JOIN against `sst_daily`**,
   which holds exactly the ocean cells for a date. That join supplies the zeros and
   excludes the land, and both tables share `ORDER BY (gy, gx, date)`, so at a point it is
   a primary-key read on both sides. A *box* deliberately does not join — see
   `_region_mhw_daily`.
2. A partly-ingested archive reports a confident **category 0** for every date it has not
   reached, not a gap — so `/coverage` carries `mhw.complete` and the frontend refuses to
   offer the variable until it is true. This is the same shape as `climatology.complete`
   gating `anom`, but sharper: there is no value that could signal the difference.

**`region_clim`** — 8 regions × 366 MMDD = **2,928 rows**. The climatology side of a
region anomaly.

**`region_daily`** — 8 regions × 15,212 days = **~121,700 rows**. The daily side, and the
one that actually costs something.

```sql
region         LowCardinality(String)
date           Date
mean_sst       Float32   -- over every ocean cell in the box      -> sst
n_cells        UInt32
mean_sst_clim  Float32   -- over its has_clim = 1 cells only      -> anom
n_cells_clim   UInt32
mean_mhw       Float32   -- sum(cat*cos) / sum(cos) over the ocean -> mhw
ENGINE = ReplacingMergeTree(updated_at) ORDER BY (region, date)
```

**Measured, this is the 97.6% that `region_clim` is not.** The climatology side costs
0.296 s live against a daily side of 12.14 s for Niño 3.4 — so `region_clim` removes 2.4%
of the request. The live daily query runs **3.14 s** for the smallest named region (Niño
1+2, 38,455 cells) and **12.14 s** for Niño 3.4 (201,392 cells), which is not a latency an
interactive panel can be built on. That measurement is what the "deliberately absent until
measurement says otherwise" note above `region_daily` was waiting for.

**The two SST columns are not redundant, and collapsing them is the way this table gets
silently wrong.** `region_timeseries()` restricts the daily mean to `has_clim = 1` for
`anom` and deliberately does not for `sst`, so the two variables average different cell
sets — and the gap moves through the year with the climatological ice edge. The Bering Sea
box holds 70,166 ocean cells but only **12,086** with a climatology on 15 March. Serving
`anom` from `mean_sst` would break the `mean(sst - clim) == mean(sst) - mean(clim)`
identity exactly over the ice fringe, which is where a marine-heatwave question gets asked.

**No bucketing lives here.** Weekly and monthly reduction stays in `region_timeseries()`,
which folds these daily rows in Python through `shared/periods.py`. Materialising weeks
would put a second definition of "a week" in the codebase — the drift retiring
`api/prerender.py` was meant to end. `mean_sst_clim` is **NaN**, not 0, when a region has
no climatology cells at all on a date; no configured region hits this, but a 0 there would
read as "exactly at climatology".

**Only named regions have a rollup.** `/regionTimeseries` on an arbitrary box still
aggregates live, and that is the only difference between the two endpoints.

**`ingest_status`** / **`mhw_status`** — `ReplacingMergeTree(updated_at) ORDER BY date`, one
row per day, one table per archive. Two tables rather than one with a `product` column
because the sorting key is `date` and a product would have to join it, which cannot be
altered in place — and because the two archives genuinely progress independently.

Four decisions worth not undoing:

1. **There are no projections anywhere, and this is the central design decision.** The
   OISST schema carried a `by_date` projection so whole-day map reads did not scan a table
   ordered for point timeseries. Measured, that projection was **1.47 of 2.22 bytes per
   row — 66% of total storage**. At CoralTemp's volume it would cost ~150 GB.

   It is gone because **images are no longer rendered from the database**: the daily
   NetCDF is still on disk when `process` renders that day's frames. ClickHouse now serves
   only what it is ordered for. The cost, stated plainly: re-rendering a historical bucket
   without its NetCDF is a partition scan, so pre-rendered images are the durable artifact
   and a mass re-render means re-downloading the range. `/image` 404s rather than hangs.

2. **`gy`/`gx` index the global grid, not the subset**, so a cell's identity does not
   depend on the current box. Conversions go through `shared.domain.global_grid()` — never
   hand-roll them.

3. **`sst_raw` is the source's own Int16, with an ALIAS doing the ×0.01.** Lossless,
   2 bytes, compresses far better than Float32. ALIAS columns cost no storage.

4. **`has_clim` is per (cell, date), not per cell.** The ice edge moves through the year,
   so it cannot be a static property. It is what makes the region identity below exact.

#### Anomaly is derived, and how depends on the query shape

`anom = sst - climatology(mmdd)`. There is no anomaly column.

- **A point** joins `sst_daily` to `sst_clim` on `(gy, gx, mmdd)`. For one cell that is
  ~15 k rows against 366, both primary-key reads. Trivial.
- **A box never joins**, because the means commute:

  ```
  mean(sst - clim) == mean(sst) - mean(clim)
  ```

  over the same cells with the same cos(lat) weights. So the daily side stays a plain
  aggregation and the climatology side collapses to one value per MMDD — precomputed in
  `region_clim` for named regions, computed on the fly for an arbitrary box. Joining
  instead would put box_cells × 366 rows on the right of a hash join — 461 M for the PDO
  box — to produce 366 numbers.

  **The identity's precondition is `has_clim = 1` on the daily side.** Both sides must
  average the same cells; without the filter the two drift apart wherever the ice edge
  sits, which is exactly where a marine-heatwave question gets asked. Verified equal to
  three decimals against a direct cell-wise `avg(sst - clim)`.

**Named-region timeseries are served from `region_daily`; arbitrary boxes are queried
live.** Named regions are 0.6–14.6 B rows over the archive (Niño 3.4 is 3.04 B; the PDO
box, the largest, 14.55 B), and `ORDER BY (gy, gx, date)` makes a box a set of contiguous
key ranges — one per `gy` — not a scan. That is still 3.14–12.14 s, so the rollup was added
exactly as predicted: a pure cache, 8 × 15,212 rows, without touching the big table.

### Process pipeline (`process/`)

Entry point `process/CRW/cli.py` (`python -m CRW.cli`). Modules:
- `config.py` — filename parsing, `scan()` / `scan_mhw()`
- `download.py` — NOAA CRW fetching; a `Product` value per archive (`SST`, `MHW`)
- `ingest.py` — NetCDF → ClickHouse; a `Target` per archive (`SST_TARGET`, `MHW_TARGET`)
- `climatology.py` — the 366-file load and `region_clim`
- `regions.py` — the `region_daily` rollup (`CRW.cli rollup`)
- `imaging.py` — day/week/month × sst/anom/mhw rendering, and the retention window
- `status.py` — the `ingest_status` table

Inserts are batched across days (`--batch`, default **5** — a day is ~7.5 M rows now, not
OISST's 96 k, so the old default of 30 was a 225 M-row insert).

#### Downloading, and revisions in place

```
.../5km/v3.1_op/nc/v1.0/daily/sst/{YYYY}/coraltemp_v3.1_{YYYYMMDD}.nc
```

**CRW has no preliminary/final filename pair.** `v3.1_op` is the operational near-real-time
stream and a date's file is **revised in place** at a URL that never changes. Since `run`
deletes the local NetCDF after ingesting, the only way to notice is to keep what the server
reported — `Content-Length` and `Last-Modified` — in `ingest_status` and HEAD the URL again
later. That is `--recheck-days` (default 30). This replaces the OISST `is_preliminary`
mechanism entirely.

Downloads stream to a `.part` file and rename on completion; `DAILY_RE` does not match
`.part`, so an interrupted transfer can never be picked up as a complete file.

#### `run`, and why it is a range

With no `--date`, `run` processes **every day from the last ingested through yesterday**,
not just yesterday. One code path then covers normal daily operation, a missed cron run,
and a bulk download that has outrun the ingest. A 404 on yesterday exits 0, not failure —
CRW publishes at ~1 day latency and not at a fixed hour, so a cron retry must be a
no-op-or-succeed.

Per date: `download+ingest SST → download+ingest MHW → render 9 images → prune both
retention windows`. The two products are handled **independently** — one being unpublished
is not a failure of the other, and the date's frames are rendered for whatever landed. The
catch-up watermark is the **earlier** of the two archives' last ingested day, so a date
that is SST-done but MHW-pending — which happens whenever a run lands in the ~90 minutes
between the two publications — is picked up on the next run rather than stranded behind
the SST watermark.

#### The retention window

A weekly frame is the mean over seven days, but `run` deletes each `.nc` after ingesting
it — so days N−6…N−1 are gone by the time day N is processed. **`imaging.retention_floor()`
keeps the files the open week and month still need** (at most ~37 files, ~380 MB) and
prunes only what has fallen out of both. `prune()` walks **both** archives: an MHW weekly
frame is a max over the same span of days and needs its own files kept for exactly as long,
and at ~640 KB a file the second window costs ~24 MB. Losing that window does not corrupt anything, but
it freezes weekly and monthly frames at whatever was last rendered.

### API (`api/`)

FastAPI in `SERVER.py`. **Timeseries are read live from ClickHouse; imagery is not.**

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + ClickHouse reachability |
| `GET /domain` | grid extent, image bounds, variable metadata, per-variable colour stops and `encoding` (mix, ranges, `limits`), `noClimColor`, region list |
| `GET /coverage` | ingested date range, row count, climatology completeness, MHW archive range and completeness |
| `GET /variables` | variable list, with `derived` on `anom` |
| `POST /timeseries` | `{lat, lon, start?, end?, period?, variable?}` → record at the nearest cell |
| `POST /regionTimeseries` | `{lat: [a,b], lon: [a,b], ...}` → area-mean over an arbitrary box |
| `GET /region/{key}` | same, for a named `domain.yml` region, using `region_clim` |
| `POST /monthlyRanking` | every calendar month at a cell, ranked within its month-of-year |
| `GET /region/{key}/monthlyRanking` | the same ranking over a named region, from `region_daily` |
| `GET /image/{date}.webp` | one bucket as a Web-Mercator WebP |

**`variable` — `sst` (default) / `anom` / `mhw`** — is accepted by every endpoint above.

**How `mhw` is bucketed differs by query shape, and each difference is deliberate:**

- **A point and the map take the MAX** over the bucket. A category is an ordinal class and
  its mean is not one — a cell at Cat 1 for two days of seven averages to 0.29, which is no
  category at all. The max answers what the frame is read for (*how bad did it get this
  week*), keeps every period on the same 1..5 scale so one legend serves all three, and
  keeps the invariant the whole period mechanism exists for: a chart point and the map
  frame carrying the same date agree.
- **A box takes the MEAN of daily area means.** Over a box the daily value is already a
  cos(lat)-weighted area mean — continuous, and no longer a category — so there is nothing
  ordinal left for a max to preserve, and a max of daily area means would be a spike
  detector for each week's worst day.
- **The monthly rankings take the MEAN**, and the point one is the place `mhw` is
  deliberately averaged at a cell. They rank years against each other, and a max would put
  most of the archive on Cat 1 and rank nothing; the mean daily category over a month is a
  severity-days index that separates one bad week from a whole month at Cat 1. The region
  ranking is a mean of daily area means, which was never a category to begin with.

**`period` — `daily` (default) / `weekly` / `monthly`** — buckets are defined once in
`shared/periods.py`: weeks start on **Monday** (`toMonday`), months are calendar months, and
a bucket is always labelled by its **first day**. The frontend mirrors the same arithmetic
in `front/app/utils/periods.ts` — **change one and change the other.**

**Area means are cos(latitude)-weighted** (`sum(v*cos(lat)) / sum(cos(lat))`). At 60°N a
0.05° cell covers half the area of one at the equator, so a plain `avg()` over-weights the
poleward end of any box tall enough to matter.

**Out-of-domain points** raise `OutsideDomainError`, rendered as a **400 carrying both a
plain-string `detail` and a structured `error` object** (`code: "outside_domain"`,
`requested`, `domain`). `detail` stays a sentence so callers that just print it keep
working; `error.code` is what lets the frontend show this as an informational empty state
rather than a red failure.

**The monthly rankings are always monthly, whatever the caller's `period`**, and they rank
`anom` by default because ranking years by absolute SST is a different question. Every
month is ranked including the archive's truncated edge months, which carry `partial: true`
— the month in progress is the one people most want to look at, so it is starred rather
than hidden. A month missing an *interior* day is **not** partial: it is as complete as it
will ever be.

**There are two of them — a cell and a named region — and the ranking itself is defined
once.** `_ranked_months()` takes any subquery yielding `(date, value)` and does the
grouping, the `stddevSamp` and the `row_number()`; only the series underneath differs, so
the two cannot drift into meaning different things. A cell's series is the ~15k-row
primary-key read the point timeseries makes; a **named region's is `region_daily`**, folded
by month instead of by period bucket — the same numbers `/region/{key}` plots, so the year
that ranks first is the year whose month the chart draws highest. Measured, that is **15 ms**
for Nino 3.4, because the rollup already exists.

Two things about the region ranking are not the cell ranking, and the response says so
rather than leaving them to be inferred:

- **`sd` is the spread of daily *area* means**, not of daily values. Spatial averaging
  cancels the noise one cell keeps — Nino 3.4's August 2015 is 0.176 against the same
  month's 0.308 at a cell inside it — so the two columns are not comparable. `areaMean:
  true` is what the frontend reads to label it (`sd of daily means`) rather than renaming
  the field.
- **`anom` comes off `mean_sst_clim`, not `mean_sst`** — `_ROLLUP_COLUMNS`, same choice
  `named_region_timeseries()` makes, because the `mean(sst - clim) == mean(sst) - mean(clim)`
  identity needs both sides averaging the `has_clim = 1` cells. The subtraction is per day
  against `region_clim`'s MMDD, not per month, so a month spanning the ice edge's seasonal
  move still subtracts the matching climatology.

**Only named regions get one.** An arbitrary box through `/regionTimeseries` has no rollup
and would be the 3–12 s live aggregation, so there is no ranking endpoint for one.

### Map imagery (`shared/render.py`)

There is **no tile pyramid**. The Pacific box is one WebP, served as a Mapbox `image`
source with corner coordinates from `/domain`'s `imageBounds`.

**`imageBounds` longitudes are unwrapped**: the box's east edge is reported as **290, not
−70**. Mapbox accepts that and places the quad correctly across the antimeridian — verified
in Chromium, where `project([290,0])` and `project([-70,0])` return the same pixel.
Normalising east into −180…180 would make west > east and collapse the image source to
nothing. This is *not* the same question as the 0–360 storage convention; the box straddles
the antimeridian either way.

The source grid is linear in longitude but **not** in Mercator y, so `to_mercator()`
resamples the rows onto an evenly spaced Mercator axis. Linear blending propagates NaN from
either neighbour, which would erode a pixel of ocean along every coastline, so it falls
back to whichever side is real. The no-climatology mask is resampled **nearest-neighbour**
instead — it is categorical, and a blended edge has no sensible threshold.

#### The images carry data, not colour

`/image` ships the **value packed into the RGB channels**, with land in alpha, and the
browser applies the colour ramp with Mapbox's `raster-color`. `shared/render.py`'s
`encode()` is the packer; `domain.yml`'s per-variable `encoding` block is the contract, and
`/domain` hands the frontend a ready-made `raster-color-mix` so the packing arithmetic is
written **once, in Python**, and never re-derived in TypeScript.

The reason is the retention window. The daily NetCDF is pruned, so a bucket's cached image
eventually becomes the only surviving copy of that field — and a pre-coloured cache would
have today's colormap and today's vmin/vmax welded into it permanently. Re-ranging the
anomaly from ±3 to ±4 would mean re-downloading the range. Value-encoded, the palette and
the displayed range are client-side settings for good.

| variable | channels | step | range | notes |
|---|---|---|---|---|
| `sst` | `G`,`B` → `G*256+B` | 0.01 °C | −5…650 | the source's own precision, lossless |
| `anom` | `R` | 0.1 °C | −12.8…+12.7 | code 0 = no climatology |
| `mhw` | `R` | 1 category | 0…255 | code == value; 0 and land both alpha 0 |

**Lossless WebP, necessarily** — lossy is YUV 4:2:0 and destroys packed data: measured at
q90 on a real frame, mean error 0.074 °C but **maximum 1.613 °C**, i.e. visible blotches.
It costs 2.3–2.7× the bytes (sst 1.14 MB, anom 0.50 MB at width 2048) and **decodes
slightly cheaper** than lossy — 41.8 ms vs 39.7 ms measured, no inverse DCT or YUV
conversion. Colouring is free: pan frame time is 16.6 ms with `raster-color` against
16.7 ms pre-coloured, both at the vsync ceiling under software rendering.

Three details that are load-bearing, all of which fail quietly:

- **`raster-resampling: nearest`, set in `AnomalyMap.vue`.** `sst` spans two channels, and
  linear filtering blends them *independently* — a texel pair straddling a low-byte wrap
  would decode ~2.56 °C away from either neighbour. Measured, nearest and linear are
  identical on the decode (median error 0.156 vs 0.159 °C, both just texel quantisation),
  and nearest is visibly cleaner at single-pixel islands, which linear renders as coloured
  speckle. So it costs nothing and removes the whole failure class.
- **Land is filled with nearby ocean values, not left at 0** (`_bleed()`). Mapbox filters
  the texture; a coastal texel blending ocean against a land 0 decodes to the bottom of the
  scale, giving a wrong-coloured fringe along every coastline. Land is cut by **alpha**,
  which is exact. Bleeding runs on the integer code, never the packed channels — averaging
  a low byte across a 255→0 wrap lands 256 counts out.
- **Values are clamped, never wrapped.** A uint8 overflow would redraw a record-warm cell as
  the coldest colour on the map. `anom`'s ±12.7 clips about **4 cells a day out of 7.5
  million** — measured over 60 days spanning the archive and the strong ENSO peaks, where
  the anomaly reaches −10.22…+14.05 °C — and every one of them is already saturated at the
  ±3 display range.

`raster-color` tabulates its ramp at **256 uniformly spaced steps over `raster-color-range`**,
so `/domain` sends a different range per variable — `Variable.color_range()` owns the
decision and `store.colorRangeFor` mirrors it. A variable whose *codes* must land one per
ramp entry tabulates its whole encoding range: `anom` (−12.8…12.7) so its sentinel gets a
slot of its own, and `mhw` (0…255) so that entry k is code k. Tabulating mhw's five classes
over 1…5 instead would put code 2 at entry 63.75, where a **Cat 2 picks up Cat 1's colour**.
`sst` has neither constraint and spends all 256 entries on the −2…32 display range.

#### `mhw` is categorical, and three things follow

None of them fails loudly if it is skipped, which is why `domain.yml` declares
`categorical: true` and everything reads that flag rather than testing the name:

1. **The Mercator resample is nearest, not bilinear** (`to_mercator(..., nearest=True)`).
   Blending a Cat 2 against a Cat 4 invents a Cat 3 along every edge. Measured on
   2023-10-01 at width 2048: bilinear mis-categorises 3,072 cells (0.28% of shared ocean),
   but the bigger cost is that it **invents 42,088 heatwave pixels** — 3.9% more than exist
   — by blending a real category against the NaN of heatwave-free ocean and letting the
   NaN-fallback make the result opaque.
2. **The ramp is a `step`, not an `interpolate`** (`categoricalRamp` in `AnomalyMap.vue`),
   and the chart's `visualMap` is `piecewise` for the same reason. There is no colour
   between two classes because there is no value between them.
3. **The displayed range is not the user's.** The stops *are* the classes, so `ColorLegend`
   shows a named key instead of a gradient and a slider, `setScale` refuses a categorical
   variable outright (a stale `localStorage` entry must not be honoured), and `stopsFor`
   skips the re-spreading — which is currently the identity, and is skipped explicitly so
   it stays correct when a class is added.

**The palette is NOAA's own, measured rather than guessed.** Extracted from NOAA's plain
PNG for 2023-10-01 by matching every pixel against that date's NetCDF: all five categories
matched **100%**, as did the three non-heatwave classes (land `#969696`, ice `#ffffff`,
no-heatwave `#b3f2ff`) — which this project draws transparent instead. `domain.yml` lists
them as `colors` rather than naming a colormap; matplotlib has no say, because the point of
these five is that people already recognise them from NOAA's maps.

| cat | | colour |
|---|---|---|
| 1 | Moderate | `#ffff80` |
| 2 | Strong | `#ffb333` |
| 3 | Severe | `#ff8000` |
| 4 | Extreme | `#cc4d00` |
| 5 | Beyond extreme | `#991a00` |

Exact values remain the timeseries endpoints' job; this raster is for looking at.

`DEFAULT_WIDTH` is **2048**, and `front/app/composables/useApi.ts`'s `IMAGE_WIDTH` mirrors
it. The cache is keyed by (variable, period, bucket start, width), so **a width mismatch is
a 404 and a blank map, not a slower render** — there is no NetCDF left to render a
historical bucket from.

Renders are cached under `OISST_IMAGE_DIR` as
`{variable}/{period}/YYYY/{bucket-start}_w{width}.webp`.

**Colour ranges live in `domain.yml`**: `sst` is **sequential** (`turbo`, −2…32) and `anom`
**diverging** (`RdBu_r`, ±3). That distinction is not cosmetic — an absolute temperature has
no meaningful midpoint, so a diverging map would invent one at 15 °C and read as a signed
field. Since the images carry data rather than colour, changing either range or colormap now
only needs an API restart — **the image cache is not invalidated by a palette change.**

`vmin`/`vmax` are only where the scale **opens**: the displayed range is adjustable in the
browser (see the colour range control below), and `domain.yml`'s pair is the default and
what Reset returns to. The colormap is not adjustable — the ramp is matplotlib's, evaluated
server-side.

**`limits` is the span the user may drag that range over, and it is deliberately not the
encoding's.** `sst` packs into two bytes at 0.01 °C and can therefore represent −5…650 °C,
which is arithmetic rather than oceanography — a control bounded by it would spend 95% of
its travel above the boiling point. So `sst` declares `limits: [-2, 36]` (the freezing point
of seawater; 36 clears the warmest ocean SST anywhere, and this box peaks near 32) and
`anom` omits the key and falls back to its encoding, ±12.7, which is already physical.
`Variable.range_limits()` clips whatever is declared to what is encodable, and `/domain`
ships the result as `encoding.limits`.

### Frontend (`front/`)

Nuxt 4 + Nuxt UI v4 (Tailwind v4) + Pinia + MapboxGL + ECharts, dark mode pinned — the
same stack as the ocean-acidification dashboard.

```
app/app.vue                        header + coverage badge; awaits store.loadMetadata()
app/pages/index.vue                numbers + ranks dock on the left, map over the chart
app/components/AnomalyMap.vue      MapboxGL + the field image source
app/components/TimeControl.vue     variable + period toggles, date stepper, playback
app/components/ColorLegend.vue     gradient + the colour range control (popover)
app/components/TimeseriesChart.vue ECharts line with dataZoom
app/components/ScopeControl.vue    point / named-region switch, over the map
app/components/StatsPanel.vue      the dock's headline value and stat cards
app/components/MonthlyRankPanel.vue  the map's month, every year ranked (under the cards)
app/components/SideDock.vue        resizable left-hand dock (drag handle, remembered width)
app/composables/useApi.ts          axios wrapper
app/composables/usePlayback.ts     play/stop loop + frame prefetch for the map animation
app/utils/periods.ts               daily/weekly/monthly bucket maths (mirrors the API)
app/utils/ranking.ts               ranking layout + both ECharts options (pure -> testable headlessly)
app/utils/colorScale.ts            domain.yml's colour stops evaluated at a single value
app/utils/csv.ts                   CSV export of the plotted series and the rankings (pure text + one download)
app/app.config.ts                  maps Nuxt UI's internal icons onto mdi
app/stores/main.ts                 Pinia store
```

#### Downloading what is plotted

Two download buttons, both exporting **the data the chart already has** rather than issuing
a request of their own — a second path to the same numbers would be a second definition of
"the weekly mean", the drift `shared/buckets.py` exists to prevent. `utils/csv.ts` is pure
text-building plus a single DOM-touching `downloadCsv()`, for the same reason `ranking.ts`
and `stats.ts` are pure.

- **`TimeControl`'s button saves the series**, at whatever variable, period and scope the
  chart is on. Columns are `start_date,end_date,<variable>` — **both** date columns, on
  every period: a weekly row labelled `2024-05-06` is a mean over seven days, and a file
  saying only `date` invites reading it as that Monday's reading. On a daily series the two
  are equal, which is honest rather than redundant. A null value is an **empty field**; any
  placeholder would read back as a reading.
- **`MonthlyRankPanel`'s button saves all twelve months**, not the one on screen — the
  panel shows the map's month because 45 rows twelve times over is unreadable on
  screen, which is not a limit a file has, and a `month` column is what makes the table
  filterable. `partial` is carried as a boolean: it is the difference between a settled rank
  and one that will move.

Filenames carry everything that decides what the numbers are —
`anom_weekly_nino-3-4_1984-12-31_2026-08-24.csv`,
`anom_monthly-ranks_47-98n_127-98w.csv`, `anom_monthly-ranks_nino-3-4.csv` — because a
folder of `timeseries.csv` files is indistinguishable a week later, and a cell and a region
are not comparable numbers: one is a point reading, the other an area mean.
Cells are slugged as hemispheres for the same reason `index.vue` prints them that way: the
0-360 longitudes the API returns would name 128 W as `232.00`.

Values are written through `toFixed` at the variable's own precision — the API's floats
arrive as `0.20200000000000001`, which a spreadsheet shows verbatim.

**The chart rail is point-only, and the map click is the only selection.** There was a
`Point | Region mean` tab pair here; the region side is gone from the UI, though
`/region/{key}` and `/regionTimeseries` still exist and now take `variable` too. The Niño
indices are the obvious thing to surface there next.

**`store.variable` (`sst` | `anom` | `mhw`) works exactly like `store.period`**: it drives the
chart request and the image URL together, so the map and the chart can never show
different fields. **It opens on `anom`** — how far from normal it is, is the question the
dashboard exists for; absolute SST is the reference view you switch to, and it comes second
in the toggle. `anom` is the derived one, though: it is undefined over the ice fringe and
needs the full 366-key climatology, so the Anomaly button stays **disabled until
`/coverage` reports `climatology.complete`**, and `loadMetadata()` falls back to `sst` when
it is not. A partly-loaded climatology would blank the missing dates rather than fail,
which is worse — and opening on a variable whose own toggle is disabled is worse still.

**`mhw` is gated the same way, for a sharper reason.** `store.variableReady()` holds both
rules in one place: `anom` needs `climatology.complete`, `mhw` needs `mhw.complete`, and
`sst` — the stored field the other two are built from — is always available, which is what
makes it the fallback. The MHW case is sharper because its table is *sparse*: a
half-backfilled archive does not blank the missing dates, it reports a confident **category
0** for them, and a monthly ranking then ranks forty fabricated zeroes below one real
month. No value could signal the difference, so the toggle is disabled and says why.

Three more things `mhw` changes in the UI, all because it is a class rather than a
measurement:

- **`ColorLegend` shows a named key**, not a gradient with a range popover. The names are
  the point — Cat 3 means Severe, which is what the map is being read for — and there is
  nothing between two classes to re-range.
- **Nothing prints a degree sign at it.** `store.activeUnitLabel` is `''` for `mhw`, and
  the chart tooltip, the legend title, the y-axis name and the ranking rows all read it.
  The tooltip names the class too: `3 (Severe)`, and `0 (no heatwave)` — which is the
  common reading and needs saying rather than looking like a missing point.
- **Below Cat 1 is drawn neutral, not clamped** (`NO_CLASS_COLOR`, shared by the chart's
  piecewise `0` piece and `colorScale`'s `belowFirst`). A ranked month whose mean category
  is 0.0 had no heatwave at all; painting it Cat 1's yellow says exactly the opposite.
  Continuous variables keep clamping, which is right for them — an SST of −4 is simply off
  the bottom, and the map saturates it the same way. The ranking's copy follows too:
  rank 1 is "most severe", not "warmest".

The monthly ranking **refetches on a variable change** but not on a period change: ranking
years by absolute SST is a different question from ranking by anomaly, whereas the ranking
is period-independent by construction.

#### The colour range control

**The displayed range is a user setting, per variable, and this is what the value-encoded
imagery is for.** Clicking the legend opens a popover (`ColorLegend.vue`) with a
two-handle slider, exact min/max number fields, and Reset. **The affordance is
spelled out rather than left to the cursor** — a gradient reads as a legend, a
thing you consult, so the trigger carries a `Customize` chip beside the title and
the whole block (title, chip, bar, ticks) is one button. Categorical variables
keep a plain title: there is no range to edit. Narrowing `sst` to 20–30 recolours
the map instantly and **issues no network request at all** — verified in Chromium, zero
`/image` fetches — because the frame on screen carries the value and Mapbox re-applies the
ramp. Nothing in the cache is invalidated, and the range is not part of the image URL's key.

**Named bands make that adjustability legible**, which the slider alone did not: it says
*that* the range moves without saying what range is worth asking for. `domain.yml` declares
a `presets` list per continuous variable — sst `Coral 24-32 / Tropical 20-32 / Temperate
10-25 / Cold -2-12`, anom `Fine ±1 / Wide ±5` — served through `/domain` beside `vmin`,
`limits` and the stops, so **the numbers are never mirrored in TypeScript**. Three things
about them:

- **The default is not declared as a preset.** It is `vmin`/`vmax`, and `ColorLegend`
  synthesises its chip from those, so the file holds one definition of the default rather
  than two that drift apart the first time one is edited. It is also the only chip that
  calls `resetScale` rather than `setScale`, which is what makes it agree with the `custom`
  badge and the Reset button without a third rule. Every other chip is `setScale` and
  nothing else — no `activePreset` state, so `colorRangeFor`/`stopsFor`/`activeScale` needed
  no changes, and a preset click is indistinguishable from a drag once persisted (verified:
  pick Tropical, reload, it reopens on 20-32 with the chip marked).
- **A band is clipped by nobody, and validated by `shared/domain.py`.** `_check_presets()`
  raises on one outside `range_limits()` rather than clamping it — the frontend's `setScale`
  would clamp it silently, and a chip that lands somewhere other than its label is worse
  than an import that fails. Raised, not asserted: `assert` vanishes under `-O`.
- **Narrowing clamps; it does not isolate.** On 20-32 the band gets all 256 ramp entries,
  which is the point, but colder water still paints the bottom colour rather than dropping
  out. "Show me only the 15-20 water" is a different feature (out-of-band drawn neutral) and
  is deliberately not built.

Chip membership is compared through the same `quantise` `setScale` applies on the way in —
exported from the store for that one caller. Every declared band is a step multiple today,
so `===` would work by luck; this keeps working when someone adds one at 24.05.

The range lives in `store.scales` and everything reads it through one getter, so the map,
the legend and the ranking dots cannot disagree about what a colour means:

- **`stopsFor(v)`** spreads `/domain`'s stops over the range in force. The server samples
  the colormap at 33 **evenly spaced** points (`render.colormap_stops`), so stop *i* is the
  colormap at `t = i/32` and re-labelling those same colours onto a new range is exact.
  **The frontend never evaluates a colormap** — that stays matplotlib's job, server-side,
  which is why there is no colormap picker.
- **`colorRangeFor(v)`** mirrors the conditional in `/domain` and must stay conditional:
  `anom` has a sentinel, so it keeps tabulating its whole encoding span (code 0 needs a slot
  of its own) and its `raster-color-range` **never follows the display range**; `sst` has
  none, so its does.
- **`scaleBoundsFor(v)`** is `encoding.limits`, with the low end stepping over the sentinel
  so a user's `vmin` can never land on the grey no-climatology entry and overwrite it.
- **`setScale`** owns all clamping, so the slider and a typed value are constrained
  identically. It holds the ends at least one step apart — a zero-width range makes the
  ramp's interpolation degenerate and the map undrawable.

Three things here fail in ways worth knowing about, all found by driving the browser:

- **The ramp's sentinel anchor collides at full range.** `anom`'s ramp is grey at −12.8,
  then the first scale colour at −12.7; drag `vmin` to its floor and the first *stop* is
  also −12.7, and Mapbox rejects an `interpolate` with two identical inputs — taking down
  the whole layer, not just the pair. `AnomalyMap.vue` skips the anchor when the first stop
  has already reached it.
- **The control's step is deliberately not the encoding's** (`rangeStep`, `max(scale, 0.1)`).
  `sst` encodes at 0.01 °C, which is right for the data and absurd for a slider: one arrow
  press would move a hundredth of a degree and crossing the range would take 3800 of them.
  Slider and number fields share the one step, so a typed value can never sit off the
  slider's grid and get silently snapped.
- **Quantising needs the quotient settled first.** `12.7 / 0.1` is `126.99999999999999`, so
  a bare `Math.floor` quietly shaves the top step off the anomaly's range.

Persistence follows `SideDock`'s convention — `localStorage`, key `enso.scale.<variable>`,
read once from `ColorLegend`'s `onMounted` (**not** `loadMetadata()`, which runs under SSR)
and re-clamped on read, since the encoding may have moved since it was written.

**The numbers and the monthly ranking are one panel, not two tabs.** They were a
`Numbers | Monthly ranks` pair in the same dock, which made two halves of one answer about
one cell take turns — and the ranking's own month rail then asked the same "which month"
question the numbers above had already answered. So the tabs are gone, `StatsPanel` takes
its natural height at the top, and `MonthlyRankPanel` is spent out of whatever the dock has
left below it. **Both scopes fill it**: `store.activeRanking` picks the cell's ranking or
the region's exactly as `activeSeries` picks the series, so the panel stays presentational
and the scope keeps one definition. The honest "monthly ranks are per cell" empty state
that used to sit there is gone with the endpoint that made it true.

The region ranking is fetched **alongside the region series**, in `loadRegionSeries()`'s
one `Promise.all`, and guarded the same way `selectPoint`'s `sameCell` is: a **period**
toggle re-enters with the same region and must not refetch a ranking that is always monthly
(it would flash the grid), while a **variable** toggle must — ranking years by absolute SST
is a different question from ranking them by anomaly. Verified in Chromium: Daily / Weekly /
Monthly issue only the series request, and switching variable issues only the ranking one.

**The ranking's month is the map's** (`selectedDate`'s), with no local state beside it —
the rail of twelve thumbnails that used to pick it is gone, and `thumbOption()` with it.
One consequence worth knowing: clicking a *row* still moves the map, so the panel would
follow the map straight off the month just clicked, because the store snaps to a bucket
start and the Monday of the week containing the 1st sits in the previous month. `dateFor()`
emits the first Monday *inside* the month on a weekly period for exactly that reason; on
daily and monthly the 1st already snaps to itself.

**The panel lives in a dock beside the map**, not over it. It needs full page height —
one month of ~45 years is already taller than the chart below the map — but it was a
fullscreen modal first, and that made picking a cell cost close / click / reopen every
time, which is unusable if you want to compare ten cells. `SideDock.vue` is a plain
column on the **left** (the map's own controls — projection, scope, legend, time bar —
grew on the right and below): the map keeps its clicks, the store updates, and the panel
follows.

Its width is the user's, not a constant — the dock and the map want the same pixels, so
there is a drag handle on its edge (arrow keys too) and the width is remembered in
`localStorage` under `enso.dock.width`. The floor is **420px**, which is where the panel's
fixed 74px of rank labels stop leaving a readable plot; the ceiling is 1000px and leaves
380px for the map. Both panels are therefore sized by **container queries** (`@container`
on their roots) rather than viewport breakpoints — the stat cards drop from two columns to
one when the dock is dragged in. `MonthlyRankPanel`'s `ResizeObserver` redraw is debounced
100ms because a drag fires it every frame and one redraw is a 45-row panel.

`detailOption()` is built by `utils/ranking.ts` rather than inside the SFC, for the same
reason `periods.ts` exists: being a pure function of its inputs it can be rendered
head-lessly with echarts' SSR mode and asserted on, which is much cheaper than driving a
browser for chart maths.

- **The month scales to itself.** `xDomainOf()` still takes a list of months — it shared
  one domain across the rail's twelve — but is now called with the one month on screen,
  which is what lets it spend the full width on the month being read. At a cell whose
  August reaches +6 °C, a domain covering the whole year left January using a third of
  the pane.
- **The row pitch is spent out of the pane's height** (`detailPitch()`, clamped to
  9–40px), so 45 years normally fit with no scrolling at all. Only a short pane hits the
  floor and lets the panel overflow. Measure the scroll container, not the section — the
  heading sits outside it deliberately.

Three encodings, three separate jobs, deliberately not overlapping: **dot colour** is the
value on the active variable's scale — the same colour that cell has on the map;
**label weight and ink** mark the top N (never hue, which already means anomaly); **amber**
is "where the map is", matching `TimeseriesChart`'s `MAP` markLine — the map's year is
ringed in the plot.

**A `partial` month is starred, not hidden.** The API ranks the archive's edge months
with the rest, and the panel marks them twice: the row label reads `15. 2026 *`, and its
dot is drawn **open** (no fill, its own colour as the stroke) so it reads on the same
scale without looking like a settled datum. The star is cashed in by `partialNote()` —
one dimmed line above the plot, "August 2026 is incomplete — ranked on 24 of 31 days" —
which sits *outside* the scroll pane, so it costs the pitch a row's worth of height and
nothing else.

**Playback (`usePlayback.ts`) is a paced loop over `store.setDate()`, not a second
clock.** The play button steps the map one bucket at a time until stopped — past the end of
coverage it wraps to the first bucket — and because each tick advances from whatever
`store.selectedDate` currently is, stepping or clicking the chart mid-run just relocates the
playhead. Typing in the date field stops it, since otherwise the field is a moving target.
Speed is a 1–10 fps slider read *per frame*, so it takes effect on the next one.

Frames are **not** all preloaded: the daily archive is ~15.2k WebPs per variable, so what is
held is a window of `AHEAD = 8` in front of the playhead,
warmed with `new Image()` + `decode()` and capped at `CACHE_MAX = 24`. `/image` sends
`Cache-Control: public, max-age=86400`, so Mapbox's own fetch for the same URL then resolves
out of the browser cache. The loop waits on frame readiness rather than firing on a bare
`setInterval`, because a Mapbox `ImageSource` never retries a failed image and silently keeps
the previous frame — a fixed interval would render that as an unexplained stutter. A 3 s
per-frame timeout keeps one slow frame from freezing playback.

**`store.selectedDate` is always a bucket start**, snapped through `store.setDate()` /
`store.setPeriod()` — never assign it directly. Both `store.period` and `store.variable`
drive the chart request and the image URL together, so switching either re-renders both.

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

**The projection is the user's, and it opens on the globe.** A Globe/Flat pair sits at the
map's top-left and the choice is remembered in `localStorage` under `enso.map.projection`.
The two are framed differently and cannot share a view: Mercator fits the whole box
(`fitBounds`; the old `INITIAL_NORTH = 68` clip is gone — that existed because the OISST
domain ran to 90°N and dominated a Mercator fit, and this box stops at 65°N), while the
globe takes a centre and a zoom — the North Pacific at `[-170, 25]`, zoom 1.7 — because no
`fitBounds` frames a 190°-wide box on a sphere without putting half of it behind the limb.

**The globe needs the field image twice.** Mercator draws world copies, so the single quad
at 100…290 crosses the antimeridian and is drawn whole. The globe has none: measured in
Chromium, that quad **clips dead at 180** and the entire eastern Pacific silently vanishes.
`AnomalyMap.vue` therefore adds a second image source with the *same* URL at −260…−70 while
the globe is on. Each source draws only the part of itself inside −180…180, so the two abut
at the dateline, do not overlap, and no image has to be cropped. It is removed again in
Mercator, where both quads land on the same ground and two layers at 0.85 opacity would
double into a visibly darker box.

**`imageBounds.east` is 290, not −70, and is passed to Mapbox as-is.** Normalising it into
−180…180 would make west > east and collapse the image source — see the Map imagery
section for the verification. (Confirmed again on the globe: west > east draws nothing at
all.)

Stepping days calls `ImageSource.updateImage()` rather than removing and re-adding the
layer, so the basemap does not flash between frames.

**The active region's box is a GeoJSON polygon, and it needs neither of the raster's two
workarounds.** Every named region's east edge is above 180 and three cross the antimeridian
(Niño 4 160..210, Bering Sea 180..200, PDO 180..250). Verified in Chromium in **both**
projections: an unwrapped ring draws as one continuous box on the globe and on Mercator —
no westward second copy, no split ring, no MultiPolygon. Splitting at 180 was tried and is
unnecessary. The edges *are* densified (a vertex every 2°), because a region box follows a
parallel and a four-corner polygon would draw the PDO box's 70°-wide edges as straight
chords bowing off it.

**`queryRenderedFeatures` is not a witness on the globe** — it returns 0 for a box that is
plainly on screen. This is the same lesson as `preserveDrawingBuffer: false`: take a
screenshot and look at it.

**Only one box is ever drawn, and only in region scope.** The box is the visual half of what
the numbers panel is reading, so the two are one selection seen twice rather than a layer
with a toggle of its own.

**Selecting a region flies the camera to it** (`frameRegion()`), because Nino 1+2 is 10
degrees wide on a basin that spans 190 and an amber rectangle at 3% of the map's width is
something you have to go looking for. It uses `fitBounds` **on the globe too** — unlike
`frame()`, which cannot, since half a 190-degree box sits behind the limb at any zoom that
fits it; the widest region is the PDO box at 70 degrees, which frames fine on the sphere
(verified in Chromium, screenshot). Longitudes go in unwrapped like everywhere else here,
and Mapbox wraps the resulting centre itself — Nino 4 at 160..210 lands centred on -175,
the Bering Sea at 180..200 on -170. `maxZoom` (4.5) keeps the smallest boxes from filling
the pane with no coastline around them to say where they are. Leaving region scope moves
nothing: the pin is already where the user clicked. Switching projection while a region is
active re-frames to the *region*, not back out to the whole basin.

**`map.isStyleLoaded()` is not "can I add layers".** It reports whether every source in the
style has settled and is routinely **false** on a fully drawn map — measured at 10 s into a
loaded page with both raster layers up. Guarding layer creation on it silently skips the
layer forever. `AnomalyMap.vue` tracks a `styleReady` flag set in the `load` handler, which
is the actual precondition.

**Charts**: always ECharts, never a hand-rolled `<canvas>`. The point series is ~15k daily
values; `dataZoom` is what makes that browsable.

**The line is coloured on the active variable's scale**, by a continuous `visualMap` built
from `store.activeStops` — the same stops the map and the ranking dots read, so a value has
one colour everywhere and dragging the colour range recolours the chart with the map (and,
like the map, fetches nothing). It was a hard-coded diverging ±3 ramp, which is right for
`anom` and nonsense for `sst`, where every ocean temperature above 3 °C sat pinned red. Two
things about it fail quietly: an explicit `series.lineStyle.color` **beats** the visualMap
rather than losing to it, so the fallback colour is only set when there are no stops; and
the y-axis needs `scale: true` plus a zero `markLine` drawn **only for `anom`** — either one
alone drags a 25–30 °C SST record down against a 0–30 axis and draws it as a flat line.


## Gotchas

- **`--env-file .env.dev` is required** on every compose invocation, as above.
- **`CH_IMAGE_TAG` must be >= the version that wrote `CH_DATA_DIR`.** ClickHouse has no
  downgrade path. Dev runs `clickhouse-server:latest`, so a data directory copied from a
  dev box to prod carries whatever major was current — 26.5.1.882 for the first copy —
  and prod's original `25.8` pin could not load its metadata. **The symptom is not a crash**:
  the container comes up and answers `/ping`, so the healthcheck passes, `SHOW TABLES`
  returns nothing, `api`'s `/health` reports ClickHouse unreachable, and `front` — which
  waits on `api` being healthy — never serves. Read the version off the source archive's own
  log before pinning:
  `grep -ao 'Starting ClickHouse [0-9.]*' <CH_LOG_DIR>/clickhouse-server.log | tail -1`.
- **`API_INTERNAL_BASE_URL` is `http://api:4000`, not the published `API_PORT`.** `API_PORT`
  (9021) is the *host* side of the mapping; inside the compose network uvicorn is on 4000.
  Pointing SSR at `api:9021` is the same `ECONNREFUSED` the dev notes warn about, just
  arriving from the other direction — and it surfaces as a plain 500 from the frontend
  while the API itself is fine on the published port.
- **Env vars are baked in at container creation.** Editing `.env.dev` does not affect a
  running container — use `up -d --force-recreate <service>`, not `restart`.
- **`domain.yml` changes need an API restart.** `shared.domain` caches the parsed YAML with
  `lru_cache`, and uvicorn's `--reload` only watches `.py` files, so a colour-range or
  region edit is invisible until the container is restarted. A **colour** change needs
  nothing more — the cached images carry data, not colour, so the palette is applied in the
  browser. Only a change to the grid or the encoding invalidates `./data/images`.
- **`UID`/`GID` in `.env.dev`** are what stop `api` writing root-owned cache files into the
  bind-mounted `./data`. Set them to your own `id -u` / `id -g`.
- **The `process` venv lives at `/opt/venv`, not `/app/.venv`** — compose bind-mounts
  `./process` over `/app`, which would hide anything installed under it.
- **`api` needs `netCDF4` too.** It renders the head of the archive (buckets still inside
  the retention window) from file. Historical frames come from the image cache, never from
  ClickHouse.
- **The MHW category's on-disk encoding changes on 2024-07-01**, at a URL and filename that
  do not. Anything reading `heatwave_category` must bound the value at 1..5 rather than
  testing a sign or a fill value — see the source section above for what a floor alone
  costs. The check that catches it in one line, and which belongs in any future test suite:
  `SELECT count() FROM mhw_daily WHERE cat > 5` must be 0.
- **A missing `mhw_daily` row is not a zero, it is an unknown.** The table is sparse by
  design, and the LEFT JOIN that restores its zeros cannot tell heatwave-free ocean from a
  date that was never ingested. Anything new that reads it must either go through that join
  *and* respect `/coverage`'s `mhw.complete`, or be honest that it is reporting zeros it
  cannot vouch for.
- **A corrupt climatology file fails the *daily* ingest, and the traceback blames the daily
  file.** `ingest.read_day()` reads the climatology to compute `has_clim`, so one unreadable
  clim file takes out that MMDD **in every year at once** while the daily files are fine.
  Found in practice: `day1106` had silently bit-rotted on disk months after `init` loaded
  it, and every 1988–2011 Nov 6 failed with `NetCDF: HDF error`. The signature to recognise
  is *one MMDD missing across many years*; `CRW.cli verify-clim` is the check, and
  re-downloading that one file plus `backfill --product sst` is the whole fix.
  - Only the older half failed because the backfill ran newest-first and the rot appeared
    partway through — so the boundary year says when, not what.
  - **The file gives nothing away**: it opened, listed all its variables, and had a byte
    count identical to the re-downloaded copy. Only a full read of `analysed_sst` raised.
    That is why `verify_files()` reads the whole array and `missing_files()` is not a
    substitute — the file was present the entire time.
  - **ClickHouse was unaffected** (`sst_clim` still had all 366 keys, loaded before the
    rot), and so was the imagery, which had been rendered before it. Only the ingest of
    dates processed after the rot was lost.
- **Never truncate `sst_daily` without `ingest_status`.** Emptying one and not the other
  makes every day look already-ingested, and `ingest_files` then issues an
  `ALTER … DELETE` mutation per day against rows that do not exist — on a full archive that
  is ~15 k synchronous no-op mutations and dwarfs the inserts they precede. `backfill
  --fresh` does both together for exactly this reason.
- **An ECharts `piecewise` visualMap whose pieces are exact `value`s draws the line
  invisible.** ECharts turns the pieces into a y-axis gradient, and a `{ value: 1 }` piece
  becomes a *zero-width* band with `stop-opacity: 0` on both sides — so every value that is
  not exactly a class gets no ink at all. `mhw`'s chart shipped that way: the pane looked
  empty while still answering clicks (the ZRender handler is on the canvas, not the line),
  and in region scope nothing could ever show, since an area mean of classes never lands on
  an integer. The pieces are half-open bands (`gte`/`lt`) now, which also matches the map's
  `step` ramp. A region's series is not a category in two more places: it is printed
  rounded rather than named, and its y-axis is not pinned to 0..5 — an archive that never
  leaves 0..1.5 drawn against five classes is a flat line on the axis floor.

- **Nuxt UI's own icons default to the `lucide` collection**, and only `@iconify-json/mdi`
  is installed. A component reaching for one of its internal icons (`UModal`'s close button
  is the first that does) logs `Collection lucide is not found locally` and renders nothing.
  `app/app.config.ts` remaps them; add an entry there rather than installing lucide.
- **Nuxt UI v4 renamed `UButtonGroup` to `UFieldGroup`**, and the old name resolves to an
  empty comment node instead of erroring — the control simply vanishes from the DOM.
- **A prefetched image must set `crossOrigin = 'anonymous'`.** Mapbox fetches an image
  source in CORS mode, while a bare `new Image()` sends no `Origin` — and the API's
  `CORSMiddleware` only answers with `Access-Control-Allow-Origin` when it sees one. Warming
  the cache without it parks a header-less response that Mapbox's own fetch then reuses and
  the browser blocks, so every prefetched frame fails and the map freezes on one image.
- **The API's ClickHouse client is per-*thread*, not per-process**
  (`modules/clickhouse_helpers.py`). Every endpoint is a sync `def`, so FastAPI runs it in
  the thread pool, and a client's session refuses a second concurrent query with
  `ProgrammingError: Attempt to execute concurrent queries within the same session`. A
  shared singleton fails ~7 of 8 overlapping requests. Symptom to recognise: intermittent
  500s under no real load, and a **map that silently keeps showing the previous frame** —
  Mapbox's `ImageSource` never retries a failed image and logs the error only to the
  browser console.
- **`CRW.cli render` runs its pool under `multiprocessing`'s `spawn` context.** Workers
  each open their own NetCDF handles and HDF5 is not fork-safe once a file has been
  touched in the parent.
- **`/image` renders on demand but never caches** (`api/modules/render.py`). Only
  `process` writes the cache, because only it knows whether the retention window still
  holds days that have yet to land in a bucket. So an unrendered bucket costs ~2.9 s
  (daily) to ~8.7 s (monthly) on **every** request, and the browser's playback prefetch
  warms 8 frames at a time — enough to saturate the API's thread pool and stall unrelated
  requests behind it. Symptom: the map is slow, `data/images/` is empty, and a `curl` to
  the API appears to hang. Fix is to run `CRW.cli render`.

## Status / not yet built

Verified working end to end on the CoralTemp pipeline:

- **Geometry and orientation.** Subset is 2500×3800; 7,477,923 ocean cells/day and
  7,240,513 with climatology, matching the source exactly. Anomaly range −4.14…+6.34 for
  a sample day (a latitude flip gives ±18).
- **Ingest round-trip.** Eight cells checked against the source NetCDF, **including three
  straddling the antimeridian** — 180.025°E and −179.975°E resolve to the same cell,
  179.975°E to its western neighbour. Land correctly absent from the table.
- **Point anomaly.** API `sst` and `anom` match a direct NetCDF computation to the cent
  across eight days.
- **The region identity.** `/region/nino34?variable=anom` equals a direct cell-wise
  `avg(sst - clim)` over the same box to three decimals, on 201,392 cells.
- **The region ranking.** `/region/nino34/monthlyRanking?variable=anom` puts August 2015 at
  rank 2 with a mean of **1.980**, which is exactly the mean of the 31 daily values
  `/region/nino34?start=2015-08-01&end=2015-08-31` returns — the fold and the chart agree
  because they read the same rollup. 15 ms. January's rank 1 is 2016 at +2.51 and August's
  top three are 2026 (partial), 2015, 1997, which are the El Nino years they should be.
  In the browser: the panel draws in region scope with an "area mean" caption, the tooltip
  reads `sd of daily means`, the CSV downloads as `anom_monthly-ranks_nino-3-4.csv` with
  500 rows, and `mhw` ranks the region without printing a category name. No console
  errors.
- **Imagery.** Land transparent, ice-fringe grey, ocean coloured — checked by pixel, not by
  eye. `bounds()` returns west 100 / east 290. `/image` 404s with an explanatory message
  for a bucket with neither cache nor NetCDF.
- **Browser** (Chromium, per the recipe below): the Pacific raster draws across the
  antimeridian, SST/Anomaly and Daily/Weekly/Monthly toggles render, a map click populates
  the chart and the ranks dock, no console errors.

Verified on the Marine Heatwave addition:

- **Ingest.** The per-category histogram in `mhw_daily` for 2023-10-01 matches the source
  NetCDF exactly across all five categories (2,355,517 / 681,076 / 60,027 / 3,618 / 305).
  Land is absent; the 16-day trial slice came to 49,284,004 rows, 3.08 M/day at that
  archive-peak date.
- **Point queries.** Eight cells checked against the NetCDF, the antimeridian pair
  included — 180.025°E and −179.975°E resolve to the same cell. A heatwave-free ocean cell
  returns **0**, not an absent row, which is the LEFT JOIN doing its job; a land cell
  returns nothing.
- **Weekly max.** At one cell over three weeks the weekly series is the max of its daily
  values, not their mean.
- **The region path.** `/region/nino34?variable=mhw` for 2023-10-01 gives 0.855, matching a
  hand-written `sum(cat*cos)/sum(cos)` over the same box to three decimals.
- **Imagery.** By pixel, not by eye: the rendered WebP is opaque *only* where a heatwave is
  (1,087,645 px), alpha is strictly 0 or 255 with nothing in between, and the only codes
  present are 1..5. Per-category proportions match the source. At 81 KB it is a sixth of
  the SST frame.
- **Nearest resampling, measured.** Bilinear would invent 42,088 heatwave pixels (3.9%
  more than exist) and mis-categorise 3,072 more.
- **Browser** (Chromium): the MHW toggle renders and switches, the map draws NOAA's palette
  with land and heatwave-free ocean transparent, the legend is a named key, the chart's
  y-axis is 0..5 with no unit, the ranks dock reads "most severe first" and draws
  zero-mean months neutral rather than Cat 1 yellow. No console errors. Two apparent bugs
  were checked and were not: the Gulf of Mexico and Hudson Bay really were at Cat 1 that
  day and sit inside the box's 70°W edge.

**Browser verification works here.** Headless Chromium renders the Mapbox canvas fine under
ANGLE/SwiftShader; launch it with
`args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']`. Note the
host's default node is 18 and Playwright needs 20+, so run it under
`$HOME/.nvm/versions/node/v22.23.1/bin`. Playwright is not a dependency of this repo —
`npm i playwright --no-save` into a scratch dir, and since a fresh install will not match
the browsers already in `~/.cache/ms-playwright`, pass `executablePath` at that cache's
`chromium-<build>/chrome-linux64/chrome` rather than downloading another one.

**Do not read the map back off its own canvas.** Mapbox runs with
`preserveDrawingBuffer: false`, so `drawImage(mapCanvas)` yields a blank frame and a
colour-count assertion on it fails even when the map is drawn correctly. Take a Playwright
screenshot and look at that instead.

Chart maths can be rendered head-lessly with echarts' SSR mode and asserted on, which is
much cheaper than driving a browser. That check does **not** catch mount-order bugs — the
ranking grid first shipped blank because its canvas sits inside `<ClientOnly>`, so
`container.value` was still null at `onMounted` and nothing ever observed it. Watch the
template ref, not the mount.

Not built yet: the full-archive backfill and render pass for **both** products (in
progress — MHW ingest runs at ~2.9 days/s, so ~90 min for the archive), the region-query
benchmark that decides whether `region_daily` is needed, a cron entry for `run`, PostHog
analytics, tests.

**Until the MHW backfill finishes, `/coverage` reports `mhw.complete: false` and the
frontend's MHW toggle stays disabled** — deliberately, see the sparse-table note above.
