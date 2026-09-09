"""Turn a region polygon into the grid cells it covers.

A region in `domain.yml` is normally a lat/lon box, and a box needs nothing more
than `WHERE gy BETWEEN ... AND gx BETWEEN ...`. A few real-world regions are not
boxes — the BC EEZ is a 200-nautical-mile arc with two negotiated lateral
boundaries — and for those the box is only a *prefilter*: the cells it selects
have to be narrowed again to the ones actually inside the polygon.

**That narrowing is materialised, not evaluated per query.** The rasterisation
here runs once per region into `region_cells`, and every aggregation then reads
that table. It is the same reasoning as `region_daily`: a named region is asked
for over and over, and re-deciding which of its cells are in the zone on each
request would put a point-in-polygon test in the path of a 113-billion-row scan.

**A cell is in the region when its CENTRE is inside the polygon.** No partial
weighting: a 0.05-degree cell is ~5.5 km, the zone is ~450,000 km2, and the
boundary cells are a rim one cell thick. Weighting them by their covered
fraction would be a second, subtler definition of "in the region" that the
`count()` in `region_daily.n_cells` could not describe.

Nothing here decides what is OCEAN. The mask is pure geometry and includes the
land inside the zone; `sst_daily` holds ocean cells only, so the join that reads
this table drops the land — which is also why the stored polygon carries no
interior rings for the islands.
"""

from __future__ import annotations

import numpy as np
from matplotlib.path import Path

from .domain import GlobalGrid, Region


def cells_in(region: Region, grid: GlobalGrid) -> tuple[np.ndarray, np.ndarray]:
    """The `(gy, gx)` global cell indices whose centres fall inside `region`.

    Returns the box's own cells for a region with no polygon, so callers do not
    have to branch — though the box path has no reason to call this, since a box
    is expressible as a `BETWEEN` and a polygon is not.
    """
    gy0, gy1 = region.gy_range(grid)
    gx0, gx1 = region.gx_range(grid)
    gy = np.arange(gy0, gy1 + 1)
    gx = np.arange(gx0, gx1 + 1)
    GY, GX = np.meshgrid(gy, gx, indexing="ij")

    if region.polygon is None:
        return GY.ravel(), GX.ravel()

    # matplotlib rather than shapely: it is already a dependency of both services
    # (shared/render.py), `contains_points` is vectorised, and a mask is 60,669
    # tests for the BC EEZ -- milliseconds. A geometry library would be a new
    # wheel in two images for one predicate.
    lat = grid.lat(GY).ravel()
    lon = grid.lon(GX).ravel()
    points = np.column_stack([lon, lat])

    inside = np.zeros(len(points), bool)
    for ring in region.polygon:
        inside |= Path(np.asarray(ring, dtype="float64")).contains_points(points)

    return GY.ravel()[inside], GX.ravel()[inside]
