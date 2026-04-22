"""Tile discovery helpers.

Accepts a single zip, a region folder, or a parent folder of region folders
and returns tile metadata parsed from OS Terrain 50 filenames.
"""

import glob
import os
import re


# Matches an OS Terrain 50 tile zip, e.g. "nn16_OST50GRID_20250529.zip".
TILE_NAME_RE = re.compile(r"^([a-z]{2})(\d)(\d)_", re.IGNORECASE)


def discover_zips(input_path):
    """Return a list of (zip_path, e_digit, n_digit, region_code) tuples.

    Accepts a single zip, a region folder, or a parent folder of region folders.
    Tiles whose filenames don't match the OS pattern are silently skipped.
    """
    # Strip a trailing quote: on Windows bash, `"OS Map Data\data\nn\"` escapes
    # the closing quote and leaves a literal `"` on the end of the argument.
    input_path = input_path.rstrip('"').rstrip("'")
    input_path = os.path.abspath(input_path)

    if not os.path.exists(input_path):
        raise ValueError(f"Input path does not exist: {input_path}")

    if input_path.lower().endswith(".zip"):
        name = os.path.basename(input_path).lower()
        m = TILE_NAME_RE.match(name)
        if not m:
            raise ValueError(f"Cannot parse tile code from {name}")
        return [(input_path, int(m.group(2)), int(m.group(3)), m.group(1))]

    results = []
    for zp in glob.glob(os.path.join(input_path, "**", "*.zip"), recursive=True):
        name = os.path.basename(zp).lower()
        m = TILE_NAME_RE.match(name)
        if m:
            results.append((zp, int(m.group(2)), int(m.group(3)), m.group(1)))
    if not results:
        raise ValueError(f"No OS Terrain 50 zip tiles found under {input_path}")
    return results


def find_tiles_by_pos(region_dir):
    """Return {(e_digit, n_digit): (zip_path, region_code)} for a region folder.

    Used by stitch.py to look up tiles by grid position when compositing a
    region-wide heightmap.
    """
    pattern = os.path.join(region_dir, "*.zip")
    tiles = {}
    for path in glob.glob(pattern):
        name = os.path.basename(path).lower()
        m = TILE_NAME_RE.match(name)
        if m:
            region_code = m.group(1)
            e_digit, n_digit = int(m.group(2)), int(m.group(3))
            tiles[(e_digit, n_digit)] = (path, region_code)
    return tiles
