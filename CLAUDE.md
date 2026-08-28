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

**Pipeline CLI** (the `process` service idles on `sleep infinity` and is driven on demand):
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm process \
  python -m CRW.cli <command>

python -m CRW.cli init                                    # tables + climatology + region means
python -m CRW.cli scan     [--limit N]                    # disk vs. already ingested
python -m CRW.cli backfill [--start|--end] [--reverse] [--fresh] [--delete-nc]
python -m CRW.cli render   [--start|--end] [--variable|--period] [--workers N] [--force]
python -m CRW.cli run      [--date] [--keep-nc] [--recheck-days N]
python -m CRW.cli status                                  # per-status day/row counts
```

`init` is not just DDL — it loads all 366 climatology files (2.68 B rows, ~20 min) and
then builds `region_clim`. It is idempotent and resumable: an interrupted load skips the
MMDD keys already present.

**ClickHouse client:**
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev exec db-ch \
  clickhouse-client --database enso --query "SHOW TABLES"
```

**Render the image cache in bulk:**
```bash
docker compose -f docker-compose.dev.yml --env-file .env.dev run --rm --no-deps process \
  python -m CRW.cli render --workers 12
```
**This reads NetCDF, not ClickHouse**, so it has a hard prerequisite: the daily archive
must still be on disk. Run it before `backfill --delete-nc` and before the daily retention
prune has eaten the range. Afterwards the source for those frames is gone.

`render` and `run` are the same code path — both go through `imaging.bucket_mean()`, so
a change to how a week is averaged cannot apply to one and not the other. `run` renders
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

**The climatology is a second archive**: 366 files in `./data/climatology/`, mounted at
`/opt/data/climatology/`, one per MMDD **including `day0229`** — so there is no leap-day
mapping rule to invent. Baseline 1991–2020. 1.6 GB, static, and **kept forever**: image
rendering reads it straight off disk.

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

Both containers mount `./shared` at `/app/shared`. Five modules:

- **`domain.py` + `domain.yml`** — grid geometry, variable metadata, named region boxes.
  Describes **two** grids and the distinction matters: `global` is the full 7200×3600
  CoralTemp grid that `gy`/`gx` index; `subset` is the Pacific box actually ingested.
- **`fields.py`** — NetCDF reading, and the single home of both orientation rules above.
- **`render.py`** — field array → Web-Mercator WebP. **Takes arrays, never a DB client.**
- **`periods.py`** — daily/weekly/monthly buckets, shared by query and render.
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

**`region_clim`** — 8 regions × 366 MMDD = **2,928 rows**. The entire precomputation layer.

**`ingest_status`** — `ReplacingMergeTree(updated_at) ORDER BY date`, one row per day.

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

**Region timeseries are queried live, not precomputed.** Named regions are 0.6–14.6 B rows
over the archive (Niño 3.4 is 3.04 B; the PDO box, the largest, 14.55 B), and
`ORDER BY (gy, gx, date)` makes a box a set of contiguous key ranges — one per `gy` — not a
scan. A `region_daily` rollup would be 8 × 15,211 = 122 k rows and can be added later as a
pure cache without touching the big table, so it is deliberately absent until measurement
says otherwise.

### Process pipeline (`process/`)

Entry point `process/CRW/cli.py` (`python -m CRW.cli`). Modules:
- `config.py` — filename parsing, `scan()`
- `download.py` — NOAA CRW fetching
- `ingest.py` — NetCDF → ClickHouse
- `climatology.py` — the 366-file load and `region_clim`
- `imaging.py` — day/week/month × sst/anom rendering, and the retention window
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

Per date: `download → ingest → render 6 images → prune retention window`.

#### The retention window

A weekly frame is the mean over seven days, but `run` deletes each `.nc` after ingesting
it — so days N−6…N−1 are gone by the time day N is processed. **`imaging.retention_floor()`
keeps the files the open week and month still need** (at most ~37 files, ~380 MB) and
prunes only what has fallen out of both. Losing that window does not corrupt anything, but
it freezes weekly and monthly frames at whatever was last rendered.

### API (`api/`)

FastAPI in `SERVER.py`. **Timeseries are read live from ClickHouse; imagery is not.**

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + ClickHouse reachability |
| `GET /domain` | grid extent, image bounds, variable metadata, per-variable colour stops, `noClimColor`, region list |
| `GET /coverage` | ingested date range, row count, climatology completeness |
| `GET /variables` | variable list, with `derived` on `anom` |
| `POST /timeseries` | `{lat, lon, start?, end?, period?, variable?}` → record at the nearest cell |
| `POST /regionTimeseries` | `{lat: [a,b], lon: [a,b], ...}` → area-mean over an arbitrary box |
| `GET /region/{key}` | same, for a named `domain.yml` region, using `region_clim` |
| `POST /monthlyRanking` | every complete calendar month at a cell, ranked within its month-of-year |
| `GET /image/{date}.webp` | one bucket as a Web-Mercator WebP |

**`variable` — `sst` (default) / `anom`** — is accepted by every endpoint above.

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

**`/monthlyRanking` is always monthly, whatever the caller's `period`**, and it ranks
`anom` by default because ranking years by absolute SST is a different question. Every
month is ranked including the archive's truncated edge months, which carry `partial: true`
— the month in progress is the one people most want to look at, so it is starred rather
than hidden. A month missing an *interior* day is **not** partial: it is as complete as it
will ever be.

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
so `/domain` sends a different range per variable: `anom` tabulates its whole encoding range
(−12.8…12.7), which puts the sentinel in a slot of its own and lands exactly one code per
step; `sst` has no sentinel and spends all 256 entries on the −2…32 display range.

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

### Frontend (`front/`)

Nuxt 4 + Nuxt UI v4 (Tailwind v4) + Pinia + MapboxGL + ECharts, dark mode pinned — the
same stack as the ocean-acidification dashboard.

```
app/app.vue                        header + coverage badge; awaits store.loadMetadata()
app/pages/index.vue                map + point chart rail on the left, ranks dock on the right
app/components/AnomalyMap.vue      MapboxGL + the field image source
app/components/TimeControl.vue     variable + period toggles, date stepper, playback
app/components/ColorLegend.vue     gradient from the active variable's colorStops
app/components/TimeseriesChart.vue ECharts line with dataZoom
app/components/MonthlyRankingBrowser.vue  month rail + one month's year ranking
app/components/SideDock.vue        resizable right-hand dock (drag handle, remembered width)
app/composables/useApi.ts          axios wrapper
app/composables/usePlayback.ts     play/stop loop + frame prefetch for the map animation
app/utils/periods.ts               daily/weekly/monthly bucket maths (mirrors the API)
app/utils/ranking.ts               ranking layout + both ECharts options (pure -> testable headlessly)
app/utils/colorScale.ts            domain.yml's colour stops evaluated at a single value
app/app.config.ts                  maps Nuxt UI's internal icons onto mdi
app/stores/main.ts                 Pinia store
```

**The chart rail is point-only, and the map click is the only selection.** There was a
`Point | Region mean` tab pair here; the region side is gone from the UI, though
`/region/{key}` and `/regionTimeseries` still exist and now take `variable` too. The Niño
indices are the obvious thing to surface there next.

**`store.variable` (`sst` | `anom`) works exactly like `store.period`**: it drives the
chart request and the image URL together, so the map and the chart can never show
different fields. It opens on `sst` — the stored variable, defined on every ocean cell —
while `anom` is undefined over the ice fringe and needs the full 366-key climatology, so
the Anomaly button stays **disabled until `/coverage` reports `climatology.complete`**. A
partly-loaded climatology would blank the missing dates rather than fail, which is worse.

The monthly ranking **refetches on a variable change** but not on a period change: ranking
years by absolute SST is a different question from ranking by anomaly, whereas the ranking
is period-independent by construction.

**The monthly-ranking browser lives in a dock beside the map**, not over it. It needs
full page height — one month of ~45 years is already taller than the chart rail — but it
was a fullscreen modal first, and that made picking a cell cost close / click / reopen
every time, which is unusable if you want to compare ten cells. `SideDock.vue` is a plain
right-hand column: the map keeps its clicks, the store updates, and the panel follows.

Its width is the user's, not a constant — the dock and the map want the same pixels, so
there is a drag handle on its left edge (arrow keys too) and the width is remembered in
`localStorage` under `enso.ranksDock.width`. The floor is **420px**, which is where the
rail plus the panel's fixed 66px of rank labels stop leaving a readable plot; the ceiling
leaves 380px for the map. `MonthlyRankingBrowser` is therefore sized by **container
queries** (`@container` on its root, `@md`/`@lg` variants) rather than viewport
breakpoints — the rail narrows, and the "warmest YYYY" captions and the panel's colour
legend drop out, when the dock is dragged in. Its `ResizeObserver` redraw is debounced
100ms because a drag fires it every frame and one redraw is twelve thumbnails plus a
45-row panel.

Inside the dock it is **master–detail, not a grid**: a scrollable left rail of twelve
thumbnails, and the selected month drawn full size beside it. Twelve panels at once was
the first shape and was legible only by scrolling ~1800px, which is what killed it.

Both ECharts options are built by `utils/ranking.ts` rather than inside the SFC, for the
same reason `periods.ts` exists: being pure functions of their inputs they can be rendered
head-lessly with echarts' SSR mode and asserted on, which is much cheaper than driving a
browser for chart maths.

- **The rail's thumbnails are marks only** — no axes, no labels. A month's name is HTML
  next to its canvas, where it stays crisp, focusable and selectable; the card is the
  click target. At ~1.4px per year, whiskers and tick labels would be mush, so what
  survives the size is the spine's shape and its colour, which is what the rail is
  scanned for.
- **The rail shares one x-domain across all twelve; the open month scales to itself.**
  Comparability is the rail's job now, so the detail is free to spend its full width on
  the month being read — at a cell whose August reaches +6 °C, a shared domain left
  January using a third of the pane. `xDomainOf()` serves both: pass all twelve months
  for the rail, the one month for the detail.
- **The detail's row pitch is spent out of the pane's height** (`detailPitch()`, clamped
  to 9–22px), so 45 years normally fit with no scrolling at all. Only a short pane hits
  the floor and lets the panel overflow. Measure the scroll container, not the section —
  the heading sits outside it deliberately.
- **Picking a month in the rail does not move the map.** `activeMonth` is seeded from
  `selectedDate`'s month and re-seeded whenever the map moves to another month, but it is
  local state; only clicking a *row* emits `select`.

Three encodings, three separate jobs, deliberately not overlapping: **dot colour** is the
value on the active variable's scale — the same colour that cell has on the map;
**label weight and ink** mark the top N (never hue, which already means anomaly); **amber**
is "where the map is", matching `TimeseriesChart`'s `MAP` markLine — the map's year is
ringed in the detail and in every thumbnail, and the map's month name is amber in the rail.

**A `partial` month is starred, not hidden.** The API ranks the archive's edge months
with the rest, and the browser marks them in three places: the detail's row label reads
`15. 2026 *`, its dot is drawn **open** (no fill, its own colour as the stroke) so it
reads on the same scale without looking like a settled datum, and the rail's dot for it
is faint (`opacity 0.45`) since there is no label out there to carry a star. The star is
cashed in by `partialNote()` — one dimmed line above the plot, "August 2026 is incomplete
— ranked on 24 of 31 days" — which sits *outside* the scroll pane, so it costs the pitch
a row's worth of height and nothing else. The rail's "warmest YYYY" caption gets the star
only when the partial month is actually rank 1.

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

**The map opens on the whole box, in Mercator.** The old `INITIAL_NORTH = 68` clip existed
because the OISST domain ran to 90°N and that strip dominated a Mercator fit; this box
stops at 65°N and fits without a sliver, so the clip is gone. The projection is
`mercator`, not `globe`: a globe hides half a single basin behind the limb at the opening
zoom.

**`imageBounds.east` is 290, not −70, and is passed to Mapbox as-is.** Normalising it into
−180…180 would make west > east and collapse the image source — see the Map imagery
section for the verification.

Stepping days calls `ImageSource.updateImage()` rather than removing and re-adding the
layer, so the basemap does not flash between frames.

**Charts**: always ECharts, never a hand-rolled `<canvas>`. The point series is ~15k daily
values; `dataZoom` is what makes that browsable.


## Gotchas

- **`--env-file .env.dev` is required** on every compose invocation, as above.
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
- **Never truncate `sst_daily` without `ingest_status`.** Emptying one and not the other
  makes every day look already-ingested, and `ingest_files` then issues an
  `ALTER … DELETE` mutation per day against rows that do not exist — on a full archive that
  is ~15 k synchronous no-op mutations and dwarfs the inserts they precede. `backfill
  --fresh` does both together for exactly this reason.
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
- **Imagery.** Land transparent, ice-fringe grey, ocean coloured — checked by pixel, not by
  eye. `bounds()` returns west 100 / east 290. `/image` 404s with an explanatory message
  for a bucket with neither cache nor NetCDF.
- **Browser** (Chromium, per the recipe below): the Pacific raster draws across the
  antimeridian, SST/Anomaly and Daily/Weekly/Monthly toggles render, a map click populates
  the chart and the ranks dock, no console errors.

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

Not built yet: the full-archive backfill and render pass (in progress), the region-query
benchmark that decides whether `region_daily` is needed, a cron entry for `run`, production
compose files, PostHog analytics, tests.
