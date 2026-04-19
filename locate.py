#!/usr/bin/env python3
"""
locate.py — Convert a WGS84 geocoordinate to a British National Grid tile reference.

Usage:
    python locate.py <lat> <lon>
    python locate.py 56.498406 -3.806473

Output:
    BNG easting/northing, 100 km square, 10 km tile code, and the zip file path
    within the OS Map Data folder (if it exists).
"""

import sys
import os
from pyproj import Transformer

# WGS84 (lat/lon) → British National Grid (EPSG:27700)
_TRANSFORMER = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)

# BNG 500 km major squares, indexed by (col, row) where col = E//500000, row = N//500000
_MAJOR = {
    (0, 0): "S", (1, 0): "T",
    (0, 1): "N", (1, 1): "O",
    (0, 2): "H",
}

# Second letter: 5×5 grid within a major square, row 4=north, row 0=south; no letter I
_MINOR_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"

DATA_ROOT = os.path.join(os.path.dirname(__file__), "OS Map Data", "data")


def wgs84_to_bng(lat, lon):
    """Return (easting, northing) in EPSG:27700."""
    easting, northing = _TRANSFORMER.transform(lon, lat)
    return easting, northing


def bng_to_tile(easting, northing):
    """
    Convert BNG easting/northing to:
      major_letter  – the 500 km square letter (S, T, N, O, H)
      minor_letter  – the 100 km sub-square letter (A-Z, no I)
      e_digit       – 10 km easting digit within the 100 km square (0-9)
      n_digit       – 10 km northing digit within the 100 km square (0-9)
    Returns (grid_ref_100km, e_digit, n_digit) e.g. ("NT", 2, 7)
    """
    if not (0 <= easting < 1_000_000 and 0 <= northing < 1_500_000):
        raise ValueError(
            f"Coordinate ({easting:.0f}E, {northing:.0f}N) is outside the BNG coverage area."
        )

    major_col = int(easting)  // 500_000
    major_row = int(northing) // 500_000
    major = _MAJOR.get((major_col, major_row))
    if major is None:
        raise ValueError(f"No major BNG square for ({easting:.0f}E, {northing:.0f}N).")

    # Position within the 500 km major square
    e_in_major = easting  % 500_000
    n_in_major = northing % 500_000

    minor_col = int(e_in_major) // 100_000   # 0–4 (W→E)
    minor_row = int(n_in_major) // 100_000   # 0–4 (S→N)
    minor_idx = (4 - minor_row) * 5 + minor_col
    minor = _MINOR_LETTERS[minor_idx]

    # Position within the 100 km square
    e_in_100 = e_in_major % 100_000
    n_in_100 = n_in_major % 100_000

    e_digit = int(e_in_100) // 10_000   # 0–9
    n_digit = int(n_in_100) // 10_000   # 0–9

    return major + minor, e_digit, n_digit


def find_zip(square, e_digit, n_digit):
    """Return path to the OS tile zip if it exists in the data folder, else None."""
    code = square.lower()
    pattern = os.path.join(DATA_ROOT, code, f"{code}{e_digit}{n_digit}_*.zip")
    import glob
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    try:
        lat = float(sys.argv[1])
        lon = float(sys.argv[2])
    except ValueError:
        print("Error: lat and lon must be numbers.")
        sys.exit(1)

    easting, northing = wgs84_to_bng(lat, lon)
    square, e_digit, n_digit = bng_to_tile(easting, northing)
    tile_code = f"{square}{e_digit}{n_digit}"
    zip_path  = find_zip(square, e_digit, n_digit)

    print(f"Coordinates:  {lat}, {lon}")
    print(f"BNG:          {easting:,.0f} E  {northing:,.0f} N")
    print(f"100 km square:{square}")
    print(f"10 km tile:   {tile_code}")
    if zip_path:
        rel = os.path.relpath(zip_path, os.path.dirname(__file__))
        print(f"Data file:    {rel}")
    else:
        print(f"Data file:    not found (tile {tile_code} may not be in the dataset)")


if __name__ == "__main__":
    main()
