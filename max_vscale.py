#!/usr/bin/env python3
"""
max_vscale.py — Find the maximum safe --vscale for a tile, region, or whole-UK run.

Reports the largest --vscale value that keeps every peak at or below Minecraft's
Y=319 build limit when fed to generate.py. The Minecraft mapping is
    Y = MAP_ZERO_Y + round(elevation_m * vscale)
with MAP_ZERO_Y = 64 and Y_MAX = 319, giving 255 blocks of vertical headroom
above sea level. The max safe vscale is therefore 255 / max_elevation_m.

Usage:
    python max_vscale.py <input>

    input   Single tile .zip, region folder, or the top-level data folder.

Examples:
    python max_vscale.py "OS Map Data/data/nn/nn16_OST50GRID_20250529.zip"  # Ben Nevis tile
    python max_vscale.py "OS Map Data/data/nn"                              # NN region
    python max_vscale.py "OS Map Data/data"                                 # whole UK
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from minecraft_uk.minecraft.constants import MAP_ZERO_Y, Y_MAX
from minecraft_uk.osdata.bng import bng_to_tile
from minecraft_uk.osdata.discovery import discover_zips
from minecraft_uk.osdata.tiles import (
    CELL_SIZE_M,
    compute_global_extent,
    load_tile_elev,
    scan_headers,
    tile_global_offset,
)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="Tile .zip, region folder, or data root folder")
    args = parser.parse_args()

    headroom = Y_MAX - MAP_ZERO_Y   # 255 blocks above sea level

    zip_entries = discover_zips(args.input)
    print(f"Found {len(zip_entries)} tiles.")

    # Stream tile-by-tile so a whole-UK scan doesn't load the full grid.
    headers = scan_headers(zip_entries)
    origin_easting, origin_northing_top, _total_rows, _total_cols = compute_global_extent(headers)

    max_elev = float("-inf")
    max_row = max_col = 0
    for key, (hdr, zp) in headers.items():
        _nrows, _ncols, arr = load_tile_elev(zp)
        tile_max = float(arr.max())
        if tile_max > max_elev:
            max_elev = tile_max
            local_r, local_c = np.unravel_index(int(arr.argmax()), arr.shape)
            core_row0, core_col0 = tile_global_offset(hdr, origin_easting, origin_northing_top)
            max_row = core_row0 + int(local_r)
            max_col = core_col0 + int(local_c)

    # BNG coordinates of the highest cell (cell centre)
    east  = origin_easting       + (max_col + 0.5) * CELL_SIZE_M
    north = origin_northing_top  - (max_row + 0.5) * CELL_SIZE_M
    try:
        region, e_digit, n_digit = bng_to_tile(east, north)
        tile_code = f"{region}{e_digit}{n_digit}"
    except ValueError:
        tile_code = "(outside BNG coverage)"

    print()
    print(f"Highest point: {max_elev:.1f} m  (tile {tile_code}, BNG {east:.0f}E {north:.0f}N)")
    print(f"MC headroom:   {headroom} blocks  (Y={MAP_ZERO_Y} sea level -> Y={Y_MAX} build limit)")

    if max_elev <= 0:
        print("\nNo land above sea level — any --vscale is safe.")
        return

    max_vscale = headroom / max_elev
    # Truncate to 4 dp rather than 2 so the recommended value actually lands
    # the peak near Y_MAX. 2-dp truncation on e.g. 0.1896 would return 0.18,
    # which wastes ~13 blocks of headroom — the peak renders at Y = 306 instead
    # of Y = 319 and looks "short" with empty air above it.
    suggested = int(max_vscale * 10000) / 10000
    peak_y = MAP_ZERO_Y + round(max_elev * suggested)

    print()
    print(f"Max safe --vscale: {max_vscale:.6f}")
    print(f"Suggested (4 dp):  {suggested:.4f}  -> peak lands at Y = {peak_y}")


if __name__ == "__main__":
    main()
