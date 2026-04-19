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

Output is an OBJ file + MTL + texture PNG.  Open in Blender, MeshLab, or any 3D viewer.
"""

import argparse
import os
import re
import sys

import numpy as np
from PIL import Image
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
from generate import discover_zips, load_elevation_grid, CELL_SIZE_M

# ---------------------------------------------------------------------------
# BNG code → easting/northing lookup (reverse of locate.py's tables)
# ---------------------------------------------------------------------------

_MAJOR_REVERSE = {"S": (0, 0), "T": (1, 0), "N": (0, 1), "O": (1, 1), "H": (0, 2)}
_MINOR_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"   # no I

TILE_DIR   = os.path.join(os.path.dirname(__file__), "OS Map Data", "tiles")
QUAD_PX    = 5000
TILE_PX    = QUAD_PX * 2      # 10 000 px per full tile
TILE_SIZE_M = 10_000           # 10 km
QUADRANTS  = {"NW": (0, 0), "NE": (QUAD_PX, 0),
              "SW": (0, QUAD_PX), "SE": (QUAD_PX, QUAD_PX)}


def _region_origin(code):
    """Return (easting, northing) of the SW corner of a two-letter BNG 100 km square."""
    mc, mr = _MAJOR_REVERSE[code[0].upper()]
    mi = _MINOR_LETTERS.index(code[1].upper())
    return mc * 500_000 + (mi % 5) * 100_000, mr * 500_000 + (4 - mi // 5) * 100_000


def _find_tiffs(region, e, n):
    """Return {quadrant: path} for the TIFF files belonging to one tile."""
    rc = region.upper()
    tile = f"{rc}{e}{n}"
    d = os.path.join(TILE_DIR, rc)
    found = {}
    for q in QUADRANTS:
        p = os.path.join(d, f"{tile}{q}.tif")
        if os.path.exists(p):
            found[q] = p
    return found

# ---------------------------------------------------------------------------
# Texture stitching
# ---------------------------------------------------------------------------

SEA_COLOUR = (142, 216, 234)


def stitch_texture(zip_entries, grid_cols, grid_rows,
                   origin_e, origin_n_top, max_size):
    """Composite TIFF quadrants into a single texture aligned to the elevation grid."""
    native_w = grid_cols * CELL_SIZE_M
    native_h = grid_rows * CELL_SIZE_M
    scale = min(1.0, max_size / max(native_w, native_h))
    tex_w = int(native_w * scale)
    tex_h = int(native_h * scale)

    print(f"Texture: {native_w:,}x{native_h:,} native -> {tex_w}x{tex_h} px "
          f"(scale {scale:.4f})")

    canvas = Image.new("RGB", (tex_w, tex_h), SEA_COLOUR)
    placed = 0

    for _, e_d, n_d, region in tqdm(zip_entries, desc="Texturing", unit="tile"):
        tiffs = _find_tiffs(region, e_d, n_d)
        if not tiffs:
            continue

        re_e, re_n = _region_origin(region)
        tile_e = re_e + e_d * TILE_SIZE_M
        tile_n = re_n + n_d * TILE_SIZE_M

        # Position in the texture (top-left origin)
        px_x = int((tile_e - origin_e) * scale)
        px_y = int((origin_n_top - tile_n - TILE_SIZE_M) * scale)
        tile_tw = int(TILE_SIZE_M * scale)
        tile_th = int(TILE_SIZE_M * scale)
        if tile_tw <= 0 or tile_th <= 0:
            continue

        # Composite the quadrant TIFFs into one tile image
        tile_img = Image.new("RGB", (TILE_PX, TILE_PX), SEA_COLOUR)
        for q, qpath in tiffs.items():
            try:
                tile_img.paste(Image.open(qpath).convert("RGB"), QUADRANTS[q])
            except Exception:
                pass

        tile_img = tile_img.resize((tile_tw, tile_th), Image.LANCZOS)
        canvas.paste(tile_img, (px_x, px_y))
        placed += 1

    print(f"Tiles textured: {placed}/{len(zip_entries)}")
    return canvas

# ---------------------------------------------------------------------------
# Mesh generation
# ---------------------------------------------------------------------------

def build_mesh(grid, step, vscale):
    """
    Build a triangle mesh from the elevation grid.

    Returns (vertices, uvs, faces):
        vertices — (N, 3) float64, X = east, Y = elevation, Z = south
        uvs      — (N, 2) float64, (u, v) in [0, 1]
        faces    — (M, 3) int32,   triangle indices (0-based), wound for +Y normals
    """
    sampled = grid[::step, ::step]
    rows, cols = sampled.shape

    cc, rr = np.meshgrid(np.arange(cols), np.arange(rows))
    x = cc.astype(np.float64) * step * CELL_SIZE_M
    z = rr.astype(np.float64) * step * CELL_SIZE_M
    y = sampled.astype(np.float64) * vscale

    vertices = np.stack([x.ravel(), y.ravel(), z.ravel()], axis=1)

    u = cc.astype(np.float64) / max(cols - 1, 1)
    v = 1.0 - rr.astype(np.float64) / max(rows - 1, 1)
    uvs = np.stack([u.ravel(), v.ravel()], axis=1)

    # Two triangles per cell, wound for upward-facing normals (+Y)
    rc2, rr2 = np.meshgrid(np.arange(cols - 1), np.arange(rows - 1))
    tl = (rr2 * cols + rc2).ravel()
    tr = tl + 1
    bl = tl + cols
    br = bl + 1

    faces = np.concatenate([
        np.stack([tl, bl, tr], axis=1),   # triangle 1
        np.stack([bl, br, tr], axis=1),   # triangle 2
    ])

    return vertices, uvs, faces

# ---------------------------------------------------------------------------
# OBJ / MTL writer
# ---------------------------------------------------------------------------

WRITE_BATCH = 50_000


def write_obj(out_dir, name, vertices, uvs, faces, tex_file):
    """Write Wavefront OBJ + MTL files."""
    # --- MTL ---
    mtl_path = os.path.join(out_dir, f"{name}.mtl")
    with open(mtl_path, "w") as f:
        f.write("newmtl terrain\n")
        f.write("Ka 1.0 1.0 1.0\n")
        f.write("Kd 1.0 1.0 1.0\n")
        f.write("Ks 0.0 0.0 0.0\n")
        f.write("illum 1\n")
        f.write(f"map_Kd {tex_file}\n")

    # --- OBJ ---
    obj_path = os.path.join(out_dir, f"{name}.obj")
    n_verts = len(vertices)
    n_faces = len(faces)
    print(f"Writing {n_verts:,} vertices, {n_faces:,} triangles...")

    with open(obj_path, "w", buffering=1 << 17) as f:
        f.write(f"# OS Terrain 50 mesh — {name}\n")
        f.write(f"# {n_verts} vertices, {n_faces} faces\n")
        f.write(f"mtllib {name}.mtl\n")
        f.write(f"usemtl terrain\n\n")

        for i in tqdm(range(0, n_verts, WRITE_BATCH), desc="Vertices", unit_scale=WRITE_BATCH):
            batch = vertices[i:i + WRITE_BATCH]
            f.writelines(f"v {v[0]:.2f} {v[1]:.2f} {v[2]:.2f}\n" for v in batch)

        f.write("\n")
        for i in tqdm(range(0, n_verts, WRITE_BATCH), desc="UVs", unit_scale=WRITE_BATCH):
            batch = uvs[i:i + WRITE_BATCH]
            f.writelines(f"vt {t[0]:.6f} {t[1]:.6f}\n" for t in batch)

        f.write("\n")
        face_1 = faces + 1   # OBJ is 1-indexed
        for i in tqdm(range(0, n_faces, WRITE_BATCH), desc="Faces", unit_scale=WRITE_BATCH):
            batch = face_1[i:i + WRITE_BATCH]
            f.writelines(f"f {a}/{a} {b}/{b} {c}/{c}\n" for a, b, c in batch)

    print(f"Saved: {obj_path}")
    print(f"Saved: {mtl_path}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    # --- Discover tiles ---
    zip_entries = discover_zips(args.input)
    print(f"Found {len(zip_entries)} tiles.")

    # Derive a short name (same logic as generate.py)
    raw = os.path.basename(os.path.normpath(args.input))
    m = re.match(r"([a-z]{2}\d{2})_", raw.lower())
    short = m.group(1).upper() if m else re.sub(r"[^a-zA-Z0-9]", "", raw)[:8].upper()
    name = f"OS_{short}"
    out_dir = args.out or os.path.join(os.path.dirname(__file__), "meshes", name)
    os.makedirs(out_dir, exist_ok=True)

    # --- Load elevation grid ---
    grid, origin_e, origin_n_top = load_elevation_grid(zip_entries)
    grid_rows, grid_cols = grid.shape
    print(f"Elevation grid: {grid_cols} x {grid_rows} cells")

    # --- Build texture ---
    texture = stitch_texture(zip_entries, grid_cols, grid_rows,
                             origin_e, origin_n_top, args.texture)
    tex_file = f"{name}_texture.png"
    texture.save(os.path.join(out_dir, tex_file))
    print(f"Texture saved: {texture.size[0]}x{texture.size[1]} px")

    # --- Build mesh ---
    print(f"\nBuilding mesh (step={args.step}, vscale={args.vscale})...")
    vertices, uvs, faces = build_mesh(grid, args.step, args.vscale)
    print(f"Mesh: {len(vertices):,} vertices, {len(faces):,} triangles")

    # --- Write OBJ ---
    write_obj(out_dir, name, vertices, uvs, faces, tex_file)

    # --- Summary ---
    elev_min, elev_max = float(np.min(grid)), float(np.max(grid))
    print(f"\nDone!")
    print(f"  Output:    {out_dir}/")
    print(f"  Elevation: {elev_min:.0f} m .. {elev_max:.0f} m")
    print(f"  Mesh:      {len(vertices):,} vertices, {len(faces):,} triangles")
    print(f"  Texture:   {texture.size[0]}x{texture.size[1]} px")
    print(f"\nOpen {name}.obj in Blender, MeshLab, or any 3D viewer.")


if __name__ == "__main__":
    main()
