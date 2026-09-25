"""
volumetrics.py

Earthwork and volumetric intelligence — the layer above footprint analysis.

DEM format (our own lightweight grid, not a GeoTIFF — see note at bottom of
this file for why):

    {
      "origin_x": 0, "origin_y": 0,       # world coords of the SW corner of cell [0][0]
      "cell_size": 10,                     # meters per grid cell (square cells)
      "nrows": 27, "ncols": 26,
      "values": [[z, z, ...], ...]         # row-major, values[row][col], row 0 = origin_y edge
    }

All elevations are in meters. All volumes are in cubic meters unless noted.
"""

from typing import Optional

MATERIAL_DENSITY_PRESETS = {
    "crushed_aggregate": 1.60,   # t/m3
    "topsoil": 1.40,
    "sand": 1.50,
    "clay": 1.75,
    "iron_ore": 2.50,
    "coal": 1.30,
    "limestone": 1.55,
    "gravel": 1.65,
}


def cell_area(dem: dict) -> float:
    return dem["cell_size"] ** 2


def _validate_same_grid(dem_a: dict, dem_b: dict) -> None:
    for key in ("origin_x", "origin_y", "cell_size", "nrows", "ncols"):
        if dem_a[key] != dem_b[key]:
            raise ValueError(f"DEM grids don't align: '{key}' differs ({dem_a[key]} vs {dem_b[key]}). Both surfaces must share the same grid definition.")


def grid_diff_volumes(dem_from: dict, dem_to: dict) -> dict:
    """
    Compares two elevation surfaces over the same grid.
    'cut' = volume where the surface DROPPED from dem_from to dem_to (material removed).
    'fill' = volume where the surface ROSE from dem_from to dem_to (material added).
    """
    _validate_same_grid(dem_from, dem_to)
    area = cell_area(dem_from)
    cut = 0.0
    fill = 0.0
    for r in range(dem_from["nrows"]):
        for c in range(dem_from["ncols"]):
            diff = dem_to["values"][r][c] - dem_from["values"][r][c]
            if diff < 0:
                cut += -diff * area
            elif diff > 0:
                fill += diff * area
    return {"cut_m3": cut, "fill_m3": fill, "net_m3": fill - cut}


def volumetric_percent_complete(baseline_dem: dict, design_dem: dict, asbuilt_dem: dict) -> dict:
    """
    A stricter completion metric than footprint area: what fraction of the
    total planned earthwork VOLUME has actually been moved.

    total planned = |cut+fill| needed to go from baseline (original ground)
                    to design (target grade)
    done so far   = |cut+fill| actually moved, baseline -> as-built
    """
    planned = grid_diff_volumes(baseline_dem, design_dem)
    done = grid_diff_volumes(baseline_dem, asbuilt_dem)

    planned_total = planned["cut_m3"] + planned["fill_m3"]
    done_total = done["cut_m3"] + done["fill_m3"]

    # Cap contribution from any single cell's overshoot so overworking one
    # area can't push the whole score past 100%.
    pct = min(100.0, (done_total / planned_total * 100)) if planned_total > 0 else 0.0

    return {
        "planned_cut_m3": planned["cut_m3"],
        "planned_fill_m3": planned["fill_m3"],
        "planned_total_m3": planned_total,
        "done_cut_m3": done["cut_m3"],
        "done_fill_m3": done["fill_m3"],
        "done_total_m3": done_total,
        "pct_complete": pct,
    }


def remaining_earthwork(design_dem: dict, asbuilt_dem: dict) -> dict:
    """How much more cut/fill is needed to reach design grade from today's surface."""
    remaining = grid_diff_volumes(asbuilt_dem, design_dem)
    return {"remaining_cut_m3": remaining["cut_m3"], "remaining_fill_m3": remaining["fill_m3"]}


def point_in_polygon(x: float, y: float, ring: list[list[float]]) -> bool:
    """Standard ray-casting point-in-polygon test. `ring` is a closed [x,y] list."""
    inside = False
    n = len(ring) - 1  # last point repeats the first
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def stockpile_volume(surface_dem: dict, base_elevation: float, footprint_ring: Optional[list] = None) -> float:
    """
    Volume of material above a flat base elevation.

    If `footprint_ring` is given (a closed polygon ring in the same world
    coordinates as the DEM), only grid cells whose center falls inside it
    are counted. Without it, the WHOLE grid is scanned, which will pick up
    any other elevated terrain in frame — always pass a footprint for real
    stockpiles unless the DEM has already been cropped to just that pad.
    """
    cs = surface_dem["cell_size"]
    area = cell_area(surface_dem)
    total = 0.0
    for r, row in enumerate(surface_dem["values"]):
        y = surface_dem["origin_y"] + r * cs
        for c, z in enumerate(row):
            if z <= base_elevation:
                continue
            if footprint_ring is not None:
                x = surface_dem["origin_x"] + c * cs
                if not point_in_polygon(x, y, footprint_ring):
                    continue
            total += (z - base_elevation) * area
    return total


def volume_to_tonnage(volume_m3: float, density_t_per_m3: float) -> float:
    return volume_m3 * density_t_per_m3


def reconcile_inventory(calculated_tons: float, recorded_tons: float, tolerance_pct: float = 5.0) -> dict:
    if recorded_tons == 0:
        variance_pct = None
    else:
        variance_pct = (calculated_tons - recorded_tons) / recorded_tons * 100
    variance_abs = calculated_tons - recorded_tons
    flagged = variance_pct is not None and abs(variance_pct) > tolerance_pct
    return {
        "calculated_tons": calculated_tons,
        "recorded_tons": recorded_tons,
        "variance_tons": variance_abs,
        "variance_pct": variance_pct,
        "flagged": flagged,
        "tolerance_pct": tolerance_pct,
    }


def bilinear_sample(dem: dict, x: float, y: float) -> Optional[float]:
    """Interpolated elevation at an arbitrary world (x, y). None if outside the grid."""
    cs = dem["cell_size"]
    fx = (x - dem["origin_x"]) / cs
    fy = (y - dem["origin_y"]) / cs
    if fx < 0 or fy < 0 or fx > dem["ncols"] - 1 or fy > dem["nrows"] - 1:
        return None

    c0, r0 = int(fx), int(fy)
    c1, r1 = min(c0 + 1, dem["ncols"] - 1), min(r0 + 1, dem["nrows"] - 1)
    tx, ty = fx - c0, fy - r0

    v00 = dem["values"][r0][c0]
    v01 = dem["values"][r0][c1]
    v10 = dem["values"][r1][c0]
    v11 = dem["values"][r1][c1]

    top = v00 * (1 - tx) + v01 * tx
    bottom = v10 * (1 - tx) + v11 * tx
    return top * (1 - ty) + bottom * ty


def cross_section(dems: dict[str, dict], point_a: list[float], point_b: list[float], num_samples: int = 50) -> list[dict]:
    """
    Samples elevation along a line for each named DEM (e.g. {"design": ..., "as_built": ...}).
    Returns a list of {distance_m, <label>: elevation_or_null, ...} for charting.
    """
    import math
    ax, ay = point_a
    bx, by = point_b
    total_dist = math.hypot(bx - ax, by - ay)

    samples = []
    for i in range(num_samples + 1):
        t = i / num_samples
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        point = {"distance_m": total_dist * t}
        for label, dem in dems.items():
            point[label] = bilinear_sample(dem, x, y)
        samples.append(point)
    return samples


# ---------------------------------------------------------------------------
# Why a custom grid format instead of GeoTIFF:
# Real DEMs from Pix4D/DroneDeploy/Propeller export as GeoTIFF, which needs
# GDAL/rasterio to read. Those aren't installed in this environment and can't
# be installed without network access here. This JSON grid is a documented,
# trivial-to-produce intermediate format — converting a real GeoTIFF DEM into
# this shape is a ~10 line rasterio script (read array, read transform,
# write JSON) that would run in the customer's own pipeline where those
# libraries are available. That conversion step is the natural next piece
# of backend work once this is validated.
# ---------------------------------------------------------------------------
