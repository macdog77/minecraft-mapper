#!/usr/bin/env python3
"""
uk_map.py — Composite every OS raster TIFF into a single UK-wide map,
scaled to a target height, with the 100 km BNG grid and region letters
overlaid in red.

Usage:
    python uk_map.py [--height N] [--out PATH] [--no-grid] [--label-scale F]

Options:
    --height N          Output height in pixels (default 2000).
    --out PATH          Output PNG path (default ./UK_map.png).
    --no-grid           Skip the red grid and region labels.
    --label-scale F     Region-letter size as fraction of a 100 km square
                        (default 0.5).
"""

import argparse
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
from mesh import (_MAJOR_REVERSE, _MINOR_LETTERS, QUAD_PX, SEA_COLOUR, TILE_DIR)

SQUARE_M = 100_000   # 100 km BNG square
TILE_M   = 10_000    # 10 km elevation tile
QUAD_M   = 5_000     # 5 km TIFF quadrant

TIFF_RE = re.compile(r"^([A-Z]{2})(\d)(\d)(NE|NW|SE|SW)\.tif$", re.IGNORECASE)

QUAD_XY = {"SW": (0, 0), "SE": (QUAD_M, 0),
           "NW": (0, QUAD_M), "NE": (QUAD_M, QUAD_M)}

GRID_COLOUR  = (220, 30, 30)
LABEL_COLOUR = (220, 30, 30)

_MAJOR_FORWARD = {(c, r): k for k, (c, r) in _MAJOR_REVERSE.items()}


def region_origin(code):
    """SW corner (easting, northing) in metres of a 100 km BNG square."""
    mc, mr = _MAJOR_REVERSE[code[0].upper()]
    mi = _MINOR_LETTERS.index(code[1].upper())
    return mc * 500_000 + (mi % 5) * 100_000, mr * 500_000 + (4 - mi // 5) * 100_000


def square_code_at(e, n):
    """Return the 2-letter BNG code for the 100 km square containing (e, n), or None."""
    if not (0 <= e < 1_000_000 and 0 <= n < 1_500_000):
        return None
    major = _MAJOR_FORWARD.get((e // 500_000, n // 500_000))
    if major is None:
        return None
    col = (e % 500_000) // 100_000
    row_from_south = (n % 500_000) // 100_000
    return major + _MINOR_LETTERS[(4 - row_from_south) * 5 + col]


def discover_all_tiffs():
    """Yield (region, e, n, quadrant, path) for every TIFF under TILE_DIR."""
    if not os.path.isdir(TILE_DIR):
        raise FileNotFoundError(f"Tiles directory not found: {TILE_DIR}")
    for region in sorted(os.listdir(TILE_DIR)):
        rdir = os.path.join(TILE_DIR, region)
        if not os.path.isdir(rdir):
            continue
        for fname in sorted(os.listdir(rdir)):
            m = TIFF_RE.match(fname)
            if m:
                yield (m.group(1).upper(), int(m.group(2)), int(m.group(3)),
                       m.group(4).upper(), os.path.join(rdir, fname))


def find_font(size):
    for path in [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/Arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/Arial.ttf",
        "C:/Windows/Fonts/consolab.ttf",
    ]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def downsample_tiff(path, target_px):
    """
    Decode a 5000x5000 TIFF and return a target_px x target_px RGB image.
    Converts to RGB first (palette/indexed TIFFs are not reducible) then
    uses Image.reduce() to cheaply get near the target size before the
    final LANCZOS resize — an order of magnitude faster than a single
    LANCZOS on the native image.
    """
    img = Image.open(path).convert("RGB")
    if img.size != (QUAD_PX, QUAD_PX):
        return img.resize((target_px, target_px), Image.LANCZOS)
    factor = max(1, QUAD_PX // max(target_px * 4, 1))
    if factor > 1:
        img = img.reduce(factor)
    return img.resize((target_px, target_px), Image.LANCZOS)


def main():
    parser = argparse.ArgumentParser(
        description="UK-wide OS map composite with red BNG grid overlay.")
    parser.add_argument("--height", type=int, default=2000)
    parser.add_argument("--out",    default=os.path.join(os.getcwd(), "UK_map.png"))
    parser.add_argument("--no-grid",     action="store_true")
    parser.add_argument("--label-scale", type=float, default=0.5)
    args = parser.parse_args()

    tiffs = list(discover_all_tiffs())
    if not tiffs:
        print(f"No TIFFs found in {TILE_DIR}")
        sys.exit(1)
    regions_present = {t[0] for t in tiffs}
    print(f"Found {len(tiffs)} quadrant TIFFs across {len(regions_present)} regions.")

    # --- Extents (in metres) ---
    min_e = min_n =  10**9
    max_e = max_n = -10**9
    for reg, e_d, n_d, quad, _ in tiffs:
        re_e, re_n = region_origin(reg)
        qx, qy = QUAD_XY[quad]
        sw_e = re_e + e_d * TILE_M + qx
        sw_n = re_n + n_d * TILE_M + qy
        if sw_e < min_e: min_e = sw_e
        if sw_n < min_n: min_n = sw_n
        if sw_e + QUAD_M > max_e: max_e = sw_e + QUAD_M
        if sw_n + QUAD_M > max_n: max_n = sw_n + QUAD_M

    # Snap canvas to 100 km grid so region letters land in clean cells
    canvas_e0 = (min_e // SQUARE_M) * SQUARE_M
    canvas_e1 = -(-max_e // SQUARE_M) * SQUARE_M
    canvas_n0 = (min_n // SQUARE_M) * SQUARE_M
    canvas_n1 = -(-max_n // SQUARE_M) * SQUARE_M

    width_m  = canvas_e1 - canvas_e0
    height_m = canvas_n1 - canvas_n0
    scale    = args.height / height_m            # px per metre
    canvas_w = int(round(width_m * scale))
    canvas_h = args.height
    print(f"Extent: {width_m/1000:.0f} km W-E x {height_m/1000:.0f} km S-N "
          f"-> {canvas_w} x {canvas_h} px  ({scale * 1000:.3f} px/km)")

    canvas = Image.new("RGB", (canvas_w, canvas_h), SEA_COLOUR)

    quad_tgt_px = max(1, int(round(QUAD_M * scale)))
    print(f"Each 5 km quadrant -> {quad_tgt_px} px")

    # --- Composite every TIFF quadrant ---
    skipped = 0
    for reg, e_d, n_d, quad, path in tqdm(tiffs, desc="Compositing", unit="tif"):
        re_e, re_n = region_origin(reg)
        qx, qy = QUAD_XY[quad]
        sw_e = re_e + e_d * TILE_M + qx
        sw_n = re_n + n_d * TILE_M + qy
        px_x = int(round((sw_e - canvas_e0) * scale))
        px_y = int(round((canvas_n1 - (sw_n + QUAD_M)) * scale))
        try:
            tile_img = downsample_tiff(path, quad_tgt_px)
        except Exception as ex:
            skipped += 1
            tqdm.write(f"  skipped {os.path.basename(path)}: {ex}")
            continue
        canvas.paste(tile_img, (px_x, px_y))
    if skipped:
        print(f"Skipped {skipped} unreadable tiff(s).")

    # --- Red grid + region letters ---
    if not args.no_grid:
        draw = ImageDraw.Draw(canvas)
        square_px = SQUARE_M * scale
        grid_w = max(2, int(round(square_px * 0.015)))
        cols = int(round(width_m / SQUARE_M))
        rows = int(round(height_m / SQUARE_M))

        for c in range(cols + 1):
            x = int(round(c * square_px))
            draw.line([(x, 0), (x, canvas_h - 1)],
                      fill=GRID_COLOUR, width=grid_w)
        for r in range(rows + 1):
            y = int(round(r * square_px))
            draw.line([(0, y), (canvas_w - 1, y)],
                      fill=GRID_COLOUR, width=grid_w)

        font_size = max(10, int(round(square_px * args.label_scale)))
        font = find_font(font_size)
        stroke_w = max(2, font_size // 14)

        for r in range(rows):
            for c in range(cols):
                sq_e = canvas_e0 + c * SQUARE_M
                sq_n = canvas_n0 + r * SQUARE_M
                code = square_code_at(sq_e, sq_n)
                if code is None:
                    continue
                cx = (sq_e + SQUARE_M / 2 - canvas_e0) * scale
                cy = (canvas_n1 - (sq_n + SQUARE_M / 2)) * scale
                try:
                    bbox = draw.textbbox((0, 0), code, font=font,
                                         stroke_width=stroke_w)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    tx, ty = cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]
                except Exception:
                    tw = font_size; th = font_size
                    tx, ty = cx - tw / 2, cy - th / 2
                draw.text((tx, ty), code, fill=LABEL_COLOUR, font=font,
                          stroke_width=stroke_w, stroke_fill=(255, 255, 255))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    canvas.save(args.out)
    print(f"Saved: {args.out}  ({canvas_w} x {canvas_h})")


if __name__ == "__main__":
    main()
