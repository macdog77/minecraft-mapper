#!/usr/bin/env python3
"""
heightmap.py — Generate a greyscale PNG heightmap from an OS Terrain 50 tile.

Usage:
    python heightmap.py <tile.zip>          # reads the .asc inside the zip
    python heightmap.py <tile.asc>          # reads the .asc directly
    python heightmap.py <tile.zip> [max_elev]

Colour scale: black = 0 m (sea level), white = max_elev m (default 1345 m, top of Ben Nevis).
Values <= 0 (sea / NODATA) are clamped to black.
Output is saved alongside the input file as <tilename>_heightmap.png.
"""

import sys
import os
import zipfile
import io
from PIL import Image


BEN_NEVIS_M = 1345.0  # default white point


def parse_asc(fileobj):
    """Parse an ESRI ASCII Grid file. Returns (header dict, list-of-rows of floats)."""
    header = {}
    rows = []
    for raw in fileobj:
        line = raw.decode("ascii").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        parts = line.split()
        if parts[0].lower() in ("ncols", "nrows", "xllcorner", "yllcorner",
                                 "xllcenter", "yllcenter", "cellsize", "nodata_value"):
            key = parts[0].lower()
            val = float(parts[1]) if "." in parts[1] else int(parts[1])
            header[key] = val
        else:
            rows.append([float(v) for v in parts])
    return header, rows


def load_tile(path):
    """Load an .asc from a path that is either a .zip or .asc file."""
    path = os.path.abspath(path)
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            asc_names = [n for n in zf.namelist() if n.lower().endswith(".asc")
                         and not n.lower().endswith(".asc.aux.xml")]
            if not asc_names:
                raise ValueError(f"No .asc file found inside {path}")
            asc_name = asc_names[0]
            print(f"Reading {asc_name} from {os.path.basename(path)}")
            with zf.open(asc_name) as f:
                return parse_asc(f), os.path.splitext(os.path.basename(path))[0]
    elif path.lower().endswith(".asc"):
        print(f"Reading {os.path.basename(path)}")
        with open(path, "r") as f:
            return parse_asc(f), os.path.splitext(os.path.basename(path))[0]
    else:
        raise ValueError("Input must be a .zip or .asc file")


def make_heightmap(rows, header, max_elev=BEN_NEVIS_M):
    nodata = header.get("nodata_value", -9999)
    height = len(rows)
    width = max(len(r) for r in rows)

    pixels = []
    # ASC rows are stored N→S (top row = northernmost), so no flip needed for a
    # map-oriented image (north up).
    for row in rows:
        for val in row:
            if val <= 0 or val == nodata:
                pixels.append(0)
            else:
                brightness = min(val / max_elev, 1.0)
                pixels.append(int(brightness * 255))
        # pad short rows
        for _ in range(width - len(row)):
            pixels.append(0)

    img = Image.new("L", (width, height))
    img.putdata(pixels)
    return img


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_path = sys.argv[1]
    max_elev = float(sys.argv[2]) if len(sys.argv) > 2 else BEN_NEVIS_M

    (header, rows), stem = load_tile(input_path)

    print(f"Grid: {header.get('ncols')}×{header.get('nrows')} cells "
          f"@ {header.get('cellsize')} m resolution")
    print(f"Origin: E{header.get('xllcorner', header.get('xllcenter'))} "
          f"N{header.get('yllcorner', header.get('yllcenter'))}")
    print(f"Scale: 0 m -> black, {max_elev} m -> white")

    img = make_heightmap(rows, header, max_elev)

    out_dir = os.path.dirname(os.path.abspath(input_path))
    out_path = os.path.join(out_dir, f"{stem}_heightmap.png")
    img.save(out_path)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
