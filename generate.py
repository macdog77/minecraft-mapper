#!/usr/bin/env python3
"""
generate.py — Generate a Minecraft Java Edition 1.21 world from OS Terrain 50 data.

Usage:
    python generate.py <input> [options]

    input   Single tile .zip, a region folder (e.g. "OS Map Data/data/nn"),
            or the top-level data folder (generates the whole UK).
"""

import argparse
import glob
import os
import re
import shutil
import sys
from math import ceil

import amulet
from amulet.level.formats.anvil_world import AnvilFormat
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
from minecraft_uk.common.paths import WORLDS_DIR
from minecraft_uk.minecraft.chunks import generate_chunk
from minecraft_uk.minecraft.constants import (
    DIMENSION,
    MAP_ZERO_Y,
    MC_VERSION,
    MC_VERSION_ID,
)
from minecraft_uk.minecraft.palettes import make_block_palette
from minecraft_uk.minecraft.world import patch_level_dat, write_entity_files
from minecraft_uk.osdata.bng import wgs84_to_bng
from minecraft_uk.osdata.discovery import discover_zips
from minecraft_uk.osdata.features import (
    MIN_BUILDING_SCALE,
    building_mask_for_tile,
    compute_water_density,
    water_mask_for_tile,
)
from minecraft_uk.osdata.rivers import rasterize_rivers_for_tile
from minecraft_uk.osdata.tiles import (
    CELL_SIZE_M,
    build_tile_index,
    chunks_owned_by_tile,
    compute_global_extent,
    load_tile_with_halo,
    resolve_spawn_elev,
    scan_headers,
)


def main():
    parser = argparse.ArgumentParser(description="Generate a Minecraft world from OS Terrain 50 data.")
    parser.add_argument("input",          help="Tile .zip, region folder, or data root folder")
    parser.add_argument("--scale",   type=int,   default=1,          help="Blocks per OS cell (default 1)")
    parser.add_argument("--vscale",  type=float, default=0.10,       help="Vertical scale multiplier (default 0.10)")
    parser.add_argument("--biomes",  choices=["default", "elevation"], default="elevation",
                                                                      help="Biome mode (default: elevation)")
    parser.add_argument("--spawn",   default=None, metavar="LAT,LON",
                        help="Spawn at a WGS84 geo coord 'lat,lon' (e.g. '56.97,-3.40'). "
                             "Falls back to map centre if the point is outside the map.")
    parser.add_argument("--void", action="store_true",
                        help="Keep the void boundary beyond the pre-filled OS area. "
                             "Minecraft will show the 'Worlds using Experimental Settings "
                             "are not supported' warning on load (default: vanilla terrain, "
                             "no warning).")
    parser.add_argument("--out",     default=None,                   help="Output world folder")
    parser.add_argument("--no-water", action="store_true",
                        help="Skip inland water detection and perimeter rim.")
    parser.add_argument("--no-rivers", action="store_true",
                        help="Skip OS Open Rivers rasterization (auto-off at scale < 4).")
    parser.add_argument("--buildings", action="store_true",
                        help="Detect light-orange building fill from OS raster TIFFs and "
                             f"place bricks on those cells (auto-off at scale < {MIN_BUILDING_SCALE}).")
    parser.add_argument("--tiles-dir", default=None,
                        help="Path to OS raster tiles root (default: '<input-parent>/../tiles').")
    parser.add_argument("--rivers-path", default=None,
                        help="Path to OS Open Rivers .mbtiles "
                             "(default: '<input-parent>/../rivers/Data/oprvrs_gb.mbtiles').")
    parser.add_argument("--halo", type=int, default=None,
                        help="Halo cells loaded around each tile for seamless bilinear / flat-area "
                             "detection. Defaults to ceil(16 / scale) + 3 (19 at scale=1, 5 at "
                             "scale=8). Bigger values cost more zip reads per tile.")
    parser.add_argument("--flush-every", type=int, default=1,
                        help="Flush amulet's in-memory chunk cache every N tiles. Default 1 "
                             "(flush after each tile) for minimum memory. Higher = fewer saves "
                             "but more RAM.")
    args = parser.parse_args()

    # --- Discover tiles ---
    zip_entries = discover_zips(args.input)
    print(f"Found {len(zip_entries)} tiles.")

    # Derive a short world name from the input path
    raw = os.path.basename(os.path.normpath(args.input))
    m = re.match(r"([a-z]{2}\d{2})_", raw.lower())
    short = m.group(1).upper() if m else re.sub(r"[^a-zA-Z0-9]", "", raw)[:8].upper()
    world_name = f"OS_{short}_s{args.scale}_v{args.vscale}"
    out_path   = args.out or os.path.join(WORLDS_DIR, world_name)

    if os.path.exists(out_path):
        print(f"Removing existing world at {out_path}")
        shutil.rmtree(out_path)
    os.makedirs(out_path, exist_ok=True)

    # --- Headers + global extent (cheap — reads only header bytes) ---
    headers = scan_headers(zip_entries)
    origin_easting, origin_northing_top, total_rows, total_cols = compute_global_extent(headers)
    tile_index = build_tile_index(headers, origin_easting, origin_northing_top)
    print(f"Grid: {total_cols} x {total_rows} cells  "
          f"({total_cols*CELL_SIZE_M/1000:.0f} km x {total_rows*CELL_SIZE_M/1000:.0f} km)")

    # --- Halo size ---
    # ceil(16 / scale) covers chunk blocks that extend past the tile edge,
    # +1 for bilinear neighbour, +2 for the 5x5 flat-area window.
    halo = args.halo if args.halo is not None else ceil(16 / args.scale) + 3
    print(f"Halo: {halo} cells per side")

    # --- Resolve tiles dir (shared by water + buildings detection) ---
    if args.tiles_dir:
        tiles_dir = args.tiles_dir
    else:
        first_zip_dir = os.path.dirname(zip_entries[0][0])
        tiles_dir = os.path.normpath(os.path.join(first_zip_dir, "..", "..", "tiles"))

    # --- Resolve rivers path (opened per-tile below) ---
    do_rivers = (not args.no_rivers) and args.scale >= 4
    rivers_path = None
    if do_rivers:
        if args.rivers_path:
            rivers_path = args.rivers_path
        else:
            first_zip_dir = os.path.dirname(zip_entries[0][0])
            rivers_path = os.path.normpath(os.path.join(
                first_zip_dir, "..", "..", "rivers", "Data", "oprvrs_gb.mbtiles"))
        if not os.path.isfile(rivers_path):
            print(f"Rivers file not found at {rivers_path} — skipping.")
            rivers_path = None
            do_rivers = False
    elif not args.no_rivers and args.scale < 4:
        print(f"Skipping rivers (--scale {args.scale} < 4 — lines would be too fat).")

    do_buildings = args.buildings and args.scale >= MIN_BUILDING_SCALE
    if args.buildings and args.scale < MIN_BUILDING_SCALE:
        print(f"Skipping buildings (--scale {args.scale} < {MIN_BUILDING_SCALE} "
              "— each cell resolves to <1 block).")

    # --- Resolve spawn (needed before patch_level_dat) ---
    spawn_col = total_cols // 2
    spawn_row = total_rows // 2
    if args.spawn:
        try:
            lat_s, lon_s = args.spawn.split(",")
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            print(f"--spawn: could not parse '{args.spawn}' — expected 'lat,lon'. "
                  "Using map centre.")
        else:
            east, north = wgs84_to_bng(lat, lon)
            col = int((east - origin_easting) / CELL_SIZE_M)
            row = int((origin_northing_top - north) / CELL_SIZE_M)
            if 0 <= col < total_cols and 0 <= row < total_rows:
                spawn_col, spawn_row = col, row
                print(f"Spawn set from {lat}, {lon} -> grid cell ({col}, {row}).")
            else:
                print(f"Spawn coord {lat}, {lon} (BNG {east:.0f}E {north:.0f}N) "
                      f"is outside the generated map — using map centre.")
    spawn_elev = resolve_spawn_elev(spawn_row, spawn_col, headers, tile_index,
                                    origin_easting, origin_northing_top)
    spawn_x = (spawn_col * args.scale) + (args.scale // 2)
    spawn_z = (spawn_row * args.scale) + (args.scale // 2)
    spawn_y = MAP_ZERO_Y + round(spawn_elev * args.vscale) + 1

    # --- Create world ---
    print(f"\nCreating world: {world_name}")
    fmt = AnvilFormat(out_path)
    fmt.create_and_open(MC_VERSION_ID, MC_VERSION, overwrite=True)
    fmt.close()

    # Patch level.dat BEFORE load_level. Amulet's AnvilFormat reads bounds from
    # level.dat's WorldGenSettings when the level loads; if WorldGenSettings is
    # missing (as it is in amulet's fresh create_and_open output), it falls
    # back to DefaultSelection (Y=0..256) and every saved chunk gets truncated
    # to 16 sections, silently dropping anything above Y=255 or below Y=0.
    patch_level_dat(out_path, world_name, spawn_x, spawn_y, spawn_z, void=args.void)

    level = amulet.load_level(out_path)
    block_uni = make_block_palette(level)

    # --- World dimensions ---
    mc_width  = total_cols * args.scale
    mc_depth  = total_rows * args.scale
    cx_max = ceil(mc_width  / 16)
    cz_max = ceil(mc_depth  / 16)
    total_chunks = cx_max * cz_max

    print(f"World size: {mc_width} x {mc_depth} blocks  ({total_chunks} chunks)")
    print(f"Scale: --scale {args.scale}  --vscale {args.vscale}  --biomes {args.biomes}")
    print(f"Streaming {len(headers)} tiles (flush every {args.flush_every})...")

    # --- Rivers: open SQLite connection once, reuse across tiles ---
    rivers_conn = None
    if do_rivers:
        import sqlite3
        rivers_conn = sqlite3.connect(rivers_path)

    elev_min, elev_max = float("inf"), float("-inf")
    tiles_done = 0

    with tqdm(total=total_chunks, unit="chunk") as pbar:
        for key in headers:
            tile_grid, core_row0, core_col0, nrows, ncols = load_tile_with_halo(
                key, headers, tile_index, origin_easting, origin_northing_top, halo)
            tile_origin = (core_row0 - halo, core_col0 - halo)

            # Core-only elevation range (halo cells belong to neighbouring tiles)
            core_slice = tile_grid[halo:halo + nrows, halo:halo + ncols]
            if core_slice.size:
                elev_min = min(elev_min, float(core_slice.min()))
                elev_max = max(elev_max, float(core_slice.max()))

            water_mask = None
            if not args.no_water:
                water_mask = water_mask_for_tile(
                    tile_grid, core_row0, core_col0, nrows, ncols, halo,
                    headers, tile_index, tiles_dir)

            river_mask = None
            if do_rivers:
                local_min_east = origin_easting + (core_col0 - halo) * CELL_SIZE_M
                local_max_north = origin_northing_top - (core_row0 - halo) * CELL_SIZE_M
                river_mask = rasterize_rivers_for_tile(
                    tile_grid.shape, local_min_east, local_max_north,
                    rivers_path, conn=rivers_conn)
                if river_mask is not None:
                    water_mask = river_mask if water_mask is None else (water_mask | river_mask)

            water_density = None
            if water_mask is not None:
                water_density = compute_water_density(water_mask, river_mask)

            building_mask = None
            if do_buildings:
                building_mask = building_mask_for_tile(
                    core_row0, core_col0, nrows, ncols, halo,
                    headers, tile_index, tiles_dir)

            for cx, cz in chunks_owned_by_tile(core_row0, core_col0, nrows, ncols, args.scale):
                chunk = generate_chunk(
                    cx, cz, tile_grid, tile_origin,
                    scale=args.scale,
                    vscale=args.vscale,
                    block_uni=block_uni,
                    biome_mode=args.biomes,
                    water_mask=water_mask,
                    water_density=water_density,
                    building_mask=building_mask,
                    world_rows=total_rows,
                    world_cols=total_cols,
                    mc_width=mc_width,
                    mc_depth=mc_depth,
                )
                level.put_chunk(chunk, DIMENSION)
                pbar.update(1)

            tiles_done += 1
            # Release mask references before the flush so the arrays can be GC'd
            tile_grid = water_mask = water_density = building_mask = river_mask = None
            if tiles_done % args.flush_every == 0:
                level.save()
                level.unload()

    # Final flush for any residual chunks
    print("Saving world...")
    level.save()
    level.close()

    if rivers_conn is not None:
        rivers_conn.close()

    # --- Entity storage (required by Minecraft 1.17+) ---
    write_entity_files(out_path, 0, cx_max, 0, cz_max)

    # --- Summary ---
    region_files = glob.glob(os.path.join(out_path, "region", "*.mca"))
    y_min_used = MAP_ZERO_Y + round(elev_min * args.vscale)
    y_max_used = MAP_ZERO_Y + round(elev_max * args.vscale)

    print(f"\nDone!")
    print(f"  World:       {out_path}")
    print(f"  Region files:{len(region_files)}")
    print(f"  Elevation:   {elev_min:.0f} m .. {elev_max:.0f} m")
    print(f"  Y range:     {y_min_used} .. {y_max_used}")
    print(f"\nCopy '{out_path}' into your Minecraft saves folder to play.")


if __name__ == "__main__":
    main()
