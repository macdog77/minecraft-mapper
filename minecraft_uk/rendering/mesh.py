"""OBJ mesh + draped texture writer for OS Terrain 50 data.

Streams one tile at a time so whole-UK exports fit in RAM; vertex indices
are computed locally per tile and never reindexed later.
"""

import os

from PIL import Image
from tqdm import tqdm

from ..osdata.bng import region_origin
from ..osdata.tiff import (
    QUADRANTS,
    SEA_COLOUR,
    TILE_PX,
    TILE_SIZE_M,
    find_tiffs,
)
from ..osdata.tiles import CELL_SIZE_M, load_tile_with_halo


WRITE_BATCH = 50_000


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
        tiffs = find_tiffs(region, e_d, n_d)
        if not tiffs:
            continue

        re_e, re_n = region_origin(region)
        tile_e = re_e + e_d * TILE_SIZE_M
        tile_n = re_n + n_d * TILE_SIZE_M

        px_x = int((tile_e - origin_e) * scale)
        px_y = int((origin_n_top - tile_n - TILE_SIZE_M) * scale)
        tile_tw = int(TILE_SIZE_M * scale)
        tile_th = int(TILE_SIZE_M * scale)
        if tile_tw <= 0 or tile_th <= 0:
            continue

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


def write_mtl(out_dir, name, tex_file):
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
    """Write a Wavefront OBJ mesh tile-by-tile.

    Each tile emits vertices for its core cells plus a 1-cell border from its
    east/south neighbours (via halo=1 on load_tile_with_halo) so boundary quads
    can be drawn without looking up a neighbour's vertex indices later. Border
    vertices are duplicated between adjacent tiles; this keeps the writer
    single-pass and memory-bounded at one tile worth of data.

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

        keys_sorted = sorted(
            headers.keys(),
            key=lambda k: (-int(headers[k][0]["yllcorner"]), int(headers[k][0]["xllcorner"]))
        )

        vertex_base = 0

        for key in tqdm(keys_sorted, desc="Tiles", unit="tile"):
            hdr, _ = headers[key]
            nrows = int(hdr["nrows"])
            ncols = int(hdr["ncols"])

            core_row0 = round((origin_n_top - (hdr["yllcorner"] + nrows * CELL_SIZE_M)) / CELL_SIZE_M)
            core_col0 = round((hdr["xllcorner"] - origin_e) / CELL_SIZE_M)
            has_east  = (core_row0, core_col0 + ncols) in tile_index
            has_south = (core_row0 + nrows, core_col0) in tile_index

            # Load core + 1-cell south/east halo so boundary quads can reach into
            # the neighbour. We call load_tile_with_halo with halo=1 for symmetry;
            # only the south/east halo cells are emitted as vertices.
            tile_grid, _core_r0, _core_c0, _nrows, _ncols = load_tile_with_halo(
                key, headers, tile_index, origin_e, origin_n_top, halo=1)

            ext_rows = nrows + (1 if has_south else 0)
            ext_cols = ncols + (1 if has_east  else 0)
            ext = tile_grid[1:1 + ext_rows, 1:1 + ext_cols]

            s_rows = (ext_rows - 1) // step + 1
            s_cols = (ext_cols - 1) // step + 1
            if s_rows < 1 or s_cols < 1:
                continue

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

            face_lines = []
            for sr in range(s_rows - 1):
                row_base = vertex_base + sr * s_cols
                for sc in range(s_cols - 1):
                    tl = row_base + sc + 1
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
