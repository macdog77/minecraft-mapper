"""Global grid layout, streaming tile loading, and halo stitching.

These helpers let downstream scripts process one tile at a time without
materialising the full UK-wide grid in memory. They also define the single
place where the OS Terrain 50 cell size lives.
"""

import os
from functools import lru_cache

import numpy as np
from tqdm import tqdm

from .asc import read_asc_from_zip


CELL_SIZE_M = 50  # OS Terrain 50 cell size in metres


def scan_headers(zip_entries):
    """Read just the header of every tile (fast — no elevation rows).

    Returns {(e_digit, n_digit, region): (header, zip_path)}. Tiles that fail
    to parse are skipped with a warning; subsequent passes iterate this dict
    rather than zip_entries to avoid re-scanning failures.
    """
    headers = {}
    print(f"Reading {len(zip_entries)} tile headers...")
    for zp, e_digit, n_digit, region in tqdm(zip_entries, unit="tile", leave=False):
        try:
            hdr, _ = read_asc_from_zip(zp)
            headers[(e_digit, n_digit, region)] = (hdr, zp)
        except Exception as ex:
            print(f"  Warning: skipping {os.path.basename(zp)}: {ex}")
    if not headers:
        raise RuntimeError("No tiles could be read.")
    return headers


def compute_global_extent(headers):
    """From the header dict, compute the bounding box and total grid size.

    Returns (min_east, max_north, total_rows, total_cols). Note that
    `max_north` is the **top** (north) edge, not the SW corner.
    """
    min_east  = min(h["xllcorner"] for h, _ in headers.values())
    min_north = min(h["yllcorner"] for h, _ in headers.values())
    max_north = max(h["yllcorner"] + h["nrows"] * CELL_SIZE_M for h, _ in headers.values())
    max_east  = max(h["xllcorner"] + h["ncols"] * CELL_SIZE_M for h, _ in headers.values())
    total_cols = round((max_east  - min_east)  / CELL_SIZE_M)
    total_rows = round((max_north - min_north) / CELL_SIZE_M)
    return min_east, max_north, total_rows, total_cols


def tile_global_offset(hdr, min_east, max_north):
    """Return (row_offset, col_offset) of a tile's NW cell in the global grid."""
    nrows = int(hdr["nrows"])
    col = round((hdr["xllcorner"] - min_east) / CELL_SIZE_M)
    row = round((max_north - (hdr["yllcorner"] + nrows * CELL_SIZE_M)) / CELL_SIZE_M)
    return row, col


def build_tile_index(headers, min_east, max_north):
    """Return {(row_offset, col_offset): key} mapping each tile's global NW cell to its key."""
    index = {}
    for key, (hdr, _zp) in headers.items():
        row, col = tile_global_offset(hdr, min_east, max_north)
        index[(row, col)] = key
    return index


@lru_cache(maxsize=16)
def load_tile_elev(zp):
    """Load a tile's elevation into a float32 2D array with nodata → 0.0.

    LRU-cached so halo reads from a neighbour don't re-parse its zip when the
    neighbour is later processed as a core tile.

    Returns (nrows, ncols, arr). Header metadata is available via scan_headers.
    """
    hdr, rows = read_asc_from_zip(zp)
    arr = np.array(rows, dtype=np.float32)
    nodata = hdr.get("nodata_value", -9999)
    arr[arr == nodata] = np.nan
    arr = np.where(np.isnan(arr), 0.0, arr).astype(np.float32)
    return int(hdr["nrows"]), int(hdr["ncols"]), arr


def load_tile_with_halo(key, headers, tile_index, min_east, max_north, halo):
    """Load a tile's elevation grid padded with `halo` cells on every side.

    Off-coverage halo cells are left at 0.0. Returns
    (tile_grid, core_row0, core_col0, nrows, ncols) where (core_row0, core_col0)
    is the global NW cell of the core (non-halo) area. The local index of the
    core's NW cell inside tile_grid is (halo, halo).
    """
    hdr, zp = headers[key]
    nrows, ncols, core = load_tile_elev(zp)
    core_row0, core_col0 = tile_global_offset(hdr, min_east, max_north)

    H = halo
    full = np.zeros((nrows + 2 * H, ncols + 2 * H), dtype=np.float32)
    full[H:H + nrows, H:H + ncols] = core

    # Walk candidate neighbours. Tile sizes vary (edge tiles of a region can
    # be smaller), so probe the 8 compass positions using this tile's own size
    # as the step — good enough because the OS dataset is uniform 200x200 and
    # any off-by-one falls into missing-tile territory anyway.
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            nkey = tile_index.get((core_row0 + dr * nrows, core_col0 + dc * ncols))
            if nkey is None:
                continue
            n_hdr, n_zp = headers[nkey]
            n_nrows, n_ncols, n_arr = load_tile_elev(n_zp)
            n_row0, n_col0 = tile_global_offset(n_hdr, min_east, max_north)

            # Global overlap between halo rectangle and neighbour tile.
            g_r0 = max(core_row0 - H, n_row0)
            g_r1 = min(core_row0 + nrows + H, n_row0 + n_nrows)
            g_c0 = max(core_col0 - H, n_col0)
            g_c1 = min(core_col0 + ncols + H, n_col0 + n_ncols)
            if g_r1 <= g_r0 or g_c1 <= g_c0:
                continue
            full[g_r0 - (core_row0 - H):g_r1 - (core_row0 - H),
                 g_c0 - (core_col0 - H):g_c1 - (core_col0 - H)] = \
                n_arr[g_r0 - n_row0:g_r1 - n_row0, g_c0 - n_col0:g_c1 - n_col0]

    return full, core_row0, core_col0, nrows, ncols


def chunks_owned_by_tile(core_row0, core_col0, nrows, ncols, scale):
    """Yield (cx, cz) chunk coordinates whose SW block corner falls inside this tile's core.

    Ownership rule guarantees each chunk is emitted by exactly one tile with
    no gaps: the SW-corner block (cx*16, cz*16) sits in
    [core_col0*scale, (core_col0+ncols)*scale) on the X axis and the
    corresponding range on Z.
    """
    x_lo = core_col0 * scale
    x_hi = (core_col0 + ncols) * scale
    z_lo = core_row0 * scale
    z_hi = (core_row0 + nrows) * scale

    cx_min = (x_lo + 15) // 16
    cx_max = (x_hi + 15) // 16
    cz_min = (z_lo + 15) // 16
    cz_max = (z_hi + 15) // 16

    for cz in range(cz_min, cz_max):
        for cx in range(cx_min, cx_max):
            yield cx, cz


def resolve_spawn_elev(spawn_row, spawn_col, headers, tile_index, min_east, max_north):
    """Return the elevation at a global (row, col) cell by loading just its owning tile.

    Avoids loading the full stitched grid for spawn lookup. Returns 0.0 if the
    cell is off-coverage.
    """
    for (row0, col0), key in tile_index.items():
        hdr, zp = headers[key]
        nrows = int(hdr["nrows"])
        ncols = int(hdr["ncols"])
        if row0 <= spawn_row < row0 + nrows and col0 <= spawn_col < col0 + ncols:
            _, _, arr = load_tile_elev(zp)
            return float(arr[spawn_row - row0, spawn_col - col0])
    return 0.0
