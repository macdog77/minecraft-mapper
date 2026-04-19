#!/usr/bin/env python3
"""
sample_palette.py — Dump the palette of an OS raster TIFF and show pixel
usage per palette index. Useful for identifying which palette indices
correspond to features like buildings (light orange), roads, water, etc.

Usage:
    python sample_palette.py <tif_path>                # full palette + histogram
    python sample_palette.py <tif_path> --only-used    # only indices actually used
    python sample_palette.py <tif_path> --filter r>200,g<200,b<180
                                                       # show indices matching RGB test

Example:
    python sample_palette.py "OS Map Data/tiles/NT/nt27NW.tif" --only-used
"""

import sys
import argparse
import numpy as np
from PIL import Image


def parse_filter(expr):
    """Parse a simple filter like 'r>200,g<200,b<180' into a predicate(r,g,b)."""
    tests = []
    for clause in expr.split(","):
        clause = clause.strip()
        if not clause:
            continue
        for op in (">=", "<=", ">", "<", "=="):
            if op in clause:
                ch, val = clause.split(op, 1)
                ch = ch.strip().lower()
                val = int(val.strip())
                if ch not in "rgb":
                    raise ValueError(f"channel must be r/g/b in '{clause}'")
                tests.append((ch, op, val))
                break
        else:
            raise ValueError(f"no comparison operator in '{clause}'")

    def predicate(r, g, b):
        vals = {"r": r, "g": g, "b": b}
        for ch, op, val in tests:
            v = vals[ch]
            if op == ">"  and not v >  val: return False
            if op == "<"  and not v <  val: return False
            if op == ">=" and not v >= val: return False
            if op == "<=" and not v <= val: return False
            if op == "==" and not v == val: return False
        return True

    return predicate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tif")
    parser.add_argument("--only-used", action="store_true",
                        help="Only show indices that appear in the image.")
    parser.add_argument("--filter", default=None, metavar="EXPR",
                        help="Only show indices whose RGB matches a filter, "
                             "e.g. 'r>200,g>150,g<210,b<180'.")
    args = parser.parse_args()

    im = Image.open(args.tif)
    if im.mode != "P":
        print(f"Warning: image mode is {im.mode!r}, not 'P' (palette). "
              "Results may not be meaningful.")

    pal = im.getpalette() or []
    arr = np.asarray(im, dtype=np.uint8)
    total_px = arr.size
    counts = np.bincount(arr.ravel(), minlength=256)

    pred = parse_filter(args.filter) if args.filter else None

    print(f"Image:  {args.tif}")
    print(f"Size:   {im.size[0]} x {im.size[1]}  ({total_px:,} pixels)")
    print(f"Palette entries: {len(pal) // 3}")
    print()
    print(f"{'idx':>4}  {'R':>3} {'G':>3} {'B':>3}  {'count':>10}  {'pct':>6}  swatch")
    print("-" * 60)

    shown = 0
    for idx in range(min(256, len(pal) // 3)):
        r, g, b = pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]
        count = int(counts[idx])
        if args.only_used and count == 0:
            continue
        if pred is not None and not pred(r, g, b):
            continue
        pct = 100.0 * count / total_px
        # ANSI 24-bit colour swatch
        swatch = f"\033[48;2;{r};{g};{b}m    \033[0m"
        print(f"{idx:>4}  {r:>3} {g:>3} {b:>3}  {count:>10,}  {pct:>5.2f}%  {swatch}")
        shown += 1

    if shown == 0:
        print("(no matching palette entries)")


if __name__ == "__main__":
    main()
