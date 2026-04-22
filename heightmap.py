#!/usr/bin/env python3
"""
heightmap.py — Generate a greyscale PNG heightmap from an OS Terrain 50 tile.

Usage:
    python heightmap.py <tile.zip>                    # reads the .asc inside the zip
    python heightmap.py <tile.asc>                    # reads the .asc directly
    python heightmap.py <tile.zip> [max_elev]
    python heightmap.py <tile.zip> [max_elev] --tiff  # overlay OS map at 50%

Colour scale: black = 0 m (sea level), white = max_elev m (default 1345 m, top of Ben Nevis).
Values <= 0 (sea / NODATA) are clamped to black.
Output is saved alongside the input file as <tilename>_heightmap.png
(or <tilename>_heightmap_tiff.png with --tiff).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from minecraft_uk.osdata.asc import load_tile
from minecraft_uk.rendering.heightmap import (
    BEN_NEVIS_M,
    TIFF_OPACITY,
    make_heightmap,
    overlay_tiff,
)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    input_path = args[0]
    max_elev = float(args[1]) if len(args) > 1 else BEN_NEVIS_M
    draw_tiff = "--tiff" in flags

    (header, rows), stem = load_tile(input_path)

    print(f"Grid: {header.get('ncols')}×{header.get('nrows')} cells "
          f"@ {header.get('cellsize')} m resolution")
    print(f"Origin: E{header.get('xllcorner', header.get('xllcenter'))} "
          f"N{header.get('yllcorner', header.get('yllcenter'))}")
    print(f"Scale: 0 m -> black, {max_elev} m -> white")
    if draw_tiff:
        print(f"TIFF overlay: enabled ({int(TIFF_OPACITY * 100)}% opacity)")

    img = make_heightmap(rows, header, max_elev)

    suffix = ""
    if draw_tiff:
        img = overlay_tiff(img, input_path)
        suffix = "_tiff"

    out_dir = os.path.dirname(os.path.abspath(input_path))
    out_path = os.path.join(out_dir, f"{stem}_heightmap{suffix}.png")
    img.save(out_path)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
