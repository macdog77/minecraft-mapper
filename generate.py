#!/usr/bin/env python3
"""
generate.py — Generate a Minecraft Java Edition 1.21 world from OS Terrain 50 data.

Usage:
    python generate.py <input> [options]

    input   Single tile .zip, a region folder (e.g. "OS Map Data/data/nn"),
            or the top-level data folder (generates the whole UK).

Options:
    --scale N       Blocks per OS cell (default 1 = 1 block per 50 m cell).
                    N=2 → 1 block = 25 m,  N=4 → 1 block = 12.5 m.
    --vscale F      Vertical multiplier on elevation metres (default 0.10).
                    Y = 64 + round(elevation_m * F).
                    Suggested values:  0.10 (subtle), 0.15 (moderate), 0.20 (dramatic).
    --biomes MODE   'default' = plains everywhere, 'elevation' = biome by height (default).
    --spawn LAT,LON Spawn at a WGS84 geo coordinate (e.g. '56.97,-3.40').
                    Falls back to map centre if the point is outside the map.
    --void          Keep the void boundary beyond the pre-filled OS area.
                    Triggers Minecraft's 'Experimental Settings' warning.
    --buildings     Detect OS light-orange building fill from the raster tiles
                    and place stacks of bricks on those cells. Auto-off below
                    --scale 8 (each cell resolves to <1 block).
    --out PATH      Output world folder (default ./worlds/<NAME>).
"""

import argparse
import glob
import os
import re
import shutil
import sys
import zipfile
from functools import lru_cache
from math import ceil

import amulet
import amulet_nbt
import numpy as np
from PIL import Image
from amulet.api.chunk import Chunk
from amulet.api.block import Block
from amulet.level.formats.anvil_world import AnvilFormat
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
from heightmap import parse_asc
from locate import wgs84_to_bng

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MC_VERSION      = (1, 21, 0)
MC_VERSION_ID   = "java"
DIMENSION       = "minecraft:overworld"
MAP_ZERO_Y      = 64          # Minecraft Y that corresponds to 0 m OS elevation
SEA_SURFACE_Y   = 63          # highest water block (one below MAP_ZERO_Y)
Y_MIN           = -64         # Minecraft bedrock level
Y_MAX           = 319         # Minecraft build limit
CELL_SIZE_M     = 50          # OS Terrain 50 cell size in metres

# Water detection tuning
TIFF_PX_PER_M        = 1       # OS tiles are 1 m/pixel, so 50 px per 50 m cell
TIFF_WATER_THRESHOLD = 0.5     # fraction of blue pixels per cell to mark as water
FLAT_WINDOW          = 5       # cells; 5 x 50 m = 250 m flat-area detector
FLAT_RANGE_M         = 0.05    # cells in window must vary less than this to be "flat"
WATER_MASK_THRESHOLD = 0.50    # threshold for bilinear-sampled water density (scale > 1)
WATER_CELL_FLOOR     = 0.85    # minimum post-blur density for any True water cell, so
                               # isolated small ponds survive bilinear sampling at
                               # scale > 1. A 1-cell pond blurs to 0.25 and a 2-cell
                               # to 0.375 — both far below WATER_MASK_THRESHOLD, so
                               # they vanish entirely at scale 4/8 without this floor.
                               # 0.85 is the minimum that renders the full "plus"
                               # pattern (12 of 16 blocks) for a 1-cell pond at
                               # scale 4 — corners are geometrically unreachable.
                               # Bloat on large lochs is <2% (edge True cells with
                               # natural density ≥ 0.85 are unchanged).

# Building detection tuning (requires --buildings; auto-off below MIN_BUILDING_SCALE)
TIFF_BUILDING_THRESHOLD = 0.30  # fraction of peach pixels per sub-cell to mark as building
BUILDING_SUBCELL_M      = 5     # m; building mask resolution — 10x finer than CELL_SIZE_M
                                #     so buildings render close to street level instead of
                                #     occupying whole 50 m OS cells.
BUILDING_HEIGHT_BLOCKS  = 3     # stack of 'bricks' blocks placed above the surface
MIN_BUILDING_SCALE      = 8     # cells below this resolve buildings as <1 block — skip

# Block palette — elevation thresholds (metres) for surface/sub-surface selection
THRESHOLDS = [
    # (min_elev, surface_name, surface_props, subsurface_name)
    (1100, "snow_block",   {},                          "stone"),
    ( 600, "stone",        {},                          "stone"),
    (  10, "grass_block",  {"snowy": "false"},          "dirt"),
    (   0, "sand",         {},                          "sandstone"),
    (None, "gravel",       {},                          "stone"),   # sea floor (elev <= 0)
]

# Biome mapping by elevation (used when --biomes elevation)
BIOME_THRESHOLDS = [
    (1100, "minecraft:frozen_peaks"),
    ( 600, "minecraft:stony_peaks"),
    ( 300, "minecraft:windswept_hills"),
    (   0, "minecraft:plains"),
    (None, "minecraft:ocean"),
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_zip(path):
    """Return (header, rows) from the .asc inside a zip."""
    with zipfile.ZipFile(path) as zf:
        asc_names = [n for n in zf.namelist()
                     if n.lower().endswith(".asc") and not n.lower().endswith(".aux.xml")]
        if not asc_names:
            raise ValueError(f"No .asc found in {path}")
        with zf.open(asc_names[0]) as f:
            return parse_asc(f)


def _load_asc(path):
    with open(path, "r") as f:
        return parse_asc(f)


def discover_zips(input_path):
    """
    Return a list of (zip_path, e_digit, n_digit, region_code) tuples.
    Accepts a single zip, a region folder, or a parent folder of region folders.
    """
    # Strip a trailing quote: on Windows bash, `"OS Map Data\data\nn\"` escapes
    # the closing quote and leaves a literal `"` on the end of the argument.
    input_path = input_path.rstrip('"').rstrip("'")
    input_path = os.path.abspath(input_path)

    if not os.path.exists(input_path):
        raise ValueError(f"Input path does not exist: {input_path}")

    if input_path.lower().endswith(".zip"):
        name = os.path.basename(input_path).lower()
        m = re.match(r"([a-z]{2})(\d)(\d)_", name)
        if not m:
            raise ValueError(f"Cannot parse tile code from {name}")
        return [(input_path, int(m.group(2)), int(m.group(3)), m.group(1))]

    # Collect all zips recursively
    results = []
    for zp in glob.glob(os.path.join(input_path, "**", "*.zip"), recursive=True):
        name = os.path.basename(zp).lower()
        m = re.match(r"([a-z]{2})(\d)(\d)_", name)
        if m:
            results.append((zp, int(m.group(2)), int(m.group(3)), m.group(1)))
    if not results:
        raise ValueError(f"No OS Terrain 50 zip tiles found under {input_path}")
    return results


def scan_headers(zip_entries):
    """
    Read just the header of every tile (fast — no elevation rows).
    Returns a dict {(e_digit, n_digit, region): (header, zip_path)}.

    Tiles that fail to parse are skipped with a warning. Subsequent passes
    iterate this dict rather than zip_entries to avoid re-scanning failures.
    """
    headers = {}
    print(f"Reading {len(zip_entries)} tile headers...")
    for zp, e_digit, n_digit, region in tqdm(zip_entries, unit="tile", leave=False):
        try:
            hdr, _ = _load_zip(zp)
            headers[(e_digit, n_digit, region)] = (hdr, zp)
        except Exception as ex:
            print(f"  Warning: skipping {os.path.basename(zp)}: {ex}")
    if not headers:
        raise RuntimeError("No tiles could be read.")
    return headers


def compute_global_extent(headers):
    """
    From the header dict, compute the bounding box and total grid size.
    Returns (min_east, max_north, total_rows, total_cols).
    """
    min_east  = min(h["xllcorner"] for h, _ in headers.values())
    min_north = min(h["yllcorner"] for h, _ in headers.values())
    max_north = max(h["yllcorner"] + h["nrows"] * CELL_SIZE_M for h, _ in headers.values())
    max_east  = max(h["xllcorner"] + h["ncols"] * CELL_SIZE_M for h, _ in headers.values())
    total_cols = round((max_east  - min_east)  / CELL_SIZE_M)
    total_rows = round((max_north - min_north) / CELL_SIZE_M)
    return min_east, max_north, total_rows, total_cols


def _tile_global_offset(hdr, min_east, max_north):
    """Return (row_offset, col_offset) of a tile's NW cell in the global grid."""
    nrows = int(hdr["nrows"])
    col = round((hdr["xllcorner"] - min_east) / CELL_SIZE_M)
    row = round((max_north - (hdr["yllcorner"] + nrows * CELL_SIZE_M)) / CELL_SIZE_M)
    return row, col


def build_tile_index(headers, min_east, max_north):
    """
    Return a dict {(row_offset, col_offset): key} mapping a tile's global NW
    cell to its header key. Used for O(1) neighbour lookup when loading halos.
    """
    index = {}
    for key, (hdr, _zp) in headers.items():
        row, col = _tile_global_offset(hdr, min_east, max_north)
        index[(row, col)] = key
    return index


@lru_cache(maxsize=16)
def _load_tile_elev(zp):
    """
    Load a tile's elevation into a float32 2D array with nodata → 0.0.
    LRU-cached so halo reads from a neighbour don't re-parse its zip when
    the neighbour is later processed as a core tile.

    Returns (nrows, ncols, arr). Header metadata is available via scan_headers.
    """
    hdr, rows = _load_zip(zp)
    arr = np.array(rows, dtype=np.float32)
    nodata = hdr.get("nodata_value", -9999)
    arr[arr == nodata] = np.nan
    arr = np.where(np.isnan(arr), 0.0, arr).astype(np.float32)
    return int(hdr["nrows"]), int(hdr["ncols"]), arr


def load_tile_with_halo(key, headers, tile_index, min_east, max_north, halo):
    """
    Load a tile's elevation grid padded with `halo` cells on every side from
    neighbouring tiles. Off-coverage halo cells are left at 0.0.

    Returns (tile_grid, core_row0, core_col0, nrows, ncols) where (core_row0,
    core_col0) is the global NW cell of the core (non-halo) area. The local
    index of the core's NW cell inside tile_grid is (halo, halo).
    """
    hdr, zp = headers[key]
    nrows, ncols, core = _load_tile_elev(zp)
    core_row0, core_col0 = _tile_global_offset(hdr, min_east, max_north)

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
            n_nrows, n_ncols, n_arr = _load_tile_elev(n_zp)
            n_row0, n_col0 = _tile_global_offset(n_hdr, min_east, max_north)

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
    """
    Yield (cx, cz) chunk coordinates whose SW block corner falls inside this
    tile's core area. Ownership rule guarantees each chunk is emitted by
    exactly one tile with no gaps: the SW-corner block (cx*16, cz*16) sits in
    [core_col0*scale, (core_col0+ncols)*scale) on the X axis and the
    corresponding range on Z.
    """
    x_lo = core_col0 * scale
    x_hi = (core_col0 + ncols) * scale
    z_lo = core_row0 * scale
    z_hi = (core_row0 + nrows) * scale

    cx_min = (x_lo + 15) // 16   # ceil(x_lo / 16)
    cx_max = (x_hi + 15) // 16   # ceil(x_hi / 16) — exclusive
    cz_min = (z_lo + 15) // 16
    cz_max = (z_hi + 15) // 16

    for cz in range(cz_min, cz_max):
        for cx in range(cx_min, cx_max):
            yield cx, cz


def resolve_spawn_elev(spawn_row, spawn_col, headers, tile_index, min_east, max_north):
    """
    Return the elevation at a global (row, col) cell by loading just the tile
    that contains it. Avoids loading the full stitched grid for spawn lookup.
    Returns 0.0 if the cell is off-coverage.
    """
    # Probe the tile_index: a tile owns cells in [row0, row0 + nrows) x
    # [col0, col0 + ncols). Since we don't know nrows/ncols a priori, iterate
    # — the dict is small (one entry per tile).
    for (row0, col0), key in tile_index.items():
        hdr, zp = headers[key]
        nrows = int(hdr["nrows"])
        ncols = int(hdr["ncols"])
        if row0 <= spawn_row < row0 + nrows and col0 <= spawn_col < col0 + ncols:
            _, _, arr = _load_tile_elev(zp)
            return float(arr[spawn_row - row0, spawn_col - col0])
    return 0.0


# ---------------------------------------------------------------------------
# Water detection
# ---------------------------------------------------------------------------

def _is_water_color(r, g, b):
    """True if an OS raster palette entry is a water colour (pale cyan..deeper blue)."""
    return (r < g - 10) and (r < b - 10) and (g + b) > 300


def _tile_water_fraction(tif_path, cell_size_px):
    """Read a palette TIFF quadrant and return fraction-of-water per cell grid."""
    return _tile_palette_fraction(tif_path, cell_size_px, _is_water_color)


def _is_building_color(r, g, b):
    """True if an OS raster palette entry is the light-peach building fill.

    Calibrated against OS Explorer raster tiles: the building fill is
    #f8d8b8 — RGB (248, 216, 184). Tight tolerances are enough because the
    palette maps exactly to that entry on every tile.
    """
    return (abs(r - 248) <= 8
            and abs(g - 216) <= 8
            and abs(b - 184) <= 8
            and r > g > b)


def _tile_building_fraction(tif_path, cell_size_px):
    """Read a palette TIFF quadrant and return fraction-of-building per cell grid."""
    return _tile_palette_fraction(tif_path, cell_size_px, _is_building_color)


def _tile_palette_fraction(tif_path, cell_size_px, predicate):
    """Shared helper: fraction of palette-matching pixels per cell_size_px block."""
    im = Image.open(tif_path)
    pal = im.getpalette() or []
    arr = np.asarray(im, dtype=np.uint8)
    lut = np.zeros(256, dtype=bool)
    for idx in range(min(256, len(pal) // 3)):
        if predicate(pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]):
            lut[idx] = True
    mask = lut[arr]
    h, w = mask.shape
    ch, cw = h // cell_size_px, w // cell_size_px
    trimmed = mask[:ch * cell_size_px, :cw * cell_size_px]
    return trimmed.reshape(ch, cell_size_px, cw, cell_size_px).mean(axis=(1, 3))


def _flat_area_mask(grid, window, range_threshold):
    """Cells whose local `window x window` elevation range is below threshold."""
    rows, cols = grid.shape
    if rows < window or cols < window:
        return np.zeros(grid.shape, dtype=bool)
    from numpy.lib.stride_tricks import sliding_window_view
    pad = window // 2
    padded = np.pad(grid, pad, mode="edge")
    win = sliding_window_view(padded, (window, window))
    local_range = win.max(axis=(-1, -2)) - win.min(axis=(-1, -2))
    return local_range < range_threshold


def _iter_neighbour_tiffs(headers, tile_index, core_row0, core_col0, nrows, ncols,
                          tiles_dir, cell_size_px, predicate_fraction_fn):
    """
    Yield (frac_array, global_row_offset, global_col_offset) for every TIFF
    quadrant belonging to this tile and its 8 neighbours. `predicate_fraction_fn`
    is one of _tile_water_fraction / _tile_building_fraction.

    Used by water_mask_for_tile and building_mask_for_tile so both share the
    neighbour-walking + quadrant-locating boilerplate.
    """
    if not (tiles_dir and os.path.isdir(tiles_dir)):
        return
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            nkey = tile_index.get((core_row0 + dr * nrows, core_col0 + dc * ncols))
            if nkey is None:
                continue
            e_digit, n_digit, region = nkey
            n_hdr, _zp = headers[nkey]
            n_nrows = int(n_hdr["nrows"])
            n_ncols = int(n_hdr["ncols"])
            n_row0 = core_row0 + dr * nrows
            n_col0 = core_col0 + dc * ncols

            tile_code = f"{region.upper()}{e_digit}{n_digit}"
            region_up = region.upper()
            half_rows, half_cols = n_nrows // 2, n_ncols // 2
            quadrants = [
                ("NW", 0,         0),
                ("NE", 0,         half_cols),
                ("SW", half_rows, 0),
                ("SE", half_rows, half_cols),
            ]
            for quad, q_row, q_col in quadrants:
                tif_path = os.path.join(tiles_dir, region_up, f"{tile_code}{quad}.tif")
                if not os.path.isfile(tif_path):
                    continue
                try:
                    frac = predicate_fraction_fn(tif_path, cell_size_px=cell_size_px)
                except Exception as ex:
                    tqdm.write(f"  Warning: {os.path.basename(tif_path)}: {ex}")
                    continue
                yield frac, n_row0 + q_row, n_col0 + q_col


def water_mask_for_tile(tile_grid, core_row0, core_col0, nrows, ncols, halo,
                        headers, tile_index, tiles_dir,
                        tiff_threshold=TIFF_WATER_THRESHOLD,
                        flat_window=FLAT_WINDOW,
                        flat_range=FLAT_RANGE_M):
    """
    Build a boolean water mask aligned to tile_grid (core + halo). A local
    cell is water if:
      * the matching 50 m block of an OS raster TIFF is mostly blue; or
      * its 5x5 elevation window is flat (inland lochs/reservoirs).

    TIFF scan covers this tile plus any of its 8 neighbours whose quadrants
    reach into the halo band — prevents seams when water hugs the tile edge.
    """
    mask = np.zeros(tile_grid.shape, dtype=bool)
    mask_row0 = core_row0 - halo   # global row of mask[0, 0]
    mask_col0 = core_col0 - halo

    for frac, g_r0, g_c0 in _iter_neighbour_tiffs(
            headers, tile_index, core_row0, core_col0, nrows, ncols,
            tiles_dir, CELL_SIZE_M * TIFF_PX_PER_M, _tile_water_fraction):
        qh, qw = frac.shape
        # Intersect [g_r0, g_r0+qh) x [g_c0, g_c0+qw) with the local mask.
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
    mask |= _flat_area_mask(tile_grid, flat_window, flat_range)
    return mask


def building_mask_for_tile(core_row0, core_col0, nrows, ncols, halo,
                           headers, tile_index, tiles_dir,
                           tiff_threshold=TIFF_BUILDING_THRESHOLD):
    """
    Build a boolean building mask at BUILDING_SUBCELL_M resolution, covering
    the tile's core + halo. Shape: ((nrows + 2H) * K, (ncols + 2H) * K) where
    K = CELL_SIZE_M // BUILDING_SUBCELL_M = 10.

    Halo coverage matters here because owned chunks on the east/south edge
    can contain blocks whose sub-cell footprint falls a few cells past the
    tile boundary (especially at high --scale).
    """
    K = CELL_SIZE_M // BUILDING_SUBCELL_M
    mask_rows = (nrows + 2 * halo) * K
    mask_cols = (ncols + 2 * halo) * K
    mask = np.zeros((mask_rows, mask_cols), dtype=bool)
    if not (tiles_dir and os.path.isdir(tiles_dir)):
        return mask

    mask_row0_sub = (core_row0 - halo) * K
    mask_col0_sub = (core_col0 - halo) * K
    subcell_px = BUILDING_SUBCELL_M * TIFF_PX_PER_M

    for frac, g_r0, g_c0 in _iter_neighbour_tiffs(
            headers, tile_index, core_row0, core_col0, nrows, ncols,
            tiles_dir, subcell_px, _tile_building_fraction):
        qh, qw = frac.shape  # sub-cells
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


# ---------------------------------------------------------------------------
# Rivers (OS Open Rivers MBTiles vector data)
# ---------------------------------------------------------------------------

def _draw_line_cells(mask, c0, r0, c1, r1):
    """Rasterize a single-cell-wide line into a boolean mask (Bresenham)."""
    rows, cols = mask.shape
    dc = abs(c1 - c0)
    dr = -abs(r1 - r0)
    sc = 1 if c0 < c1 else -1
    sr = 1 if r0 < r1 else -1
    err = dc + dr
    while True:
        if 0 <= r0 < rows and 0 <= c0 < cols:
            mask[r0, c0] = True
        if c0 == c1 and r0 == r1:
            return
        e2 = 2 * err
        if e2 >= dr:
            err += dr
            c0 += sc
        if e2 <= dc:
            err += dc
            r0 += sr


def rasterize_rivers_for_tile(mask_shape, local_min_east, local_max_north, mbtiles_path,
                              forms=("canal", "inlandRiver", "tidalRiver"),
                              conn=None):
    """
    Rasterize OS Open Rivers centrelines into a boolean mask covering the
    BNG rectangle starting at (local_min_east, local_max_north) with
    mask_shape = (rows, cols). Typically called per-tile with the tile's
    halo-inclusive bbox so the mask aligns with tile_grid.

    Returns None if mapbox-vector-tile is missing, or a boolean array of
    `mask_shape` otherwise. Pass `conn` to reuse an open sqlite connection
    across tiles (caller is responsible for opening/closing).
    """
    try:
        import mapbox_vector_tile
    except ImportError:
        return None

    import gzip as _gzip
    import math as _math
    import sqlite3
    from pyproj import Transformer

    mask_rows, mask_cols = mask_shape
    local_max_east  = local_min_east + mask_cols * CELL_SIZE_M
    local_min_north = local_max_north - mask_rows * CELL_SIZE_M

    to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    to_bng   = Transformer.from_crs("EPSG:4326",  "EPSG:27700", always_xy=True)

    zoom = 14
    n_tiles = 2 ** zoom

    def lonlat_to_tile(lon, lat):
        tx = int((lon + 180) / 360 * n_tiles)
        lat_rad = _math.radians(lat)
        ty = int((1 - _math.log(_math.tan(lat_rad) + 1 / _math.cos(lat_rad)) / _math.pi) / 2 * n_tiles)
        return tx, ty

    samples = []
    for f_e in np.linspace(local_min_east, local_max_east, 5):
        for f_n in np.linspace(local_min_north, local_max_north, 5):
            lon, lat = to_wgs84.transform(f_e, f_n)
            samples.append(lonlat_to_tile(lon, lat))
    txs, tys = zip(*samples)
    tx_min, tx_max = min(txs), max(txs)
    ty_min, ty_max = min(tys), max(tys)

    tms_y_min = n_tiles - 1 - ty_max
    tms_y_max = n_tiles - 1 - ty_min

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(mbtiles_path)
        close_conn = True
    cur = conn.cursor()
    cur.execute(
        "SELECT tile_column, tile_row, tile_data FROM tiles "
        "WHERE zoom_level=? AND tile_column BETWEEN ? AND ? "
        "AND tile_row BETWEEN ? AND ?",
        (zoom, tx_min, tx_max, tms_y_min, tms_y_max),
    )
    tile_rows = cur.fetchall()
    if close_conn:
        conn.close()

    mask = np.zeros(mask_shape, dtype=bool)

    for tile_col, tile_row, raw in tile_rows:
        tx = tile_col
        ty = n_tiles - 1 - tile_row
        try:
            pbf = _gzip.decompress(raw)
        except OSError:
            pbf = raw
        try:
            decoded = mapbox_vector_tile.decode(pbf, default_options={"y_coord_down": True})
        except Exception:
            continue
        layer = decoded.get("watercourse_link")
        if not layer:
            continue
        extent = layer.get("extent", 4096)

        all_lines = []
        for feat in layer["features"]:
            form = feat.get("properties", {}).get("form")
            if form not in forms:
                continue
            geom = feat["geometry"]
            gtype = geom["type"]
            if gtype == "LineString":
                all_lines.append(geom["coordinates"])
            elif gtype == "MultiLineString":
                all_lines.extend(geom["coordinates"])
        if not all_lines:
            continue

        flat_lon, flat_lat, segments = [], [], []
        for line in all_lines:
            start = len(flat_lon)
            for px, py in line:
                lon = (tx + px / extent) / n_tiles * 360.0 - 180.0
                y_frac = (ty + py / extent) / n_tiles
                lat = _math.degrees(_math.atan(_math.sinh(_math.pi * (1 - 2 * y_frac))))
                flat_lon.append(lon)
                flat_lat.append(lat)
            segments.append((start, len(flat_lon)))

        es, ns = to_bng.transform(flat_lon, flat_lat)
        cs = np.floor((np.asarray(es) - local_min_east)  / CELL_SIZE_M).astype(np.int32)
        rs = np.floor((local_max_north - np.asarray(ns)) / CELL_SIZE_M).astype(np.int32)

        for start, end in segments:
            if end - start < 2:
                continue
            for i in range(start, end - 1):
                _draw_line_cells(mask, int(cs[i]), int(rs[i]),
                                 int(cs[i + 1]), int(rs[i + 1]))

    return mask


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------

def _make_palette(level):
    """
    Return a dict of name -> universal Block for all needed blocks.
    Also returns a helper fn: block_ids(chunk) -> dict mapping names to palette IDs.
    """
    ver = level.level_wrapper.translation_manager.get_version(MC_VERSION_ID, MC_VERSION)

    def to_uni(name, str_props=None):
        props = {k: amulet.StringTag(v) for k, v in (str_props or {}).items()}
        u, _, _ = ver.block.to_universal(Block("minecraft", name, props))
        return u

    return {
        "air":      to_uni("air"),
        "bedrock":  to_uni("bedrock"),
        "stone":    to_uni("stone"),
        "dirt":     to_uni("dirt"),
        "grass":    to_uni("grass_block", {"snowy": "false"}),
        "sand":     to_uni("sand"),
        "sandstone":to_uni("sandstone"),
        "gravel":   to_uni("gravel"),
        "snow":     to_uni("snow_block"),
        "water":    to_uni("water", {"level": "0"}),
        "bricks":   to_uni("bricks"),
    }


def _surface_and_sub(elev_m):
    """Return (surface_block_key, sub_block_key) for a given elevation."""
    for threshold, surf, _, sub in THRESHOLDS:
        if threshold is None or elev_m >= threshold:
            # Map surf name to our palette key
            surf_key = {
                "snow_block":  "snow",
                "stone":       "stone",
                "grass_block": "grass",
                "sand":        "sand",
                "gravel":      "gravel",
            }[surf]
            return surf_key, sub
    return "gravel", "stone"


def _biome_name(elev_m):
    for threshold, biome in BIOME_THRESHOLDS:
        if threshold is None or elev_m >= threshold:
            return biome
    return "minecraft:ocean"

# ---------------------------------------------------------------------------
# Chunk generation
# ---------------------------------------------------------------------------

ARRAY_OFFSET = -Y_MIN   # = 64; array index = Y + ARRAY_OFFSET
ARRAY_HEIGHT = Y_MAX - Y_MIN + 1  # = 384

def generate_chunk(cx, cz, tile_grid, tile_origin, scale, vscale, block_uni, biome_mode,
                   water_mask=None, water_density=None,
                   building_mask=None,
                   world_rows=None, world_cols=None,
                   mc_width=None, mc_depth=None):
    """
    Build and return an amulet Chunk for chunk coordinates (cx, cz).

    `tile_grid` is the halo-inclusive elevation grid for the owning tile;
    `tile_origin = (row, col)` is the global cell index of tile_grid[0, 0].
    The halo must be wide enough to cover every cell this chunk reads — see
    chunks_owned_by_tile / load_tile_with_halo.

    `water_mask` / `water_density` share tile_grid's shape. `building_mask` is
    K-times denser (K = CELL_SIZE_M // BUILDING_SUBCELL_M) and covers the same
    halo-inclusive extent.

    `world_rows` / `world_cols` are the global cell dimensions, used to mark
    blocks past the world edge as elev=0. `mc_width` / `mc_depth` are global
    block dimensions used for the perimeter rim.
    """
    chunk = Chunk(cx, cz)
    pal = chunk.block_palette

    # Register all blocks and get their palette IDs
    ids = {name: pal.get_add_block(blk) for name, blk in block_uni.items()}
    AIR_ID     = ids["air"]
    BEDROCK_ID = ids["bedrock"]
    STONE_ID   = ids["stone"]
    DIRT_ID    = ids["dirt"]
    SAND_ID    = ids["sand"]
    SANDS_ID   = ids["sandstone"]
    GRAVEL_ID  = ids["gravel"]
    SNOW_ID    = ids["snow"]
    GRASS_ID   = ids["grass"]
    WATER_ID   = ids["water"]
    BRICKS_ID  = ids["bricks"]

    surf_ids = {
        "snow":   SNOW_ID,
        "stone":  STONE_ID,
        "grass":  GRASS_ID,
        "sand":   SAND_ID,
        "gravel": GRAVEL_ID,
    }
    sub_ids = {
        "stone":     STONE_ID,
        "dirt":      DIRT_ID,
        "sandstone": SANDS_ID,
    }

    t_row0, t_col0 = tile_origin
    tg_rows, tg_cols = tile_grid.shape
    # If world dims aren't supplied, fall back to the local grid shape
    # (single-tile standalone use).
    w_rows = world_rows if world_rows is not None else tg_rows
    w_cols = world_cols if world_cols is not None else tg_cols
    K_BLD = CELL_SIZE_M // BUILDING_SUBCELL_M

    # Build a full-height column array: shape (16, ARRAY_HEIGHT, 16)
    # Index [lx, y_arr, lz] where y_arr = Y + ARRAY_OFFSET
    col_blocks = np.zeros((16, ARRAY_HEIGHT, 16), dtype=np.uint32)  # 0 = air

    # Track dominant biome per column for assignment
    biome_elevs = np.zeros((16, 16), dtype=np.float32)

    for lx in range(16):
        for lz in range(16):
            gx = cx * 16 + lx   # global block X
            gz = cz * 16 + lz   # global block Z

            # Map block coords → global cell coords (integer, for bounds check)
            cell_col = gx // scale
            cell_row = gz // scale

            in_bounds = 0 <= cell_row < w_rows and 0 <= cell_col < w_cols

            if not in_bounds:
                elev = 0.0
                is_mask_water_cell = False
            elif scale > 1:
                # Bilinear sampling for both elevation and the water mask so
                # neither shows a 2x2/4x4 grid pattern at the cell boundaries.
                # Block centres are at (gx+0.5, gz+0.5) block-space; cell centres
                # sit at (c+0.5)*scale, so the float grid coordinate is:
                fx = (gx + 0.5) / scale - 0.5
                fz = (gz + 0.5) / scale - 0.5
                c0 = int(np.floor(fx)); r0 = int(np.floor(fz))
                tx = fx - c0;           tz = fz - r0
                # Clamp to world bounds, then translate into tile_grid local.
                c0c = max(0, min(w_cols - 1, c0))     - t_col0
                c1c = max(0, min(w_cols - 1, c0 + 1)) - t_col0
                r0c = max(0, min(w_rows - 1, r0))     - t_row0
                r1c = max(0, min(w_rows - 1, r0 + 1)) - t_row0
                w00 = (1 - tx) * (1 - tz)
                w01 = tx       * (1 - tz)
                w10 = (1 - tx) * tz
                w11 = tx       * tz
                elev = float(
                    w00 * tile_grid[r0c, c0c] + w01 * tile_grid[r0c, c1c]
                    + w10 * tile_grid[r1c, c0c] + w11 * tile_grid[r1c, c1c]
                )
                if water_density is not None:
                    wm = (w00 * water_density[r0c, c0c] + w01 * water_density[r0c, c1c]
                          + w10 * water_density[r1c, c0c] + w11 * water_density[r1c, c1c])
                    is_mask_water_cell = wm >= WATER_MASK_THRESHOLD
                else:
                    is_mask_water_cell = False
            else:
                lr = cell_row - t_row0
                lc = cell_col - t_col0
                elev = float(tile_grid[lr, lc])
                is_mask_water_cell = (
                    water_mask is not None and bool(water_mask[lr, lc])
                )

            # Buildings use a separate sub-cell mask (BUILDING_SUBCELL_M).
            # Nearest-neighbour lookup per block — no bilinear blur, so
            # isolated buildings stay crisp instead of merging into blocks.
            is_mask_building_cell = False
            if in_bounds and building_mask is not None:
                mr, mc = building_mask.shape
                sub_col = int((gx + 0.5) * CELL_SIZE_M / (scale * BUILDING_SUBCELL_M)) - t_col0 * K_BLD
                sub_row = int((gz + 0.5) * CELL_SIZE_M / (scale * BUILDING_SUBCELL_M)) - t_row0 * K_BLD
                if 0 <= sub_row < mr and 0 <= sub_col < mc:
                    is_mask_building_cell = bool(building_mask[sub_row, sub_col])

            biome_elevs[lx, lz] = elev

            y_surf = int(round(MAP_ZERO_Y + elev * vscale))
            y_surf = max(Y_MIN, min(Y_MAX, y_surf))
            # Force sea floor at least one block below sea surface so water can sit on top.
            # Without this, cells with elev ~= 0 round to y_surf = MAP_ZERO_Y (Y=64) and
            # leave no room for a water block at SEA_SURFACE_Y (Y=63).
            if elev <= 0 and y_surf > SEA_SURFACE_Y - 1:
                y_surf = SEA_SURFACE_Y - 1
            arr_surf = y_surf + ARRAY_OFFSET  # array index of surface

            # --- Bedrock at Y_MIN ---
            col_blocks[lx, 0, lz] = BEDROCK_ID

            # --- Stone fill: Y_MIN+1 .. y_surf-4 ---
            stone_end = max(1, arr_surf - 3)
            if stone_end > 1:
                col_blocks[lx, 1:stone_end, lz] = STONE_ID

            # --- Sub-surface: y_surf-3 .. y_surf-1 ---
            surf_key, sub_key = _surface_and_sub(elev)
            sub_id  = sub_ids.get(sub_key, STONE_ID)
            surf_id = surf_ids.get(surf_key, GRASS_ID)

            sub_start = max(1, arr_surf - 3)
            sub_end   = arr_surf
            if sub_end > sub_start:
                col_blocks[lx, sub_start:sub_end, lz] = sub_id

            # --- Surface block ---
            if 0 <= arr_surf < ARRAY_HEIGHT:
                col_blocks[lx, arr_surf, lz] = surf_id

            # --- Water fill (for elevation <= 0) ---
            if elev <= 0 and arr_surf < SEA_SURFACE_Y + ARRAY_OFFSET:
                water_start = arr_surf + 1
                water_end   = SEA_SURFACE_Y + ARRAY_OFFSET + 1   # inclusive of Y=63
                if water_end > water_start:
                    col_blocks[lx, water_start:water_end, lz] = WATER_ID

            # --- Inland water overlay: replace top block with water ---
            # Applies to cells flagged by TIFF-blue or flat-area detection.
            # Sea (elev <= 0) is already handled above, so we only touch elev > 0.
            if (is_mask_water_cell
                    and elev > 0
                    and 0 <= arr_surf < ARRAY_HEIGHT):
                col_blocks[lx, arr_surf, lz] = WATER_ID

            # --- Buildings: stack bricks above the surface ---
            # Skip if this cell was flagged as water (building detector can
            # touch harbours / river banks). Only on land (elev > 0).
            if (is_mask_building_cell
                    and not is_mask_water_cell
                    and elev > 0):
                bld_start = arr_surf + 1
                bld_end   = min(ARRAY_HEIGHT, bld_start + BUILDING_HEIGHT_BLOCKS)
                if bld_end > bld_start:
                    col_blocks[lx, bld_start:bld_end, lz] = BRICKS_ID

            # --- Perimeter rim: only where water reaches the world edge ---
            if (mc_width is not None and mc_depth is not None
                    and (gx == 0 or gx == mc_width - 1
                         or gz == 0 or gz == mc_depth - 1)):
                is_mask_water = is_mask_water_cell and elev > 0
                is_sea_water = elev <= 0 and arr_surf < SEA_SURFACE_Y + ARRAY_OFFSET
                if is_mask_water:
                    water_top_arr = arr_surf
                elif is_sea_water:
                    water_top_arr = SEA_SURFACE_Y + ARRAY_OFFSET
                else:
                    water_top_arr = None
                if water_top_arr is not None:
                    rim_start = max(0, arr_surf)
                    rim_end = min(ARRAY_HEIGHT - 1, water_top_arr + 1)
                    if rim_end >= rim_start:
                        col_blocks[lx, rim_start:rim_end + 1, lz] = STONE_ID

    # --- Add sections to chunk ---
    for si in range(-4, 20):   # sections covering Y -64..319
        arr_start = si * 16 + ARRAY_OFFSET
        arr_end   = arr_start + 16
        if arr_end <= 0 or arr_start >= ARRAY_HEIGHT:
            continue
        section_data = col_blocks[:, arr_start:arr_end, :]
        if np.any(section_data != 0):
            chunk.blocks.add_section(si, section_data)

    # --- Biomes ---
    if biome_mode == "elevation":
        chunk.biomes.convert_to_3d()
        avg_elev = float(np.mean(biome_elevs))
        biome_name = _biome_name(avg_elev)
        biome_id = chunk.biome_palette.get_add_biome(biome_name)

        for si in range(-4, 20):
            sec = np.full((4, 4, 4), biome_id, dtype=np.uint32)
            chunk.biomes.add_section(si, sec)

    return chunk

# ---------------------------------------------------------------------------
# Spawn point helpers
# ---------------------------------------------------------------------------


def write_entity_files(world_path, cx_min, cx_max, cz_min, cz_max):
    """
    Create entities/<r.X.Z.mca> files for every chunk in the generated range.
    Minecraft 1.17+ stores entity data separately from chunk data; without these
    files every chunk fails to load with NoSuchElementException in EntityStorage.

    Each chunk entry is an NBT compound with DataVersion, Position, and an empty
    Entities list — the minimum Minecraft needs to not error.
    """
    import struct, zlib

    entities_dir = os.path.join(world_path, "entities")
    os.makedirs(entities_dir, exist_ok=True)

    # Group chunks by region file (32×32 chunks per region)
    regions = {}
    for cx in range(cx_min, cx_max):
        for cz in range(cz_min, cz_max):
            rx, rz = cx >> 5, cz >> 5
            regions.setdefault((rx, rz), []).append((cx, cz))

    for (rx, rz), chunks in regions.items():
        out = os.path.join(entities_dir, f"r.{rx}.{rz}.mca")

        # Build sector data for each chunk
        location_table  = bytearray(4096)  # 1024 × 4 bytes
        timestamp_table = bytearray(4096)  # 1024 × 4 bytes
        sector_data     = bytearray()

        sector_offset = 2  # first two sectors are the header tables

        for cx, cz in chunks:
            # Local chunk position within region
            local_cx = cx & 0x1f
            local_cz = cz & 0x1f
            table_idx = local_cz * 32 + local_cx

            # Minimal entity chunk NBT
            nbt = amulet_nbt.NamedTag(
                amulet_nbt.CompoundTag({
                    "DataVersion": amulet_nbt.IntTag(3953),
                    "Position":   amulet_nbt.IntArrayTag([cx, cz]),
                    "Entities":   amulet_nbt.ListTag([]),
                })
            )
            raw_nbt   = nbt.to_nbt(compressed=False, little_endian=False)
            compressed = zlib.compress(raw_nbt)

            # Chunk payload: 4-byte length + 1-byte compression type + data
            payload = struct.pack(">I", len(compressed) + 1) + b"\x02" + compressed

            # Pad to 4096-byte sector boundary
            pad = (4096 - len(payload) % 4096) % 4096
            payload += b"\x00" * pad
            sectors = len(payload) // 4096

            # Write location entry: 3-byte offset + 1-byte sector count
            loc_bytes = struct.pack(">I", (sector_offset << 8) | sectors)
            location_table[table_idx * 4:(table_idx + 1) * 4] = loc_bytes

            sector_data += payload
            sector_offset += sectors

        with open(out, "wb") as f:
            f.write(bytes(location_table))
            f.write(bytes(timestamp_table))
            f.write(bytes(sector_data))

    print(f"Entity files written: {len(regions)} region(s) in {entities_dir}")


def _void_flat_generator(biome="minecraft:plains"):
    """Return a flat/void generator compound.

    Triggers Minecraft's "Worlds using Experimental Settings are not
    supported" warning on load because inline flat-generator settings are
    treated as custom worldgen. Only used when the user passes --void to
    opt into that warning in exchange for keeping the void boundary beyond
    the pre-filled OS area.
    """
    return amulet_nbt.CompoundTag({
        "type": amulet_nbt.StringTag("minecraft:flat"),
        "settings": amulet_nbt.CompoundTag({
            "biome": amulet_nbt.StringTag(biome),
            "features": amulet_nbt.ByteTag(0),
            "lakes": amulet_nbt.ByteTag(0),
            "layers": amulet_nbt.ListTag([
                amulet_nbt.CompoundTag({
                    "block":  amulet_nbt.StringTag("minecraft:air"),
                    "height": amulet_nbt.IntTag(1),
                })
            ]),
        }),
    })


def _vanilla_generator(dim):
    """Return the vanilla noise generator for a standard dimension.

    Uses preset references ("minecraft:overworld", "minecraft:nether",
    "minecraft:the_end") so WorldGenSettings exactly matches the shape
    Minecraft writes for a default-created world. Any inline custom
    worldgen (flat with custom layers, custom biome sources, etc.) is
    treated by Minecraft's Codec as a custom dimension and permanently
    flags the world as experimental — which surfaces the
    "Worlds using Experimental Settings are not supported" warning on
    every load, even after the user upgrades the world.

    Tradeoff: chunks beyond our pre-filled OS area will fill with vanilla
    terrain instead of void. The pre-filled chunks themselves load as-is
    from the region files and are not affected.
    """
    if dim == "minecraft:overworld":
        return amulet_nbt.CompoundTag({
            "type": amulet_nbt.StringTag("minecraft:noise"),
            "biome_source": amulet_nbt.CompoundTag({
                "type":   amulet_nbt.StringTag("minecraft:multi_noise"),
                "preset": amulet_nbt.StringTag("minecraft:overworld"),
            }),
            "settings": amulet_nbt.StringTag("minecraft:overworld"),
        })
    if dim == "minecraft:the_nether":
        return amulet_nbt.CompoundTag({
            "type": amulet_nbt.StringTag("minecraft:noise"),
            "biome_source": amulet_nbt.CompoundTag({
                "type":   amulet_nbt.StringTag("minecraft:multi_noise"),
                "preset": amulet_nbt.StringTag("minecraft:nether"),
            }),
            "settings": amulet_nbt.StringTag("minecraft:nether"),
        })
    if dim == "minecraft:the_end":
        return amulet_nbt.CompoundTag({
            "type": amulet_nbt.StringTag("minecraft:noise"),
            "biome_source": amulet_nbt.CompoundTag({
                "type": amulet_nbt.StringTag("minecraft:the_end"),
            }),
            "settings": amulet_nbt.StringTag("minecraft:end"),
        })
    raise ValueError(f"Unknown dimension: {dim}")


def patch_level_dat(world_path, world_name, mc_x, mc_y, mc_z, void=False):
    """
    Replace the minimal amulet-generated level.dat with a complete, valid one
    that Minecraft 1.21.x will accept.

    void=False (default): references the vanilla noise generators so
    Minecraft treats the world as a standard one (no experimental warning).
    Chunks beyond the pre-filled area get vanilla terrain.

    void=True: writes inline flat-void generators so areas beyond the
    pre-filled map stay as void. Minecraft will show the
    "Worlds using Experimental Settings are not supported" warning.
    """
    dat_path = os.path.join(world_path, "level.dat")
    nbt = amulet_nbt.load(dat_path)
    data = nbt.tag["Data"]

    # --- Spawn ---
    data["SpawnX"] = amulet_nbt.IntTag(mc_x)
    data["SpawnY"] = amulet_nbt.IntTag(mc_y)
    data["SpawnZ"] = amulet_nbt.IntTag(mc_z)
    data["SpawnAngle"] = amulet_nbt.FloatTag(0.0)

    # --- World name and game settings ---
    data["LevelName"] = amulet_nbt.StringTag(world_name)
    data["GameType"]  = amulet_nbt.IntTag(1)        # 1 = creative
    data["Difficulty"] = amulet_nbt.ByteTag(2)       # 2 = normal
    data["allowCommands"] = amulet_nbt.ByteTag(1)
    data["hardcore"]  = amulet_nbt.ByteTag(0)
    data["initialized"] = amulet_nbt.ByteTag(1)
    data["DayTime"]   = amulet_nbt.LongTag(6000)
    data["Time"]      = amulet_nbt.LongTag(0)
    data["rainTime"]  = amulet_nbt.IntTag(0)
    data["raining"]   = amulet_nbt.ByteTag(0)
    data["thunderTime"] = amulet_nbt.IntTag(0)
    data["thundering"] = amulet_nbt.ByteTag(0)

    # --- Version compound (required for 1.18+) ---
    # DataVersion 3953 = Java 1.21.0.  Minecraft will auto-upgrade the world data.
    data["Version"] = amulet_nbt.CompoundTag({
        "Id":       amulet_nbt.IntTag(3953),
        "Name":     amulet_nbt.StringTag("1.21"),
        "Series":   amulet_nbt.StringTag("main"),
        "Snapshot": amulet_nbt.ByteTag(0),
    })

    # --- DataPacks / enabled_features (suppress "Experimental Settings" warning) ---
    # Amulet's default level.dat enables experimental datapacks (bundle,
    # trade_rebalance, etc.) which causes Minecraft 1.21 to show
    # "Worlds using Experimental Settings are not supported" on load.
    # Force a vanilla-only configuration.
    # Empty Disabled list must be typed as string (tag id 8); amulet_nbt
    # defaults an untyped empty ListTag to byte (1), which Minecraft's
    # DataPacks codec rejects.
    data["DataPacks"] = amulet_nbt.CompoundTag({
        "Enabled":  amulet_nbt.ListTag([amulet_nbt.StringTag("vanilla")]),
        "Disabled": amulet_nbt.ListTag([], 8),
    })
    data["enabled_features"] = amulet_nbt.ListTag(
        [amulet_nbt.StringTag("minecraft:vanilla")]
    )

    # --- WorldGenSettings ---
    if void:
        overworld_gen = _void_flat_generator("minecraft:plains")
        nether_gen    = _void_flat_generator("minecraft:nether_wastes")
        end_gen       = _void_flat_generator("minecraft:the_end")
    else:
        overworld_gen = _vanilla_generator("minecraft:overworld")
        nether_gen    = _vanilla_generator("minecraft:the_nether")
        end_gen       = _vanilla_generator("minecraft:the_end")

    data["WorldGenSettings"] = amulet_nbt.CompoundTag({
        "bonus_chest":       amulet_nbt.ByteTag(0),
        "generate_features": amulet_nbt.ByteTag(0),
        "seed":              amulet_nbt.LongTag(0),
        "dimensions": amulet_nbt.CompoundTag({
            "minecraft:overworld": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:overworld"),
                "generator": overworld_gen,
            }),
            "minecraft:the_nether": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:the_nether"),
                "generator": nether_gen,
            }),
            "minecraft:the_end": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:the_end"),
                "generator": end_gen,
            }),
        }),
    })

    nbt.save_to(dat_path)
    print(f"Spawn set: X={mc_x} Y={mc_y} Z={mc_z}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a Minecraft world from OS Terrain 50 data.")
    parser.add_argument("input",          help="Tile .zip, region folder, or data root folder")
    parser.add_argument("--scale",   type=int,   default=1,          help="Blocks per OS cell (default 1)")
    parser.add_argument("--vscale",  type=float, default=0.10,       help="Vertical scale multiplier (default 0.10)")
    parser.add_argument("--biomes",  choices=["default", "elevation"], default="elevation",
                                                                      help="Biome mode (default: elevation)")
    parser.add_argument("--spawn",   default=None, metavar="LAT,LON",
                        help="Spawn at a WGS84 geo coord 'lat,lon' (e.g. '56.97,-3.40'). "
                             "Falls back to map centre if the point is outside the map.")
    parser.add_argument("--void", action="store_true",
                        help="Keep the void boundary beyond the pre-filled OS area. "
                             "Minecraft will show the 'Worlds using Experimental Settings "
                             "are not supported' warning on load (default: vanilla terrain, "
                             "no warning).")
    parser.add_argument("--out",     default=None,                   help="Output world folder")
    parser.add_argument("--no-water", action="store_true",
                        help="Skip inland water detection and perimeter rim.")
    parser.add_argument("--no-rivers", action="store_true",
                        help="Skip OS Open Rivers rasterization (auto-off at scale < 4).")
    parser.add_argument("--buildings", action="store_true",
                        help="Detect light-orange building fill from OS raster TIFFs and "
                             f"place bricks on those cells (auto-off at scale < {MIN_BUILDING_SCALE}).")
    parser.add_argument("--tiles-dir", default=None,
                        help="Path to OS raster tiles root (default: '<input-parent>/../tiles').")
    parser.add_argument("--rivers-path", default=None,
                        help="Path to OS Open Rivers .mbtiles "
                             "(default: '<input-parent>/../rivers/Data/oprvrs_gb.mbtiles').")
    parser.add_argument("--halo", type=int, default=None,
                        help="Halo cells loaded around each tile for seamless bilinear / flat-area "
                             "detection. Defaults to ceil(16 / scale) + 3 (19 at scale=1, 5 at "
                             "scale=8). Bigger values cost more zip reads per tile.")
    parser.add_argument("--flush-every", type=int, default=1,
                        help="Flush amulet's in-memory chunk cache every N tiles. Default 1 "
                             "(flush after each tile) for minimum memory. Higher = fewer saves "
                             "but more RAM.")
    args = parser.parse_args()

    # --- Discover tiles ---
    zip_entries = discover_zips(args.input)
    print(f"Found {len(zip_entries)} tiles.")

    # Derive a short world name from the input path
    raw = os.path.basename(os.path.normpath(args.input))
    # For a single zip like "nn30_OST50GRID_20250529.zip" extract just the tile code
    m = re.match(r"([a-z]{2}\d{2})_", raw.lower())
    short = m.group(1).upper() if m else re.sub(r"[^a-zA-Z0-9]", "", raw)[:8].upper()
    world_name = f"OS_{short}_s{args.scale}_v{args.vscale}"
    out_path   = args.out or os.path.join(os.path.dirname(__file__), "worlds", world_name)

    if os.path.exists(out_path):
        print(f"Removing existing world at {out_path}")
        shutil.rmtree(out_path)
    os.makedirs(out_path, exist_ok=True)

    # --- Headers + global extent (cheap — reads only header bytes) ---
    headers = scan_headers(zip_entries)
    origin_easting, origin_northing_top, total_rows, total_cols = compute_global_extent(headers)
    tile_index = build_tile_index(headers, origin_easting, origin_northing_top)
    print(f"Grid: {total_cols} x {total_rows} cells  "
          f"({total_cols*CELL_SIZE_M/1000:.0f} km x {total_rows*CELL_SIZE_M/1000:.0f} km)")

    # --- Halo size ---
    # ceil(16 / scale) covers chunk blocks that extend past the tile edge,
    # +1 for bilinear neighbour, +2 for the 5x5 flat-area window.
    halo = args.halo if args.halo is not None else ceil(16 / args.scale) + 3
    print(f"Halo: {halo} cells per side")

    # --- Resolve tiles dir (shared by water + buildings detection) ---
    if args.tiles_dir:
        tiles_dir = args.tiles_dir
    else:
        first_zip_dir = os.path.dirname(zip_entries[0][0])
        tiles_dir = os.path.normpath(os.path.join(first_zip_dir, "..", "..", "tiles"))

    # --- Resolve rivers path (opened per-tile below) ---
    do_rivers = (not args.no_rivers) and args.scale >= 4
    rivers_path = None
    if do_rivers:
        if args.rivers_path:
            rivers_path = args.rivers_path
        else:
            first_zip_dir = os.path.dirname(zip_entries[0][0])
            rivers_path = os.path.normpath(os.path.join(
                first_zip_dir, "..", "..", "rivers", "Data", "oprvrs_gb.mbtiles"))
        if not os.path.isfile(rivers_path):
            print(f"Rivers file not found at {rivers_path} — skipping.")
            rivers_path = None
            do_rivers = False
    elif not args.no_rivers and args.scale < 4:
        print(f"Skipping rivers (--scale {args.scale} < 4 — lines would be too fat).")

    do_buildings = args.buildings and args.scale >= MIN_BUILDING_SCALE
    if args.buildings and args.scale < MIN_BUILDING_SCALE:
        print(f"Skipping buildings (--scale {args.scale} < {MIN_BUILDING_SCALE} "
              "— each cell resolves to <1 block).")

    # --- Resolve spawn (needed before patch_level_dat) ---
    spawn_col = total_cols // 2
    spawn_row = total_rows // 2
    if args.spawn:
        try:
            lat_s, lon_s = args.spawn.split(",")
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            print(f"--spawn: could not parse '{args.spawn}' — expected 'lat,lon'. "
                  "Using map centre.")
        else:
            east, north = wgs84_to_bng(lat, lon)
            col = int((east - origin_easting) / CELL_SIZE_M)
            row = int((origin_northing_top - north) / CELL_SIZE_M)
            if 0 <= col < total_cols and 0 <= row < total_rows:
                spawn_col, spawn_row = col, row
                print(f"Spawn set from {lat}, {lon} -> grid cell ({col}, {row}).")
            else:
                print(f"Spawn coord {lat}, {lon} (BNG {east:.0f}E {north:.0f}N) "
                      f"is outside the generated map — using map centre.")
    spawn_elev = resolve_spawn_elev(spawn_row, spawn_col, headers, tile_index,
                                    origin_easting, origin_northing_top)
    spawn_x = (spawn_col * args.scale) + (args.scale // 2)
    spawn_z = (spawn_row * args.scale) + (args.scale // 2)
    spawn_y = MAP_ZERO_Y + round(spawn_elev * args.vscale) + 1

    # --- Create world ---
    print(f"\nCreating world: {world_name}")
    fmt = AnvilFormat(out_path)
    fmt.create_and_open(MC_VERSION_ID, MC_VERSION, overwrite=True)
    fmt.close()

    # Patch level.dat BEFORE load_level. Amulet's AnvilFormat reads bounds from
    # level.dat's WorldGenSettings when the level loads; if WorldGenSettings is
    # missing (as it is in amulet's fresh create_and_open output), it falls
    # back to DefaultSelection (Y=0..256) and every saved chunk gets truncated
    # to 16 sections, silently dropping anything above Y=255 or below Y=0.
    patch_level_dat(out_path, world_name, spawn_x, spawn_y, spawn_z, void=args.void)

    level = amulet.load_level(out_path)
    block_uni = _make_palette(level)

    # --- World dimensions ---
    mc_width  = total_cols * args.scale
    mc_depth  = total_rows * args.scale
    cx_max = ceil(mc_width  / 16)
    cz_max = ceil(mc_depth  / 16)
    total_chunks = cx_max * cz_max

    print(f"World size: {mc_width} x {mc_depth} blocks  ({total_chunks} chunks)")
    print(f"Scale: --scale {args.scale}  --vscale {args.vscale}  --biomes {args.biomes}")
    print(f"Streaming {len(headers)} tiles (flush every {args.flush_every})...")

    # --- Rivers: open SQLite connection once, reuse across tiles ---
    rivers_conn = None
    if do_rivers:
        import sqlite3
        rivers_conn = sqlite3.connect(rivers_path)

    elev_min, elev_max = float("inf"), float("-inf")
    tiles_done = 0

    with tqdm(total=total_chunks, unit="chunk") as pbar:
        for key in headers:
            # Load elevation + halo for this tile
            tile_grid, core_row0, core_col0, nrows, ncols = load_tile_with_halo(
                key, headers, tile_index, origin_easting, origin_northing_top, halo)
            tile_origin = (core_row0 - halo, core_col0 - halo)

            # Track elevation range (core only — halo elevations are counted by
            # the tiles that own them)
            core_slice = tile_grid[halo:halo + nrows, halo:halo + ncols]
            if core_slice.size:
                elev_min = min(elev_min, float(core_slice.min()))
                elev_max = max(elev_max, float(core_slice.max()))

            # Masks for this tile
            water_mask = None
            if not args.no_water:
                water_mask = water_mask_for_tile(
                    tile_grid, core_row0, core_col0, nrows, ncols, halo,
                    headers, tile_index, tiles_dir)

            river_mask = None
            if do_rivers:
                local_min_east = origin_easting + (core_col0 - halo) * CELL_SIZE_M
                local_max_north = origin_northing_top - (core_row0 - halo) * CELL_SIZE_M
                river_mask = rasterize_rivers_for_tile(
                    tile_grid.shape, local_min_east, local_max_north,
                    rivers_path, conn=rivers_conn)
                if river_mask is not None:
                    water_mask = river_mask if water_mask is None else (water_mask | river_mask)

            water_density = None
            if water_mask is not None:
                m = water_mask.astype(np.float32)
                p = np.pad(m, 1, mode="edge")
                water_density = (
                    1 * p[:-2, :-2] + 2 * p[:-2, 1:-1] + 1 * p[:-2, 2:]
                    + 2 * p[1:-1, :-2] + 4 * p[1:-1, 1:-1] + 2 * p[1:-1, 2:]
                    + 1 * p[2:, :-2] + 2 * p[2:, 1:-1] + 1 * p[2:, 2:]
                ) / 16.0
                water_density = np.maximum(water_density, m * WATER_CELL_FLOOR)
                if river_mask is not None:
                    water_density[river_mask] = 1.0

            building_mask = None
            if do_buildings:
                building_mask = building_mask_for_tile(
                    core_row0, core_col0, nrows, ncols, halo,
                    headers, tile_index, tiles_dir)

            # Generate chunks owned by this tile
            for cx, cz in chunks_owned_by_tile(core_row0, core_col0, nrows, ncols, args.scale):
                chunk = generate_chunk(
                    cx, cz, tile_grid, tile_origin,
                    scale=args.scale,
                    vscale=args.vscale,
                    block_uni=block_uni,
                    biome_mode=args.biomes,
                    water_mask=water_mask,
                    water_density=water_density,
                    building_mask=building_mask,
                    world_rows=total_rows,
                    world_cols=total_cols,
                    mc_width=mc_width,
                    mc_depth=mc_depth,
                )
                level.put_chunk(chunk, DIMENSION)
                pbar.update(1)

            tiles_done += 1
            # Release mask references before the flush so the arrays can be GC'd
            tile_grid = water_mask = water_density = building_mask = river_mask = None
            if tiles_done % args.flush_every == 0:
                level.save()
                level.unload()

    # Final flush for any residual chunks
    print("Saving world...")
    level.save()
    level.close()

    if rivers_conn is not None:
        rivers_conn.close()

    # --- Entity storage (required by Minecraft 1.17+) ---
    write_entity_files(out_path, 0, cx_max, 0, cz_max)

    # --- Summary ---
    region_files = glob.glob(os.path.join(out_path, "region", "*.mca"))
    y_min_used = MAP_ZERO_Y + round(elev_min * args.vscale)
    y_max_used = MAP_ZERO_Y + round(elev_max * args.vscale)

    print(f"\nDone!")
    print(f"  World:       {out_path}")
    print(f"  Region files:{len(region_files)}")
    print(f"  Elevation:   {elev_min:.0f} m .. {elev_max:.0f} m")
    print(f"  Y range:     {y_min_used} .. {y_max_used}")
    print(f"\nCopy '{out_path}' into your Minecraft saves folder to play.")


if __name__ == "__main__":
    main()
