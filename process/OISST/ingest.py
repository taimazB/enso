"""NetCDF -> ClickHouse ingest for OISST daily SST anomaly.

Each file holds one day of the `anom` variable on a regular 0.25-degree lat/lon
grid, encoded as `short` counts of 0.01 degC with `_FillValue = -999` over land.
Those raw counts go into ClickHouse untouched (see `shared.ch` for why), keyed by
their index into the *global* OISST grid so a wider re-subset later needs no
re-ingest.
"""

from __future__ import annotations

import datetime as dt
import logging

import netCDF4
import numpy as np
from shared.ch import DATABASE, STATUS_FAILED, STATUS_INGESTING, STATUS_SUCCESS
from shared.domain import global_grid, subset, variable

from . import status as status_mod
from .config import NcFile

log = logging.getLogger(__name__)

COLUMNS = ["date", "gy", "gx", "anom_raw"]

# `time` is "days since 1978-01-01 12:00:00" in every OISST file.
_TIME_EPOCH = dt.date(1978, 1, 1)


def read_day(nc: NcFile) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return `(gy, gx, anom_raw)` for the valid (ocean) cells of one file."""
    var = variable("anom")
    grid = global_grid()

    with netCDF4.Dataset(nc.path) as ds:
        anom = ds.variables["anom"]
        # Raw shorts, not the masked/scaled floats netCDF4 would hand back —
        # the scale factor is reapplied by the `anom` ALIAS column in ClickHouse.
        anom.set_auto_maskandscale(False)
        raw = np.asarray(anom[0, 0, :, :])

        lat = np.asarray(ds.variables["lat"][:], dtype="float64")
        lon = np.asarray(ds.variables["lon"][:], dtype="float64")

        file_date = _TIME_EPOCH + dt.timedelta(days=float(ds.variables["time"][0]))
        if file_date != nc.date:
            raise ValueError(
                f"{nc.filename}: time variable says {file_date}, filename says {nc.date}"
            )

    if raw.shape != (lat.size, lon.size):
        raise ValueError(f"{nc.filename}: anom shape {raw.shape} != ({lat.size}, {lon.size})")

    gy_axis = grid.gy(lat)
    gx_axis = grid.gx(lon)
    _check_within_subset(nc, lat, lon)

    valid = raw != var.fill_value
    iy, ix = np.nonzero(valid)
    return (
        gy_axis[iy].astype("uint16"),
        gx_axis[ix].astype("uint16"),
        raw[iy, ix].astype("int16"),
    )


def _check_within_subset(nc: NcFile, lat: np.ndarray, lon: np.ndarray) -> None:
    """Warn (do not fail) when a file falls outside the configured subset box.

    Ingest is deliberately tolerant here: cell indices are global, so a file from
    a wider re-subset is still storable and queryable. Only `domain.yml`'s
    `subset` block — which drives map extents — would be out of date.
    """
    box = subset()
    if (
        lat.min() < box.lat_min - 0.125
        or lat.max() > box.lat_max + 0.125
        or lon.min() < box.lon_min - 0.125
        or lon.max() > box.lon_max + 0.125
    ):
        log.warning(
            "%s covers lat %.3f..%.3f lon %.3f..%.3f, wider than domain.yml's subset "
            "(lat %.3f..%.3f lon %.3f..%.3f) — update the subset block",
            nc.filename,
            lat.min(),
            lat.max(),
            lon.min(),
            lon.max(),
            box.lat_min,
            box.lat_max,
            box.lon_min,
            box.lon_max,
        )


def delete_day(client, date: dt.date) -> None:
    """Remove an already-ingested day so it can be replaced.

    `sst_anom` is a plain MergeTree, so this is a mutation that rewrites the
    affected parts of the day's year-partition (~35M rows). That is expensive,
    which is fine because it only happens when a preliminary file is superseded
    by its final version — roughly the last two weeks of the archive.
    """
    client.command(
        f"ALTER TABLE {DATABASE}.sst_anom DELETE WHERE date = %(date)s",
        parameters={"date": date},
        settings={"mutations_sync": 2},
    )


def ingest_files(
    client,
    files: list[NcFile],
    *,
    force: bool = False,
    batch_days: int = 30,
) -> dict[str, int]:
    """Ingest a list of files, skipping days already loaded from the same file.

    Files are accumulated into batches of `batch_days` before a single insert —
    ~96k rows per day means a per-file insert would create ~16k tiny parts for a
    full-archive load, and ClickHouse merges are far happier with fewer, larger
    ones.
    """
    existing = status_mod.load(client)
    counts = {"ingested": 0, "skipped": 0, "failed": 0, "rows": 0}

    pending: list[NcFile] = []
    rows_per_day: list[int] = []
    buffers: list[list] = [[], [], [], []]

    def flush() -> None:
        if not pending:
            return
        try:
            client.insert(
                f"{DATABASE}.sst_anom",
                buffers,
                column_names=COLUMNS,
                column_oriented=True,
            )
        except Exception as exc:  # noqa: BLE001 — recorded per-day below
            log.exception("insert failed for %d day(s)", len(pending))
            for nc in pending:
                status_mod.record(client, nc, STATUS_FAILED, message=str(exc)[:500])
                counts["failed"] += 1
        else:
            for nc, n_rows in zip(pending, rows_per_day):
                status_mod.record(client, nc, STATUS_SUCCESS, n_rows=n_rows)
                counts["ingested"] += 1
                counts["rows"] += n_rows
        finally:
            pending.clear()
            rows_per_day.clear()
            for buf in buffers:
                buf.clear()

    for nc in files:
        row = existing.get(nc.date)
        if not force and status_mod.is_current(row, nc):
            counts["skipped"] += 1
            continue

        try:
            gy, gx, anom_raw = read_day(nc)
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the run
            log.exception("failed to read %s", nc.filename)
            status_mod.record(client, nc, STATUS_FAILED, message=str(exc)[:500])
            counts["failed"] += 1
            continue

        # A day that was previously loaded must be cleared first: sst_anom is a
        # plain MergeTree and would otherwise end up with both versions.
        if row is not None and row["status"] == STATUS_SUCCESS:
            flush()
            log.info("replacing already-ingested %s", nc.date)
            delete_day(client, nc.date)

        status_mod.record(client, nc, STATUS_INGESTING)

        n = gy.size
        buffers[0].extend([nc.date] * n)
        buffers[1].extend(gy.tolist())
        buffers[2].extend(gx.tolist())
        buffers[3].extend(anom_raw.tolist())
        pending.append(nc)
        rows_per_day.append(n)

        if len(pending) >= batch_days:
            log.info("inserting %d day(s) up to %s", len(pending), nc.date)
            flush()

    flush()
    return counts
