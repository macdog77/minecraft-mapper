"""Greyscale-heightmap rendering with optional OS raster overlay."""

import os
import re

from PIL import Image

from ..osdata.tiff import (
    QUADRANTS,
    SEA_COLOUR,
    TILE_PX as TIFF_TILE_PX,
    find_tiffs,
)


BEN_NEVIS_M = 1345.0    # default white point
TIFF_OPACITY = 0.50


def make_heightmap(rows, header, max_elev=BEN_NEVIS_M):
    """Render a greyscale elevation image from parsed ASC rows + header.

    Black = sea level / NODATA, white = max_elev. ASC rows are stored N→S so
    no flip is needed for a north-up image.
    """
    nodata = header.get("nodata_value", -9999)
    height = len(rows)
    width = max(len(r) for r in rows)

    pixels = []
    for row in rows:
        for val in row:
            if val <= 0 or val == nodata:
                pixels.append(0)
            else:
                brightness = min(val / max_elev, 1.0)
                pixels.append(int(brightness * 255))
        for _ in range(width - len(row)):
            pixels.append(0)

    img = Image.new("L", (width, height))
    img.putdata(pixels)
    return img


def overlay_tiff(canvas, input_path, opacity=TIFF_OPACITY):
    """Blend OS raster TIFF quadrants for a single tile over the canvas.

    If the tile code can't be parsed from `input_path` or no TIFFs are found,
    returns the canvas converted to RGB unchanged.
    """
    name = os.path.basename(input_path).lower()
    m = re.match(r"([a-z]{2})(\d)(\d)", name)
    if not m:
        print(f"  TIFF overlay: cannot parse tile code from "
              f"{os.path.basename(input_path)}; skipping.")
        return canvas.convert("RGB")
    region_code = m.group(1)
    e_d, n_d = int(m.group(2)), int(m.group(3))

    tiffs = find_tiffs(region_code, e_d, n_d)
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
