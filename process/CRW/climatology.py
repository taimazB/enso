"""Load the 366-file daily climatology, and the per-region means derived from it.

The climatology is static — a 1991-2020 baseline that only changes if NOAA
re-baselines the product — so both of these run once at `init` rather than per
date.

`sst_clim` is ordered `(gy, gx, mmdd)`: the *timeseries* ordering. A point
anomaly series reads 366 contiguous rows for its cell. Whole-day reads across
all cells are deliberately not served here, because image generation reads the
climatology NetCDF directly instead — the 366 files are 1.6 GB and kept forever.
"""

from __future__ import annotations

import logging

import numpy as np
from shared.ch import DATABASE
from shared.domain import global_grid, regions, subset
from shared.fields import CLIM_DIR, clim_path, read_clim_raw, valid_mask

log = logging.getLogger(__name__)

COLUMNS = ["mmdd", "gy", "gx", "clim_raw"]

# Every MMDD CoralTemp ships, 02-29 included — so no leap-day rule is needed.
MMDD_KEYS: tuple[int, ...] = tuple(
    month * 100 + day
    for month, days in zip(range(1, 13), (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31))
    for day in range(1, days + 1)
)


def missing_files(clim_dir=None) -> list[int]:
    """MMDD keys with no file on disk."""
    out = []
    for mmdd in MMDD_KEYS:
        try:
            clim_path(mmdd, clim_dir)
        except FileNotFoundError:
            out.append(mmdd)
    return out


def load_climatology(client, *, force: bool = False, batch: int = 5) -> dict[str, int]:
    """Ingest every climatology file into `sst_clim`.

    2.65 billion rows, about 2 GB. Idempotent: unless `force`, an MMDD already
    present is skipped, so an interrupted load resumes.
    """
    grid, box = global_grid(), subset()
    gy0, _ = box.gy_range(grid)
    gx0, _ = box.gx_range(grid)

    present: set[int] = set()
    if not force:
        present = {
            row[0]
            for row in client.query(
                f"SELECT DISTINCT mmdd FROM {DATABASE}.sst_clim"
            ).result_rows
        }
    elif client.query(f"SELECT count() FROM {DATABASE}.sst_clim").result_rows[0][0]:
        log.info("force: truncating sst_clim")
        client.command(f"TRUNCATE TABLE {DATABASE}.sst_clim")

    counts = {"loaded": 0, "skipped": 0, "rows": 0}
    buffers: list[list] = [[] for _ in COLUMNS]
    pending = 0

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        client.insert(
            f"{DATABASE}.sst_clim", buffers, column_names=COLUMNS, column_oriented=True
        )
        for buf in buffers:
            buf.clear()
        pending = 0

    for mmdd in MMDD_KEYS:
        if mmdd in present:
            counts["skipped"] += 1
            continue
        raw = read_clim_raw(mmdd)
        iy, ix = np.nonzero(valid_mask(raw))
        n = int(iy.size)
        buffers[0].extend([mmdd] * n)
        buffers[1].extend((iy + gy0).astype("uint16").tolist())
        buffers[2].extend((ix + gx0).astype("uint16").tolist())
        buffers[3].extend(raw[iy, ix].astype("int16").tolist())
        pending += 1
        counts["loaded"] += 1
        counts["rows"] += n
        if pending >= batch:
            log.info("inserting climatology up to %04d (%d days)", mmdd, pending)
            flush()

    flush()
    return counts


def build_region_clim(client) -> int:
    """Fill `region_clim`: each named region's cos(lat)-weighted mean per MMDD.

    8 regions x 366 = 2,928 rows, and it is the *entire* precomputation layer.
    A region anomaly series needs no join against `sst_daily` because

        mean(sst - clim) == mean(sst) - mean(clim)

    when both average the same cells with the same weights — so only this side,
    which depends on (region, mmdd) alone, has to be materialised.

    The identity's precondition is what `sst_daily.has_clim` enforces on the
    other side: the daily mean must be restricted to cells that appear here.
    """
    grid = global_grid()
    rows: list[list] = []

    for key, region in regions().items():
        gy0, gy1 = sorted(int(grid.gy(v)) for v in region.lat)
        gx0, gx1 = sorted(int(grid.gx(v)) for v in region.lon)
        result = client.query(
            f"""
            SELECT mmdd,
                   sum(clim * cos(lat * pi() / 180)) / sum(cos(lat * pi() / 180)) AS mean_clim,
                   count() AS n_cells
            FROM {DATABASE}.sst_clim
            WHERE gy BETWEEN %(gy0)s AND %(gy1)s
              AND gx BETWEEN %(gx0)s AND %(gx1)s
            GROUP BY mmdd
            ORDER BY mmdd
            """,
            parameters={"gy0": gy0, "gy1": gy1, "gx0": gx0, "gx1": gx1},
        ).result_rows
        for mmdd, mean_clim, n_cells in result:
            rows.append([key, int(mmdd), float(mean_clim), int(n_cells)])
        log.info("region_clim: %s -> %d mmdd rows", key, len(result))

    if rows:
        client.insert(
            f"{DATABASE}.region_clim",
            rows,
            column_names=["region", "mmdd", "mean_clim", "n_cells"],
        )
    return len(rows)
