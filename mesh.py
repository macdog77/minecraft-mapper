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
from generate import (
    discover_zips, scan_headers, compute_global_extent, build_tile_index,
    load_tile_with_halo, CELL_SIZE_M,
)

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
# OBJ / MTL writer — streamed per tile
# ---------------------------------------------------------------------------

WRITE_BATCH = 50_000


def _write_mtl(out_dir, name, tex_file):
    mtl_path = os.path.join(out_dir, f"{name}.mtl")
    with open(mtl_path, "w") as f:
        f.write("newmtl terrain\n")
        f.write("Ka 1.0 1.0 1.0\n")
        f.write("Kd 1.0 1.0 1.0\n")
        f.write("Ks 0.0 0.0 0.0\n")
        f.write("illum 1\n")
        f.write(f"map_Kd {tex_file}\n")
    return mtl_path


def stream_obj(out_dir, name, headers, tile_index, origin_e, origin_n_top,
               total_rows, total_cols, step, vscale, tex_file):
    """
    Write a Wavefront OBJ mesh tile-by-tile. Each tile emits vertices for its
    core cells plus a 1-cell border from its east/south neighbours (via halo=1
    on load_tile_with_halo) so boundary quads can be drawn without looking up
    a neighbour's vertex indices later. Border vertices are duplicated between
    adjacent tiles; this keeps the writer single-pass and memory-bounded at
    one tile worth of data.

    `step` subsamples within each tile; it is applied to the tile-local grid,
    so users picking a step that doesn't divide 200 may see minor alignment
    seams at tile boundaries.
    """
    obj_path = os.path.join(out_dir, f"{name}.obj")
    n_verts_total = 0
    n_faces_total = 0

    with open(obj_path, "w", buffering=1 << 17) as f:
        f.write(f"# OS Terrain 50 mesh — {name}\n")
        f.write(f"mtllib {name}.mtl\n")
        f.write(f"usemtl terrain\n\n")

        # Deterministic order — row-major by global NW cell.
        keys_sorted = sorted(
            headers.keys(),
            key=lambda k: (-int(headers[k][0]["yllcorner"]), int(headers[k][0]["xllcorner"]))
        )

        vertex_base = 0  # 0-based; OBJ face indices add 1

        for key in tqdm(keys_sorted, desc="Tiles", unit="tile"):
            hdr, _ = headers[key]
            nrows = int(hdr["nrows"])
            ncols = int(hdr["ncols"])

            # Does a neighbour exist on the east/south edge?
            core_row0 = round((origin_n_top - (hdr["yllcorner"] + nrows * CELL_SIZE_M)) / CELL_SIZE_M)
            core_col0 = round((hdr["xllcorner"] - origin_e) / CELL_SIZE_M)
            has_east  = (core_row0, core_col0 + ncols) in tile_index
            has_south = (core_row0 + nrows, core_col0) in tile_index

            # Load core + 1-cell south/east halo so boundary quads can reach
            # into the neighbour. We still call load_tile_with_halo with halo=1
            # (both sides) for symmetry — only the south/east halo cells are
            # emitted as vertices; NW halo stays unused.
            tile_grid, _core_r0, _core_c0, _nrows, _ncols = load_tile_with_halo(
                key, headers, tile_index, origin_e, origin_n_top, halo=1)

            # Slice starting at the core (drop the NW halo row/col).
            ext_rows = nrows + (1 if has_south else 0)
            ext_cols = ncols + (1 if has_east  else 0)
            ext = tile_grid[1:1 + ext_rows, 1:1 + ext_cols]

            # Apply step — vertices are sampled at rows [0, step, 2*step, ...]
            # up to ext_rows-1. ceil((ext_rows-1)/step)+1 sampled rows.
            s_rows = (ext_rows - 1) // step + 1
            s_cols = (ext_cols - 1) // step + 1
            if s_rows < 1 or s_cols < 1:
                continue

            # Emit vertices (v + vt in batches)
            v_lines = []
            vt_lines = []
            for sr in range(s_rows):
                local_r = sr * step
                global_r = core_row0 + local_r
                z_m = global_r * CELL_SIZE_M
                for sc in range(s_cols):
                    local_c = sc * step
                    global_c = core_col0 + local_c
                    x_m = global_c * CELL_SIZE_M
                    y_m = float(ext[local_r, local_c]) * vscale
                    u = global_c / max(total_cols - 1, 1)
                    v = 1.0 - global_r / max(total_rows - 1, 1)
                    v_lines.append(f"v {x_m:.2f} {y_m:.2f} {z_m:.2f}\n")
                    vt_lines.append(f"vt {u:.6f} {v:.6f}\n")
                    if len(v_lines) >= WRITE_BATCH:
                        f.writelines(v_lines); v_lines.clear()
                        f.writelines(vt_lines); vt_lines.clear()
            if v_lines:
                f.writelines(v_lines); f.writelines(vt_lines)

            # Emit faces — (s_rows-1) * (s_cols-1) quads, two triangles each.
            face_lines = []
            for sr in range(s_rows - 1):
                row_base = vertex_base + sr * s_cols
                for sc in range(s_cols - 1):
                    tl = row_base + sc + 1   # OBJ is 1-based
                    tr = tl + 1
                    bl = tl + s_cols
                    br = bl + 1
                    face_lines.append(f"f {tl}/{tl} {bl}/{bl} {tr}/{tr}\n")
                    face_lines.append(f"f {bl}/{bl} {br}/{br} {tr}/{tr}\n")
                    if len(face_lines) >= WRITE_BATCH:
                        f.writelines(face_lines); face_lines.clear()
            if face_lines:
                f.writelines(face_lines)

            n_verts_total += s_rows * s_cols
            n_faces_total += 2 * max(0, s_rows - 1) * max(0, s_cols - 1)
            vertex_base += s_rows * s_cols

    print(f"Saved: {obj_path}  ({n_verts_total:,} vertices, {n_faces_total:,} triangles)")
    return n_verts_total, n_faces_total

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

    # --- Headers + global extent ---
    headers = scan_headers(zip_entries)
    origin_e, origin_n_top, total_rows, total_cols = compute_global_extent(headers)
    tile_index = build_tile_index(headers, origin_e, origin_n_top)
    print(f"Elevation grid: {total_cols} x {total_rows} cells")

    # --- Build texture (unchanged — PIL canvas is memory-bounded at --texture size) ---
    texture = stitch_texture(zip_entries, total_cols, total_rows,
                             origin_e, origin_n_top, args.texture)
    tex_file = f"{name}_texture.png"
    texture.save(os.path.join(out_dir, tex_file))
    print(f"Texture saved: {texture.size[0]}x{texture.size[1]} px")

    # --- Write OBJ (streamed tile-by-tile) ---
    print(f"\nStreaming mesh (step={args.step}, vscale={args.vscale})...")
    mtl_path = _write_mtl(out_dir, name, tex_file)
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
