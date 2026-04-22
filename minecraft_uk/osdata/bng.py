"""British National Grid (EPSG:27700) conversions and tile-code helpers.

Single source of truth for the BNG letter tables; previously these were
duplicated between `locate.py` and `mesh.py`.
"""

import glob
import os

from pyproj import Transformer

from ..common.paths import DATA_ROOT


# WGS84 (lat/lon) → British National Grid (EPSG:27700)
_WGS84_TO_BNG = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)

# BNG 500 km major squares, keyed by (col, row) where col = E // 500000,
# row = N // 500000.
_MAJOR = {
    (0, 0): "S", (1, 0): "T",
    (0, 1): "N", (1, 1): "O",
    (0, 2): "H",
}
_MAJOR_REVERSE = {v: k for k, v in _MAJOR.items()}

# Second letter: 5×5 grid within a major square, row 4=north, row 0=south; no I.
MINOR_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


def wgs84_to_bng(lat, lon):
    """Return (easting, northing) in EPSG:27700."""
    easting, northing = _WGS84_TO_BNG.transform(lon, lat)
    return easting, northing


def bng_to_tile(easting, northing):
    """Convert BNG easting/northing to a 100 km grid ref and 10 km digits.

    Returns (grid_ref_100km, e_digit, n_digit), e.g. ("NT", 2, 7).
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

    e_in_major = easting  % 500_000
    n_in_major = northing % 500_000

    minor_col = int(e_in_major) // 100_000
    minor_row = int(n_in_major) // 100_000
    minor_idx = (4 - minor_row) * 5 + minor_col
    minor = MINOR_LETTERS[minor_idx]

    e_in_100 = e_in_major % 100_000
    n_in_100 = n_in_major % 100_000

    e_digit = int(e_in_100) // 10_000
    n_digit = int(n_in_100) // 10_000

    return major + minor, e_digit, n_digit


def region_origin(code):
    """SW corner (easting, northing) in metres of a 100 km BNG square."""
    mc, mr = _MAJOR_REVERSE[code[0].upper()]
    mi = MINOR_LETTERS.index(code[1].upper())
    return mc * 500_000 + (mi % 5) * 100_000, mr * 500_000 + (4 - mi // 5) * 100_000


def square_code_at(easting, northing):
    """Return the 2-letter BNG code for the 100 km square containing a point, or None."""
    if not (0 <= easting < 1_000_000 and 0 <= northing < 1_500_000):
        return None
    major = _MAJOR.get((easting // 500_000, northing // 500_000))
    if major is None:
        return None
    col = (easting % 500_000) // 100_000
    row_from_south = (northing % 500_000) // 100_000
    return major + MINOR_LETTERS[(4 - row_from_south) * 5 + col]


def find_tile_zip(square, e_digit, n_digit, data_root=DATA_ROOT):
    """Return the OS Terrain 50 tile zip for the given grid ref, or None."""
    code = square.lower()
    pattern = os.path.join(data_root, code, f"{code}{e_digit}{n_digit}_*.zip")
    matches = glob.glob(pattern)
    return matches[0] if matches else None
