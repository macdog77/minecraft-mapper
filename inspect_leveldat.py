#!/usr/bin/env python3
"""
inspect_leveldat.py — Dump the fields of a Minecraft level.dat that are relevant
to the "Experimental Settings" warning.

Usage:
    python inspect_leveldat.py <path-to-world-folder-or-level.dat>
"""

import os
import sys
import amulet_nbt


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    path = sys.argv[1]
    if os.path.isdir(path):
        path = os.path.join(path, "level.dat")
    if not os.path.isfile(path):
        print(f"Not found: {path}")
        sys.exit(1)

    nbt = amulet_nbt.load(path)
    data = nbt.tag["Data"]

    print(f"=== {path} ===\n")
    print("Top-level Data keys:")
    for k in sorted(data.keys()):
        print(f"  {k}")
    print()

    for key in ("DataPacks", "enabled_features", "removed_features",
                "experiments", "WasModded", "Version"):
        if key in data:
            print(f"[{key}]")
            print(f"  {data[key]}")
            print()

    wgs = data.get("WorldGenSettings")
    if wgs is not None:
        print("[WorldGenSettings] top-level keys:")
        for k in sorted(wgs.keys()):
            print(f"  {k}")
        print()
        dims = wgs.get("dimensions")
        if dims is not None:
            for dname in dims.keys():
                dim = dims[dname]
                t = dim.get("type") if hasattr(dim, "get") else None
                print(f"  dimension {dname}: type tag class = "
                      f"{type(t).__name__ if t is not None else 'MISSING'}, "
                      f"value = {t}")


if __name__ == "__main__":
    main()
