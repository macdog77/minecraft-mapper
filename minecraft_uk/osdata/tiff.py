"""OS OpenMap Local raster TIFF handling.

Quadrant discovery, palette-based feature detection (water / buildings), and
neighbour-aware iteration so tile-local masks stay seamless across borders.
"""

import os

import numpy as np
from PIL import Image
from tqdm import tqdm

from ..common.paths import TILES_ROOT


# Quadrant geometry
SEA_COLOUR   = (142, 216, 234)
QUAD_PX      = 5000                 # pixels per 5 km quadrant
TILE_PX      = QUAD_PX * 2          # 10 000 px per full 10 km tile
TILE_SIZE_M  = 10_000
QUADRANTS    = {
    "NW": (0, 0),       "NE": (QUAD_PX, 0),
    "SW": (0, QUAD_PX), "SE": (QUAD_PX, QUAD_PX),
}

# Feature-detection tuning
TIFF_PX_PER_M            = 1      # OS tiles are 1 m/pixel
TIFF_WATER_THRESHOLD     = 0.5    # fraction of blue pixels per cell to mark as water
TIFF_BUILDING_THRESHOLD  = 0.30   # fraction of peach pixels per sub-cell to mark as building


def find_tiffs(region, e_digit, n_digit, tiles_root=TILES_ROOT):
    """Return {quadrant: path} for the TIFF files belonging to one 10 km tile."""
    rc = region.upper()
    tile = f"{rc}{e_digit}{n_digit}"
    d = os.path.join(tiles_root, rc)
    found = {}
    for q in QUADRANTS:
        p = os.path.join(d, f"{tile}{q}.tif")
        if os.path.exists(p):
            found[q] = p
    return found


def is_water_color(r, g, b):
    """True if an OS raster palette entry is a water colour (pale cyan..deeper blue)."""
    return (r < g - 10) and (r < b - 10) and (g + b) > 300


def is_building_color(r, g, b):
    """True if an OS raster palette entry is the light-peach building fill.

    Calibrated against OS Explorer raster tiles: the building fill is
    #f8d8b8 — RGB (248, 216, 184). Tight tolerances are enough because the
    palette maps exactly to that entry on every tile.
    """
    return (abs(r - 248) <= 8
            and abs(g - 216) <= 8
            and abs(b - 184) <= 8
            and r > g > b)


def tile_palette_fraction(tif_path, cell_size_px, predicate):
    """Fraction of palette-matching pixels per cell_size_px × cell_size_px block."""
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


def tile_water_fraction(tif_path, cell_size_px):
    """Read a palette TIFF quadrant and return fraction-of-water per cell grid."""
    return tile_palette_fraction(tif_path, cell_size_px, is_water_color)


def tile_building_fraction(tif_path, cell_size_px):
    """Read a palette TIFF quadrant and return fraction-of-building per cell grid."""
    return tile_palette_fraction(tif_path, cell_size_px, is_building_color)


def iter_neighbour_tiffs(headers, tile_index, core_row0, core_col0, nrows, ncols,
                         tiles_dir, cell_size_px, fraction_fn):
    """Yield (frac_array, global_row_offset, global_col_offset) for every TIFF
    quadrant belonging to this tile and its 8 neighbours.

    `fraction_fn` is one of tile_water_fraction / tile_building_fraction. Used
    by feature mask builders so they all share the neighbour-walking boilerplate.
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
                    frac = fraction_fn(tif_path, cell_size_px=cell_size_px)
                except Exception as ex:
                    tqdm.write(f"  Warning: {os.path.basename(tif_path)}: {ex}")
                    continue
                yield frac, n_row0 + q_row, n_col0 + q_col
