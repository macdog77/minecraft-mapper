"""Per-tile feature masks (water, buildings) with halo-aware stitching."""

import numpy as np

from .tiff import (
    TIFF_BUILDING_THRESHOLD,
    TIFF_PX_PER_M,
    TIFF_WATER_THRESHOLD,
    iter_neighbour_tiffs,
    tile_building_fraction,
    tile_water_fraction,
)
from .tiles import CELL_SIZE_M


# Feature-detection tuning
FLAT_WINDOW             = 5      # cells; 5 x 50 m = 250 m flat-area detector
FLAT_RANGE_M            = 0.05   # cells in window must vary less than this to be "flat"
WATER_CELL_FLOOR        = 0.85   # minimum post-blur density for any True water cell, so
                                 # isolated small ponds survive bilinear sampling at
                                 # scale > 1. A 1-cell pond blurs to 0.25 and a 2-cell
                                 # to 0.375 — both far below WATER_MASK_THRESHOLD, so
                                 # they vanish entirely at scale 4/8 without this floor.
                                 # 0.85 is the minimum that renders the full "plus"
                                 # pattern (12 of 16 blocks) for a 1-cell pond at
                                 # scale 4 — corners are geometrically unreachable.
                                 # Bloat on large lochs is <2% (edge True cells with
                                 # natural density ≥ 0.85 are unchanged).

# Building-detection tuning
BUILDING_SUBCELL_M = 5   # m; building mask resolution — 10x finer than CELL_SIZE_M so
                         # buildings render close to street level instead of occupying
                         # whole 50 m OS cells.
MIN_BUILDING_SCALE = 8   # cells below this resolve buildings as <1 block — skip


def flat_area_mask(grid, window=FLAT_WINDOW, range_threshold=FLAT_RANGE_M):
    """Cells whose local window×window elevation range is below range_threshold."""
    rows, cols = grid.shape
    if rows < window or cols < window:
        return np.zeros(grid.shape, dtype=bool)
    from numpy.lib.stride_tricks import sliding_window_view
    pad = window // 2
    padded = np.pad(grid, pad, mode="edge")
    win = sliding_window_view(padded, (window, window))
    local_range = win.max(axis=(-1, -2)) - win.min(axis=(-1, -2))
    return local_range < range_threshold


def water_mask_for_tile(tile_grid, core_row0, core_col0, nrows, ncols, halo,
                        headers, tile_index, tiles_dir,
                        tiff_threshold=TIFF_WATER_THRESHOLD,
                        flat_window=FLAT_WINDOW,
                        flat_range=FLAT_RANGE_M):
    """Build a boolean water mask aligned to tile_grid (core + halo).

    A local cell is water if:
      * the matching 50 m block of an OS raster TIFF is mostly blue; or
      * its 5×5 elevation window is flat (inland lochs/reservoirs).

    TIFF scan covers this tile plus any of its 8 neighbours whose quadrants
    reach into the halo band — prevents seams when water hugs the tile edge.
    """
    mask = np.zeros(tile_grid.shape, dtype=bool)
    mask_row0 = core_row0 - halo
    mask_col0 = core_col0 - halo

    for frac, g_r0, g_c0 in iter_neighbour_tiffs(
            headers, tile_index, core_row0, core_col0, nrows, ncols,
            tiles_dir, CELL_SIZE_M * TIFF_PX_PER_M, tile_water_fraction):
        qh, qw = frac.shape
        l_r0 = max(0, g_r0 - mask_row0)
        l_r1 = min(mask.shape[0], g_r0 + qh - mask_row0)
        l_c0 = max(0, g_c0 - mask_col0)
        l_c1 = min(mask.shape[1], g_c0 + qw - mask_col0)
        if l_r1 <= l_r0 or l_c1 <= l_c0:
            continue
        f_r0 = l_r0 - (g_r0 - mask_row0)
        f_c0 = l_c0 - (g_c0 - mask_col0)
        mask[l_r0:l_r1, l_c0:l_c1] |= frac[f_r0:f_r0 + (l_r1 - l_r0),
                                           f_c0:f_c0 + (l_c1 - l_c0)] > tiff_threshold

    # Flat-area detection over the halo-inclusive grid so window results near
    # the core boundary still have neighbour data available.
    mask |= flat_area_mask(tile_grid, flat_window, flat_range)
    return mask


def building_mask_for_tile(core_row0, core_col0, nrows, ncols, halo,
                           headers, tile_index, tiles_dir,
                           tiff_threshold=TIFF_BUILDING_THRESHOLD):
    """Build a boolean building mask at BUILDING_SUBCELL_M resolution.

    Shape: ((nrows + 2H) * K, (ncols + 2H) * K) where
    K = CELL_SIZE_M // BUILDING_SUBCELL_M = 10.

    Halo coverage matters because owned chunks on the east/south edge can
    contain blocks whose sub-cell footprint falls a few cells past the tile
    boundary (especially at high --scale).
    """
    K = CELL_SIZE_M // BUILDING_SUBCELL_M
    mask_rows = (nrows + 2 * halo) * K
    mask_cols = (ncols + 2 * halo) * K
    mask = np.zeros((mask_rows, mask_cols), dtype=bool)
    import os
    if not (tiles_dir and os.path.isdir(tiles_dir)):
        return mask

    mask_row0_sub = (core_row0 - halo) * K
    mask_col0_sub = (core_col0 - halo) * K
    subcell_px = BUILDING_SUBCELL_M * TIFF_PX_PER_M

    for frac, g_r0, g_c0 in iter_neighbour_tiffs(
            headers, tile_index, core_row0, core_col0, nrows, ncols,
            tiles_dir, subcell_px, tile_building_fraction):
        qh, qw = frac.shape
        g_r0_sub = g_r0 * K
        g_c0_sub = g_c0 * K
        l_r0 = max(0, g_r0_sub - mask_row0_sub)
        l_r1 = min(mask_rows, g_r0_sub + qh - mask_row0_sub)
        l_c0 = max(0, g_c0_sub - mask_col0_sub)
        l_c1 = min(mask_cols, g_c0_sub + qw - mask_col0_sub)
        if l_r1 <= l_r0 or l_c1 <= l_c0:
            continue
        f_r0 = l_r0 - (g_r0_sub - mask_row0_sub)
        f_c0 = l_c0 - (g_c0_sub - mask_col0_sub)
        mask[l_r0:l_r1, l_c0:l_c1] |= frac[f_r0:f_r0 + (l_r1 - l_r0),
                                           f_c0:f_c0 + (l_c1 - l_c0)] > tiff_threshold
    return mask


def compute_water_density(water_mask, river_mask=None):
    """3×3 gaussian-ish blur with a per-cell floor so lone ponds survive downsampling.

    Used to soften water mask edges for bilinear chunk sampling at scale > 1.
    River cells (if provided) are pinned to density 1.0 so single-cell lines
    don't dissolve under the threshold.
    """
    m = water_mask.astype(np.float32)
    p = np.pad(m, 1, mode="edge")
    density = (
        1 * p[:-2, :-2] + 2 * p[:-2, 1:-1] + 1 * p[:-2, 2:]
        + 2 * p[1:-1, :-2] + 4 * p[1:-1, 1:-1] + 2 * p[1:-1, 2:]
        + 1 * p[2:, :-2] + 2 * p[2:, 1:-1] + 1 * p[2:, 2:]
    ) / 16.0
    density = np.maximum(density, m * WATER_CELL_FLOOR)
    if river_mask is not None:
        density[river_mask] = 1.0
    return density
