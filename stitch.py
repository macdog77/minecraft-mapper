#!/usr/bin/env python3
"""
stitch.py — Stitch all tiles in a region folder into one heightmap image.

Usage:
    python stitch.py <region_dir>                # e.g. "OS Map Data/data/nn"
    python stitch.py <region_dir> [max_elev]     # override white point (default 1345 m)
    python stitch.py <region_dir> [max_elev] --tiff   # overlay OS map tiles at 33%
    python stitch.py <region_dir> [max_elev] --grid   # overlay tile grid with labels

Tile naming convention: <XX><E><N>_*.zip where E and N are single easting/northing
digits (0-9) within the 100 km square. Output is saved as <REGION>_heightmap.png
in the current directory.

Layering order: heightmap -> TIFF map at 33% (if --tiff) -> grid (if --grid).

Scale: black = 0 m (sea/NODATA), white = max_elev m (Ben Nevis default).
North is up in the output image.
"""

import sys
import os
import glob
import re
import zipfile
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(__file__))
from minecraft_uk.osdata.asc import parse_asc
from minecraft_uk.osdata.tiff import (
    QUADRANTS,
    SEA_COLOUR,
    TILE_PX as TIFF_TILE_PX,
    find_tiffs,
)
from minecraft_uk.rendering.heightmap import make_heightmap

TILE_PX = 200          # cells per tile side (1 px per 50 m cell)
BEN_NEVIS_M = 1345.0
TIFF_OPACITY = 0.75

GRID_LINE_COLOUR = (220, 30, 30)      # red
GRID_LINE_WIDTH  = 2
LABEL_COLOUR     = (220, 30, 30)
LABEL_PADDING    = 4                  # px from top-left corner of tile


def find_tiles(region_dir):
    """Return a dict of (easting_digit, northing_digit) -> zip_path for all tiles."""
    pattern = os.path.join(region_dir, "*.zip")
    tiles = {}
    for path in glob.glob(pattern):
        name = os.path.basename(path).lower()
        m = re.match(r"([a-z]{2})(\d)(\d)_", name)
        if m:
            region_code = m.group(1)
            e_digit, n_digit = int(m.group(2)), int(m.group(3))
            tiles[(e_digit, n_digit)] = (path, region_code)
    return tiles


def stitch(region_dir, max_elev=BEN_NEVIS_M):
    tiles = find_tiles(region_dir)
    if not tiles:
        raise ValueError(f"No tile zips found in {region_dir}")

    e_digits = sorted(set(k[0] for k in tiles))
    n_digits = sorted(set(k[1] for k in tiles))
    cols = max(e_digits) - min(e_digits) + 1
    rows = max(n_digits) - min(n_digits) + 1
    e_min, n_min = min(e_digits), min(n_digits)
    n_max = max(n_digits)

    img_w = cols * TILE_PX
    img_h = rows * TILE_PX
    canvas = Image.new("L", (img_w, img_h), color=0)

    total = len(tiles)
    for i, ((e, n), (path, _)) in enumerate(sorted(tiles.items()), 1):
        print(f"  [{i:3d}/{total}] {os.path.basename(path)}", end="\r", flush=True)
        try:
            with zipfile.ZipFile(path) as zf:
                asc_names = [name for name in zf.namelist()
                             if name.lower().endswith(".asc")
                             and not name.lower().endswith(".asc.aux.xml")]
                if not asc_names:
                    continue
                with zf.open(asc_names[0]) as f:
                    header, row_data = parse_asc(f)

            tile_img = make_heightmap(row_data, header, max_elev)

            col = e - e_min
            row = n_max - n
            canvas.paste(tile_img, (col * TILE_PX, row * TILE_PX))

        except Exception as ex:
            print(f"\n  Warning: skipped {os.path.basename(path)}: {ex}")

    print()
    return canvas, tiles, e_min, n_max


def stitch_tiff_layer(tiles, e_min, n_max, img_w, img_h):
    """
    Build an RGB canvas of OS raster TIFF map tiles aligned to the heightmap canvas.
    Missing tiles (and missing quadrants within a tile) are filled with SEA_COLOUR.
    """
    canvas = Image.new("RGB", (img_w, img_h), SEA_COLOUR)
    total = len(tiles)
    placed = 0

    for i, ((e, n), (_, region_code)) in enumerate(sorted(tiles.items()), 1):
        print(f"  [{i:3d}/{total}] TIFF {region_code.upper()}{e}{n}",
              end="\r", flush=True)
        tiffs = find_tiffs(region_code, e, n)
        if not tiffs:
            continue

        tile_img = Image.new("RGB", (TIFF_TILE_PX, TIFF_TILE_PX), SEA_COLOUR)
        for q, qpath in tiffs.items():
            try:
                tile_img.paste(Image.open(qpath).convert("RGB"), QUADRANTS[q])
            except Exception:
                pass

        tile_img = tile_img.resize((TILE_PX, TILE_PX), Image.LANCZOS)
        col = e - e_min
        row = n_max - n
        canvas.paste(tile_img, (col * TILE_PX, row * TILE_PX))
        placed += 1

    print()
    print(f"TIFF tiles placed: {placed}/{total}")
    return canvas


def overlay_grid(canvas, tiles, e_min, n_max):
    """
    Convert canvas to RGB and draw a red grid with tile labels (e.g. NN30).
    Returns the annotated RGB image.
    """
    rgb = canvas.convert("RGB")
    draw = ImageDraw.Draw(rgb)

    e_digits = sorted(set(k[0] for k in tiles))
    n_digits = sorted(set(k[1] for k in tiles))
    cols = max(e_digits) - min(e_digits) + 1
    rows = max(n_digits) - min(n_digits) + 1
    img_w = cols * TILE_PX
    img_h = rows * TILE_PX

    # Try to load a small bitmap font; fall back to default if unavailable
    font = None
    font_size = 16
    for font_path in [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/Arial.ttf",
    ]:
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, font_size)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    # Draw grid lines along tile boundaries
    for col in range(cols + 1):
        x = col * TILE_PX
        draw.line([(x, 0), (x, img_h - 1)], fill=GRID_LINE_COLOUR, width=GRID_LINE_WIDTH)
    for row in range(rows + 1):
        y = row * TILE_PX
        draw.line([(0, y), (img_w - 1, y)], fill=GRID_LINE_COLOUR, width=GRID_LINE_WIDTH)

    # Label each tile that actually exists with its code (e.g. NN30)
    for (e, n), (_, region_code) in tiles.items():
        col = e - e_min
        row = n_max - n
        label = f"{region_code.upper()}{e}{n}"
        x = col * TILE_PX + LABEL_PADDING
        y = row * TILE_PX + LABEL_PADDING
        # Thin drop-shadow for legibility over bright areas
        draw.text((x + 1, y + 1), label, fill=(0, 0, 0), font=font)
        draw.text((x, y),         label, fill=LABEL_COLOUR, font=font)

    return rgb


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print(__doc__)
        sys.exit(1)

    region_dir = args[0]
    max_elev   = float(args[1]) if len(args) > 1 else BEN_NEVIS_M
    draw_grid  = "--grid" in flags
    draw_tiff  = "--tiff" in flags

    region_name = os.path.basename(os.path.normpath(region_dir)).upper()
    print(f"Stitching region {region_name} from {region_dir}")
    print(f"Scale: 0 m -> black, {max_elev} m -> white")
    if draw_tiff:
        print(f"TIFF overlay: enabled ({int(TIFF_OPACITY * 100)}% opacity)")
    if draw_grid:
        print("Grid overlay: enabled")

    tiles_raw = find_tiles(region_dir)
    e_digits  = sorted(set(k[0] for k in tiles_raw))
    n_digits  = sorted(set(k[1] for k in tiles_raw))
    print(f"Found {len(tiles_raw)} tiles "
          f"(E{min(e_digits)}-{max(e_digits)}, N{min(n_digits)}-{max(n_digits)}) "
          f"-> {(max(e_digits)-min(e_digits)+1)*TILE_PX} x "
          f"{(max(n_digits)-min(n_digits)+1)*TILE_PX} px")

    canvas, tiles, e_min, n_max = stitch(region_dir, max_elev)

    out_img = canvas
    suffix_parts = []

    if draw_tiff:
        img_w, img_h = canvas.size
        tiff_canvas = stitch_tiff_layer(tiles, e_min, n_max, img_w, img_h)
        out_img = Image.blend(out_img.convert("RGB"), tiff_canvas, TIFF_OPACITY)
        suffix_parts.append("tiff")

    if draw_grid:
        out_img = overlay_grid(out_img, tiles, e_min, n_max)
        suffix_parts.append("grid")

    suffix = ("_" + "_".join(suffix_parts)) if suffix_parts else ""
    out_path = os.path.join(os.getcwd(), f"{region_name}_heightmap{suffix}.png")
    out_img.save(out_path)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
