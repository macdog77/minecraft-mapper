#!/usr/bin/env python3
"""
building_mask_preview.py — Render a building-detection mask from an OS raster
TIFF so you can tune the colour filter without regenerating a world.

By default the output is a pixel-exact mask: pixels whose palette RGB matches
the filter become black, everything else is white. A separate cell-aggregate
mode groups pixels into 50 m OS cells and marks whole cells black when the
building fraction exceeds --cell-threshold — that mirrors what generate.py
does.

Usage:
    python building_mask_preview.py <tif> [options]

Examples:
    # Default (pixel mask, reuses generate._is_building_color):
    python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif"

    # Cell-aggregate view (matches generate.py's TIFF_BUILDING_THRESHOLD):
    python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif" --cells

    # Loosen the filter around the #f8d8b8 target to catch softer peaches:
    python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif" \
        --r 248,15 --g 216,15 --b 184,20

    # Side-by-side overlay with the source image (mask at 50% opacity in red):
    python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif" --overlay
"""

import argparse
import os
import sys
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate import (
    _is_building_color,
    CELL_SIZE_M,
    TIFF_PX_PER_M,
    TIFF_BUILDING_THRESHOLD,
    BUILDING_SUBCELL_M,
)


def parse_channel(spec, default_tol):
    """'target,tol' → (target, tol) ints."""
    if spec is None:
        return None
    parts = spec.split(",")
    target = int(parts[0])
    tol = int(parts[1]) if len(parts) > 1 else default_tol
    return target, tol


def build_predicate(r_spec, g_spec, b_spec, require_rgb_order):
    """Return a predicate(r,g,b). If all three specs are None, fall back to
    generate._is_building_color so this script and generate.py stay in sync."""
    if r_spec is None and g_spec is None and b_spec is None:
        return _is_building_color

    r_t, r_tol = r_spec or (248, 8)
    g_t, g_tol = g_spec or (216, 8)
    b_t, b_tol = b_spec or (184, 8)

    def predicate(r, g, b):
        if abs(r - r_t) > r_tol: return False
        if abs(g - g_t) > g_tol: return False
        if abs(b - b_t) > b_tol: return False
        if require_rgb_order and not (r > g > b): return False
        return True

    return predicate


def pixel_mask(im, predicate):
    """Return a bool array (H, W) where True = pixel matches predicate."""
    if im.mode != "P":
        print(f"Warning: image mode is {im.mode!r}, not 'P'. Converting via RGB.")
        rgb = np.asarray(im.convert("RGB"))
        r = rgb[..., 0]; g = rgb[..., 1]; b = rgb[..., 2]
        mask = np.zeros(r.shape, dtype=bool)
        uniq = set(map(tuple, rgb.reshape(-1, 3)))
        hits = {c for c in uniq if predicate(*c)}
        if not hits:
            return mask
        # Vectorised: build a boolean map per unique hit.
        for (hr, hg, hb) in hits:
            mask |= (r == hr) & (g == hg) & (b == hb)
        return mask

    pal = im.getpalette() or []
    arr = np.asarray(im, dtype=np.uint8)
    lut = np.zeros(256, dtype=bool)
    for idx in range(min(256, len(pal) // 3)):
        if predicate(pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]):
            lut[idx] = True
    return lut[arr]


def cell_mask(pixel_bool, cell_px, threshold):
    """Downsample a pixel mask to a cell grid and threshold it. Returns the
    cell-resolution bool mask plus the upsampled-to-pixel preview mask."""
    h, w = pixel_bool.shape
    ch, cw = h // cell_px, w // cell_px
    trimmed = pixel_bool[:ch * cell_px, :cw * cell_px]
    frac = trimmed.reshape(ch, cell_px, cw, cell_px).mean(axis=(1, 3))
    cell_bool = frac > threshold
    upsample = np.kron(cell_bool, np.ones((cell_px, cell_px), dtype=bool))
    # Pad back up to the original image size with False.
    full = np.zeros((h, w), dtype=bool)
    full[:upsample.shape[0], :upsample.shape[1]] = upsample
    return cell_bool, full


def save_mask_png(mask, path):
    """Black where True, white elsewhere."""
    img = np.where(mask, 0, 255).astype(np.uint8)
    Image.fromarray(img, mode="L").save(path)


def save_overlay_png(im, mask, path, alpha=0.5, colour=(255, 0, 0)):
    base = im.convert("RGB")
    arr = np.asarray(base).copy()
    r, g, b = colour
    a = alpha
    arr[mask, 0] = (a * r + (1 - a) * arr[mask, 0]).astype(np.uint8)
    arr[mask, 1] = (a * g + (1 - a) * arr[mask, 1]).astype(np.uint8)
    arr[mask, 2] = (a * b + (1 - a) * arr[mask, 2]).astype(np.uint8)
    Image.fromarray(arr, mode="RGB").save(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tif")
    parser.add_argument("--out", default=None,
                        help="Output PNG path (default: <tif stem>_buildings.png in cwd).")
    parser.add_argument("--cells", action="store_true",
                        help=f"Aggregate into {BUILDING_SUBCELL_M} m sub-cells and threshold "
                             "at --cell-threshold (mirrors what generate.py --buildings places).")
    parser.add_argument("--cell-threshold", type=float, default=TIFF_BUILDING_THRESHOLD,
                        help=f"Sub-cell fraction threshold (default {TIFF_BUILDING_THRESHOLD}).")
    parser.add_argument("--overlay", action="store_true",
                        help="Also write <stem>_overlay.png with the source image "
                             "tinted red where the mask is True.")
    parser.add_argument("--r", default=None, metavar="T[,TOL]",
                        help="Override red channel target (default uses generate._is_building_color).")
    parser.add_argument("--g", default=None, metavar="T[,TOL]",
                        help="Override green channel target.")
    parser.add_argument("--b", default=None, metavar="T[,TOL]",
                        help="Override blue channel target.")
    parser.add_argument("--no-rgb-order", action="store_true",
                        help="Drop the r > g > b requirement when using --r/--g/--b overrides.")
    args = parser.parse_args()

    predicate = build_predicate(
        parse_channel(args.r, 10),
        parse_channel(args.g, 20),
        parse_channel(args.b, 25),
        require_rgb_order=not args.no_rgb_order,
    )

    im = Image.open(args.tif)
    print(f"Image: {args.tif}  ({im.size[0]} x {im.size[1]}, mode={im.mode})")

    pixels = pixel_mask(im, predicate)
    n_hit = int(pixels.sum())
    pct = 100.0 * n_hit / pixels.size
    print(f"Pixel hits: {n_hit:,} / {pixels.size:,}  ({pct:.3f}%)")

    if args.cells:
        cell_px = BUILDING_SUBCELL_M * TIFF_PX_PER_M
        cell_bool, mask_img = cell_mask(pixels, cell_px, args.cell_threshold)
        n_cells = int(cell_bool.sum())
        total_cells = cell_bool.size
        print(f"Sub-cell hits ({BUILDING_SUBCELL_M} m, threshold {args.cell_threshold}): "
              f"{n_cells:,} / {total_cells:,}  ({100 * n_cells / total_cells:.2f}%)")
    else:
        mask_img = pixels

    stem = os.path.splitext(os.path.basename(args.tif))[0]
    out = args.out or f"{stem}_buildings.png"
    save_mask_png(mask_img, out)
    print(f"Wrote mask: {out}")

    if args.overlay:
        overlay_path = os.path.splitext(out)[0] + "_overlay.png"
        save_overlay_png(im, mask_img, overlay_path)
        print(f"Wrote overlay: {overlay_path}")


if __name__ == "__main__":
    main()
