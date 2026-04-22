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

import os
import sys

from minecraft_uk.common.paths import REPO_ROOT
from minecraft_uk.osdata.bng import bng_to_tile, find_tile_zip, wgs84_to_bng


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
    zip_path  = find_tile_zip(square, e_digit, n_digit)

    print(f"Coordinates:  {lat}, {lon}")
    print(f"BNG:          {easting:,.0f} E  {northing:,.0f} N")
    print(f"100 km square:{square}")
    print(f"10 km tile:   {tile_code}")
    if zip_path:
        rel = os.path.relpath(zip_path, REPO_ROOT)
        print(f"Data file:    {rel}")
    else:
        print(f"Data file:    not found (tile {tile_code} may not be in the dataset)")


if __name__ == "__main__":
    main()
