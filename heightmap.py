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

import sys
import os
import re
import zipfile
import io
from PIL import Image


BEN_NEVIS_M = 1345.0  # default white point
TIFF_OPACITY = 0.50


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


def overlay_tiff(canvas, input_path, opacity=TIFF_OPACITY):
    """
    Composite the four OS raster TIFF quadrants for this tile over the canvas
    at the given opacity. Returns an RGB image. If the tile code can't be
    parsed or no TIFFs are found, returns the canvas converted to RGB.
    """
    name = os.path.basename(input_path).lower()
    m = re.match(r"([a-z]{2})(\d)(\d)", name)
    if not m:
        print(f"  TIFF overlay: cannot parse tile code from "
              f"{os.path.basename(input_path)}; skipping.")
        return canvas.convert("RGB")
    region_code = m.group(1)
    e_d, n_d = int(m.group(2)), int(m.group(3))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mesh import _find_tiffs, QUADRANTS, TILE_PX as TIFF_TILE_PX, SEA_COLOUR

    tiffs = _find_tiffs(region_code, e_d, n_d)
    if not tiffs:
        print(f"  TIFF overlay: no TIFFs found for "
              f"{region_code.upper()}{e_d}{n_d}; skipping.")
        return canvas.convert("RGB")

    tile_img = Image.new("RGB", (TIFF_TILE_PX, TIFF_TILE_PX), SEA_COLOUR)
    for q, qpath in tiffs.items():
        try:
            tile_img.paste(Image.open(qpath).convert("RGB"), QUADRANTS[q])
        except Exception:
            pass

    tile_img = tile_img.resize(canvas.size, Image.LANCZOS)
    print(f"  TIFF overlay: {len(tiffs)} quadrant(s) for "
          f"{region_code.upper()}{e_d}{n_d} @ {int(opacity * 100)}%")
    return Image.blend(canvas.convert("RGB"), tile_img, opacity)


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
