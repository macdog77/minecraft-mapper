#!/usr/bin/env python3
"""
mesh.py — Generate a textured 3D mesh from OS Terrain 50 height data and OS map tiles.

Usage:
    python mesh.py <input> [options]

    input   Single tile .zip, a region folder (e.g. "OS Map Data/data/nn"),
            or the top-level data folder (generates the whole UK).

Options:
    --vscale F      Vertical exaggeration (default 1.0 = true proportions).
    --step N        Use every Nth elevation cell (default 1 = full detail).
                    Step 2 halves vertex count per axis (4x fewer triangles).
    --texture N     Max texture dimension in pixels (default 8192).
    --out PATH      Output directory (default ./meshes/<NAME>).

Output is an OBJ file + MTL + texture PNG. Open in Blender, MeshLab, or any 3D viewer.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from minecraft_uk.common.paths import MESHES_DIR
from minecraft_uk.osdata.discovery import discover_zips
from minecraft_uk.osdata.tiles import (
    build_tile_index,
    compute_global_extent,
    scan_headers,
)
from minecraft_uk.rendering.mesh import stitch_texture, stream_obj, write_mtl


def main():
    parser = argparse.ArgumentParser(
        description="Generate a textured 3D mesh from OS Terrain 50 data.")
    parser.add_argument("input",          help="Tile .zip, region folder, or data root folder")
    parser.add_argument("--vscale",  type=float, default=1.0,
                        help="Vertical exaggeration (default 1.0 = true proportions)")
    parser.add_argument("--step",    type=int,   default=1,
                        help="Use every Nth cell (default 1 = full detail)")
    parser.add_argument("--texture", type=int,   default=8192,
                        help="Max texture dimension in pixels (default 8192)")
    parser.add_argument("--out",     default=None,
                        help="Output directory (default ./meshes/<NAME>)")
    args = parser.parse_args()

    zip_entries = discover_zips(args.input)
    print(f"Found {len(zip_entries)} tiles.")

    raw = os.path.basename(os.path.normpath(args.input))
    m = re.match(r"([a-z]{2}\d{2})_", raw.lower())
    short = m.group(1).upper() if m else re.sub(r"[^a-zA-Z0-9]", "", raw)[:8].upper()
    name = f"OS_{short}"
    out_dir = args.out or os.path.join(MESHES_DIR, name)
    os.makedirs(out_dir, exist_ok=True)

    headers = scan_headers(zip_entries)
    origin_e, origin_n_top, total_rows, total_cols = compute_global_extent(headers)
    tile_index = build_tile_index(headers, origin_e, origin_n_top)
    print(f"Elevation grid: {total_cols} x {total_rows} cells")

    texture = stitch_texture(zip_entries, total_cols, total_rows,
                             origin_e, origin_n_top, args.texture)
    tex_file = f"{name}_texture.png"
    texture.save(os.path.join(out_dir, tex_file))
    print(f"Texture saved: {texture.size[0]}x{texture.size[1]} px")

    print(f"\nStreaming mesh (step={args.step}, vscale={args.vscale})...")
    mtl_path = write_mtl(out_dir, name, tex_file)
    n_verts, n_faces = stream_obj(
        out_dir, name, headers, tile_index, origin_e, origin_n_top,
        total_rows, total_cols, args.step, args.vscale, tex_file)
    print(f"Saved: {mtl_path}")

    print(f"\nDone!")
    print(f"  Output:    {out_dir}/")
    print(f"  Mesh:      {n_verts:,} vertices, {n_faces:,} triangles")
    print(f"  Texture:   {texture.size[0]}x{texture.size[1]} px")
    print(f"\nOpen {name}.obj in Blender, MeshLab, or any 3D viewer.")


if __name__ == "__main__":
    main()
