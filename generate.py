#!/usr/bin/env python3
"""
generate.py — Generate a Minecraft Java Edition 1.21 world from OS Terrain 50 data.

Usage:
    python generate.py <input> [options]

    input   Single tile .zip, a region folder (e.g. "OS Map Data/data/nn"),
            or the top-level data folder (generates the whole UK).

Options:
    --scale N       Blocks per OS cell (default 1 = 1 block per 50 m cell).
                    N=2 → 1 block = 25 m,  N=4 → 1 block = 12.5 m.
    --vscale F      Vertical multiplier on elevation metres (default 0.10).
                    Y = 64 + round(elevation_m * F).
                    Suggested values:  0.10 (subtle), 0.15 (moderate), 0.20 (dramatic).
    --biomes MODE   'default' = plains everywhere, 'elevation' = biome by height (default).
    --spawn NAME    Named spawn point (default 'ben_lomond').
    --out PATH      Output world folder (default ./worlds/<NAME>).
"""

import argparse
import glob
import os
import re
import shutil
import sys
import zipfile
from math import ceil

import amulet
import amulet_nbt
import numpy as np
from PIL import Image
from amulet.api.chunk import Chunk
from amulet.api.block import Block
from amulet.level.formats.anvil_world import AnvilFormat
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
from heightmap import parse_asc

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MC_VERSION      = (1, 21, 0)
MC_VERSION_ID   = "java"
DIMENSION       = "minecraft:overworld"
MAP_ZERO_Y      = 64          # Minecraft Y that corresponds to 0 m OS elevation
SEA_SURFACE_Y   = 63          # highest water block (one below MAP_ZERO_Y)
Y_MIN           = -64         # Minecraft bedrock level
Y_MAX           = 319         # Minecraft build limit
CELL_SIZE_M     = 50          # OS Terrain 50 cell size in metres

# Water detection tuning
TIFF_PX_PER_M        = 1       # OS tiles are 1 m/pixel, so 50 px per 50 m cell
TIFF_WATER_THRESHOLD = 0.5     # fraction of blue pixels per cell to mark as water
FLAT_WINDOW          = 5       # cells; 5 x 50 m = 250 m flat-area detector
FLAT_RANGE_M         = 0.05    # cells in window must vary less than this to be "flat"

# Block palette — elevation thresholds (metres) for surface/sub-surface selection
THRESHOLDS = [
    # (min_elev, surface_name, surface_props, subsurface_name)
    (1100, "snow_block",   {},                          "stone"),
    ( 600, "stone",        {},                          "stone"),
    (  10, "grass_block",  {"snowy": "false"},          "dirt"),
    (   0, "sand",         {},                          "sandstone"),
    (None, "gravel",       {},                          "stone"),   # sea floor (elev <= 0)
]

# Biome mapping by elevation (used when --biomes elevation)
BIOME_THRESHOLDS = [
    (1100, "minecraft:frozen_peaks"),
    ( 600, "minecraft:stony_peaks"),
    ( 300, "minecraft:windswept_hills"),
    (   0, "minecraft:plains"),
    (None, "minecraft:ocean"),
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_zip(path):
    """Return (header, rows) from the .asc inside a zip."""
    with zipfile.ZipFile(path) as zf:
        asc_names = [n for n in zf.namelist()
                     if n.lower().endswith(".asc") and not n.lower().endswith(".aux.xml")]
        if not asc_names:
            raise ValueError(f"No .asc found in {path}")
        with zf.open(asc_names[0]) as f:
            return parse_asc(f)


def _load_asc(path):
    with open(path, "r") as f:
        return parse_asc(f)


def discover_zips(input_path):
    """
    Return a list of (zip_path, e_digit, n_digit, region_code) tuples.
    Accepts a single zip, a region folder, or a parent folder of region folders.
    """
    input_path = os.path.abspath(input_path)

    if input_path.lower().endswith(".zip"):
        name = os.path.basename(input_path).lower()
        m = re.match(r"([a-z]{2})(\d)(\d)_", name)
        if not m:
            raise ValueError(f"Cannot parse tile code from {name}")
        return [(input_path, int(m.group(2)), int(m.group(3)), m.group(1))]

    # Collect all zips recursively
    results = []
    for zp in glob.glob(os.path.join(input_path, "**", "*.zip"), recursive=True):
        name = os.path.basename(zp).lower()
        m = re.match(r"([a-z]{2})(\d)(\d)_", name)
        if m:
            results.append((zp, int(m.group(2)), int(m.group(3)), m.group(1)))
    if not results:
        raise ValueError(f"No OS Terrain 50 zip tiles found under {input_path}")
    return results


def load_elevation_grid(zip_entries):
    """
    Stitch all tiles into a single 2D numpy float32 array.
    Returns (grid, origin_easting, origin_northing_top).

    grid[row, col] = elevation in metres.
    Row 0 = northernmost, col 0 = westernmost.
    origin_easting      = BNG easting of the left edge of col 0.
    origin_northing_top = BNG northing of the top edge of row 0.
    """
    # Determine bounding box from tile headers
    # Each tile header gives us xllcorner, yllcorner (SW corner of tile)
    headers = {}
    print(f"Reading {len(zip_entries)} tile headers...")
    for zp, e_digit, n_digit, region in tqdm(zip_entries, unit="tile", leave=False):
        try:
            hdr, _ = _load_zip(zp)
            headers[(e_digit, n_digit, region)] = hdr
        except Exception as ex:
            print(f"  Warning: skipping {os.path.basename(zp)}: {ex}")

    if not headers:
        raise RuntimeError("No tiles could be read.")

    # Grid extents in terms of (e_digit, n_digit) — may span multiple regions
    # We use the actual BNG coordinates from headers for accuracy.
    # Compute global origin
    min_east  = min(h["xllcorner"] for h in headers.values())
    min_north = min(h["yllcorner"] for h in headers.values())
    max_north = max(h["yllcorner"] + h["nrows"] * CELL_SIZE_M for h in headers.values())
    max_east  = max(h["xllcorner"] + h["ncols"] * CELL_SIZE_M for h in headers.values())

    total_cols = round((max_east  - min_east)  / CELL_SIZE_M)
    total_rows = round((max_north - min_north) / CELL_SIZE_M)

    print(f"Grid: {total_cols} x {total_rows} cells  "
          f"({total_cols*CELL_SIZE_M/1000:.0f} km x {total_rows*CELL_SIZE_M/1000:.0f} km)")
    print(f"BNG origin: E{min_east:.0f}  N{min_north:.0f}..{max_north:.0f}")

    grid = np.full((total_rows, total_cols), np.nan, dtype=np.float32)

    print("Loading elevation data...")
    for zp, e_digit, n_digit, region in tqdm(zip_entries, unit="tile"):
        key = (e_digit, n_digit, region)
        if key not in headers:
            continue
        try:
            hdr, rows = _load_zip(zp)
        except Exception as ex:
            print(f"  Warning: skipping {os.path.basename(zp)}: {ex}")
            continue

        xll = hdr["xllcorner"]
        yll = hdr["yllcorner"]
        ncols = int(hdr["ncols"])
        nrows = int(hdr["nrows"])
        nodata = hdr.get("nodata_value", -9999)

        # Map this tile's SW corner to grid indices
        col_offset = round((xll - min_east)  / CELL_SIZE_M)
        # Tile's northernmost row → grid row offset (grid row 0 = northernmost overall)
        row_offset = round((max_north - (yll + nrows * CELL_SIZE_M)) / CELL_SIZE_M)

        arr = np.array(rows, dtype=np.float32)
        arr[arr == nodata] = np.nan
        # ASC row 0 = northernmost, col 0 = westernmost — matches our grid convention
        grid[row_offset:row_offset + nrows, col_offset:col_offset + ncols] = arr

    # Replace NaN (sea / missing) with 0
    grid = np.where(np.isnan(grid), 0.0, grid)
    return grid, min_east, max_north


# ---------------------------------------------------------------------------
# Water detection
# ---------------------------------------------------------------------------

def _is_water_color(r, g, b):
    """True if an OS raster palette entry is a water colour (pale cyan..deeper blue)."""
    return (r < g - 10) and (r < b - 10) and (g + b) > 300


def _tile_water_fraction(tif_path, cell_size_px):
    """Read a palette TIFF quadrant and return fraction-of-water per cell grid."""
    im = Image.open(tif_path)
    pal = im.getpalette() or []
    arr = np.asarray(im, dtype=np.uint8)
    lut = np.zeros(256, dtype=bool)
    for idx in range(min(256, len(pal) // 3)):
        if _is_water_color(pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]):
            lut[idx] = True
    mask = lut[arr]
    h, w = mask.shape
    ch, cw = h // cell_size_px, w // cell_size_px
    trimmed = mask[:ch * cell_size_px, :cw * cell_size_px]
    return trimmed.reshape(ch, cell_size_px, cw, cell_size_px).mean(axis=(1, 3))


def _flat_area_mask(grid, window, range_threshold):
    """Cells whose local `window x window` elevation range is below threshold."""
    rows, cols = grid.shape
    if rows < window or cols < window:
        return np.zeros(grid.shape, dtype=bool)
    from numpy.lib.stride_tricks import sliding_window_view
    pad = window // 2
    padded = np.pad(grid, pad, mode="edge")
    win = sliding_window_view(padded, (window, window))
    local_range = win.max(axis=(-1, -2)) - win.min(axis=(-1, -2))
    return local_range < range_threshold


def load_water_mask(zip_entries, grid, min_east, max_north, tiles_dir,
                    tiff_threshold=TIFF_WATER_THRESHOLD,
                    flat_window=FLAT_WINDOW,
                    flat_range=FLAT_RANGE_M):
    """
    Build a boolean water mask, same shape as `grid`.

    A cell is water if either:
      * the matching 50 m x 50 m block of the OS raster TIFF is mostly blue; or
      * its local 5 x 5 elevation window has a range below `flat_range`
        (this catches lochs and reservoirs, which show a single flat elevation).
    """
    grid_rows, grid_cols = grid.shape
    mask = np.zeros((grid_rows, grid_cols), dtype=bool)

    # --- TIFF blue detection ---
    if tiles_dir and os.path.isdir(tiles_dir):
        print(f"Detecting water from map TIFFs in {tiles_dir}...")
        missing = 0
        for zp, e_digit, n_digit, region in tqdm(zip_entries, unit="tile"):
            try:
                hdr, _ = _load_zip(zp)
            except Exception:
                continue
            ncols, nrows = int(hdr["ncols"]), int(hdr["nrows"])
            col_offset = round((hdr["xllcorner"] - min_east) / CELL_SIZE_M)
            row_offset = round((max_north - (hdr["yllcorner"] + nrows * CELL_SIZE_M)) / CELL_SIZE_M)

            tile_code = f"{region.upper()}{e_digit}{n_digit}"
            region_up = region.upper()
            half_rows, half_cols = nrows // 2, ncols // 2
            quadrants = [
                ("NW", 0,         0),
                ("NE", 0,         half_cols),
                ("SW", half_rows, 0),
                ("SE", half_rows, half_cols),
            ]
            for quad, q_row, q_col in quadrants:
                tif_path = os.path.join(tiles_dir, region_up, f"{tile_code}{quad}.tif")
                if not os.path.isfile(tif_path):
                    missing += 1
                    continue
                try:
                    frac = _tile_water_fraction(tif_path, cell_size_px=CELL_SIZE_M * TIFF_PX_PER_M)
                except Exception as ex:
                    tqdm.write(f"  Warning: {os.path.basename(tif_path)}: {ex}")
                    continue
                qh, qw = frac.shape
                r0, c0 = row_offset + q_row, col_offset + q_col
                r1, c1 = min(r0 + qh, grid_rows), min(c0 + qw, grid_cols)
                if r1 > r0 and c1 > c0:
                    mask[r0:r1, c0:c1] |= frac[:r1 - r0, :c1 - c0] > tiff_threshold
        if missing:
            print(f"  ({missing} quadrant TIFFs missing — OK if the area is off-coverage)")
    else:
        print("Skipping TIFF water detection (tiles directory not found).")

    # --- Flat-area detection (inland lochs, reservoirs) ---
    print("Detecting water from flat elevation regions...")
    flat = _flat_area_mask(grid, flat_window, flat_range)
    mask |= flat

    n_water = int(mask.sum())
    print(f"Water cells: {n_water:,} / {mask.size:,}  ({100 * n_water / mask.size:.1f}%)")
    return mask


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------

def _make_palette(level):
    """
    Return a dict of name -> universal Block for all needed blocks.
    Also returns a helper fn: block_ids(chunk) -> dict mapping names to palette IDs.
    """
    ver = level.level_wrapper.translation_manager.get_version(MC_VERSION_ID, MC_VERSION)

    def to_uni(name, str_props=None):
        props = {k: amulet.StringTag(v) for k, v in (str_props or {}).items()}
        u, _, _ = ver.block.to_universal(Block("minecraft", name, props))
        return u

    return {
        "air":      to_uni("air"),
        "bedrock":  to_uni("bedrock"),
        "stone":    to_uni("stone"),
        "dirt":     to_uni("dirt"),
        "grass":    to_uni("grass_block", {"snowy": "false"}),
        "sand":     to_uni("sand"),
        "sandstone":to_uni("sandstone"),
        "gravel":   to_uni("gravel"),
        "snow":     to_uni("snow_block"),
        "water":    to_uni("water", {"level": "0"}),
    }


def _surface_and_sub(elev_m):
    """Return (surface_block_key, sub_block_key) for a given elevation."""
    for threshold, surf, _, sub in THRESHOLDS:
        if threshold is None or elev_m >= threshold:
            # Map surf name to our palette key
            surf_key = {
                "snow_block":  "snow",
                "stone":       "stone",
                "grass_block": "grass",
                "sand":        "sand",
                "gravel":      "gravel",
            }[surf]
            return surf_key, sub
    return "gravel", "stone"


def _biome_name(elev_m):
    for threshold, biome in BIOME_THRESHOLDS:
        if threshold is None or elev_m >= threshold:
            return biome
    return "minecraft:ocean"

# ---------------------------------------------------------------------------
# Chunk generation
# ---------------------------------------------------------------------------

ARRAY_OFFSET = -Y_MIN   # = 64; array index = Y + ARRAY_OFFSET
ARRAY_HEIGHT = Y_MAX - Y_MIN + 1  # = 384

def generate_chunk(cx, cz, grid, scale, vscale, block_uni, biome_mode,
                   water_mask=None, mc_width=None, mc_depth=None):
    """
    Build and return an amulet Chunk for chunk coordinates (cx, cz).
    block_uni: dict name -> universal Block object.

    If `water_mask` is given, cells where it is True have their top block
    replaced with water (lochs get a single water block at the loch surface).

    If `mc_width`/`mc_depth` are given, edge columns that contain water are
    capped with stone up to one block above the local water level — just
    enough to stop water escaping into the surrounding void.
    """
    chunk = Chunk(cx, cz)
    pal = chunk.block_palette

    # Register all blocks and get their palette IDs
    ids = {name: pal.get_add_block(blk) for name, blk in block_uni.items()}
    AIR_ID     = ids["air"]
    BEDROCK_ID = ids["bedrock"]
    STONE_ID   = ids["stone"]
    DIRT_ID    = ids["dirt"]
    SAND_ID    = ids["sand"]
    SANDS_ID   = ids["sandstone"]
    GRAVEL_ID  = ids["gravel"]
    SNOW_ID    = ids["snow"]
    GRASS_ID   = ids["grass"]
    WATER_ID   = ids["water"]

    surf_ids = {
        "snow":   SNOW_ID,
        "stone":  STONE_ID,
        "grass":  GRASS_ID,
        "sand":   SAND_ID,
        "gravel": GRAVEL_ID,
    }
    sub_ids = {
        "stone":     STONE_ID,
        "dirt":      DIRT_ID,
        "sandstone": SANDS_ID,
    }

    grid_rows, grid_cols = grid.shape

    # Build a full-height column array: shape (16, ARRAY_HEIGHT, 16)
    # Index [lx, y_arr, lz] where y_arr = Y + ARRAY_OFFSET
    col_blocks = np.zeros((16, ARRAY_HEIGHT, 16), dtype=np.uint32)  # 0 = air

    # Track dominant biome per column for assignment
    biome_elevs = np.zeros((16, 16), dtype=np.float32)

    for lx in range(16):
        for lz in range(16):
            gx = cx * 16 + lx   # global block X
            gz = cz * 16 + lz   # global block Z

            # Map block coords → cell coords
            cell_col = gx // scale
            cell_row = gz // scale

            if 0 <= cell_row < grid_rows and 0 <= cell_col < grid_cols:
                elev = float(grid[cell_row, cell_col])
            else:
                elev = 0.0

            biome_elevs[lx, lz] = elev

            y_surf = int(round(MAP_ZERO_Y + elev * vscale))
            y_surf = max(Y_MIN, min(Y_MAX, y_surf))
            # Force sea floor at least one block below sea surface so water can sit on top.
            # Without this, cells with elev ~= 0 round to y_surf = MAP_ZERO_Y (Y=64) and
            # leave no room for a water block at SEA_SURFACE_Y (Y=63).
            if elev <= 0 and y_surf > SEA_SURFACE_Y - 1:
                y_surf = SEA_SURFACE_Y - 1
            arr_surf = y_surf + ARRAY_OFFSET  # array index of surface

            # --- Bedrock at Y_MIN ---
            col_blocks[lx, 0, lz] = BEDROCK_ID

            # --- Stone fill: Y_MIN+1 .. y_surf-4 ---
            stone_end = max(1, arr_surf - 3)
            if stone_end > 1:
                col_blocks[lx, 1:stone_end, lz] = STONE_ID

            # --- Sub-surface: y_surf-3 .. y_surf-1 ---
            surf_key, sub_key = _surface_and_sub(elev)
            sub_id  = sub_ids.get(sub_key, STONE_ID)
            surf_id = surf_ids.get(surf_key, GRASS_ID)

            sub_start = max(1, arr_surf - 3)
            sub_end   = arr_surf
            if sub_end > sub_start:
                col_blocks[lx, sub_start:sub_end, lz] = sub_id

            # --- Surface block ---
            if 0 <= arr_surf < ARRAY_HEIGHT:
                col_blocks[lx, arr_surf, lz] = surf_id

            # --- Water fill (for elevation <= 0) ---
            if elev <= 0 and arr_surf < SEA_SURFACE_Y + ARRAY_OFFSET:
                water_start = arr_surf + 1
                water_end   = SEA_SURFACE_Y + ARRAY_OFFSET + 1   # inclusive of Y=63
                if water_end > water_start:
                    col_blocks[lx, water_start:water_end, lz] = WATER_ID

            # --- Inland water overlay: replace top block with water ---
            # Applies to cells flagged by TIFF-blue or flat-area detection.
            # Sea (elev <= 0) is already handled above, so we only touch elev > 0.
            if (water_mask is not None
                    and 0 <= cell_row < grid_rows and 0 <= cell_col < grid_cols
                    and water_mask[cell_row, cell_col]
                    and elev > 0
                    and 0 <= arr_surf < ARRAY_HEIGHT):
                col_blocks[lx, arr_surf, lz] = WATER_ID

            # --- Perimeter rim: only where water reaches the world edge ---
            if (mc_width is not None and mc_depth is not None
                    and (gx == 0 or gx == mc_width - 1
                         or gz == 0 or gz == mc_depth - 1)):
                is_mask_water = (
                    water_mask is not None
                    and 0 <= cell_row < grid_rows and 0 <= cell_col < grid_cols
                    and water_mask[cell_row, cell_col]
                    and elev > 0
                )
                is_sea_water = elev <= 0 and arr_surf < SEA_SURFACE_Y + ARRAY_OFFSET
                if is_mask_water:
                    water_top_arr = arr_surf
                elif is_sea_water:
                    water_top_arr = SEA_SURFACE_Y + ARRAY_OFFSET
                else:
                    water_top_arr = None
                if water_top_arr is not None:
                    rim_start = max(0, arr_surf)
                    rim_end = min(ARRAY_HEIGHT - 1, water_top_arr + 1)
                    if rim_end >= rim_start:
                        col_blocks[lx, rim_start:rim_end + 1, lz] = STONE_ID

    # --- Add sections to chunk ---
    for si in range(-4, 20):   # sections covering Y -64..319
        arr_start = si * 16 + ARRAY_OFFSET
        arr_end   = arr_start + 16
        if arr_end <= 0 or arr_start >= ARRAY_HEIGHT:
            continue
        section_data = col_blocks[:, arr_start:arr_end, :]
        if np.any(section_data != 0):
            chunk.blocks.add_section(si, section_data)

    # --- Biomes ---
    if biome_mode == "elevation":
        chunk.biomes.convert_to_3d()
        avg_elev = float(np.mean(biome_elevs))
        biome_name = _biome_name(avg_elev)
        biome_id = chunk.biome_palette.get_add_biome(biome_name)

        for si in range(-4, 20):
            sec = np.full((4, 4, 4), biome_id, dtype=np.uint32)
            chunk.biomes.add_section(si, sec)

    return chunk

# ---------------------------------------------------------------------------
# Spawn point helpers
# ---------------------------------------------------------------------------


def write_entity_files(world_path, cx_min, cx_max, cz_min, cz_max):
    """
    Create entities/<r.X.Z.mca> files for every chunk in the generated range.
    Minecraft 1.17+ stores entity data separately from chunk data; without these
    files every chunk fails to load with NoSuchElementException in EntityStorage.

    Each chunk entry is an NBT compound with DataVersion, Position, and an empty
    Entities list — the minimum Minecraft needs to not error.
    """
    import struct, zlib

    entities_dir = os.path.join(world_path, "entities")
    os.makedirs(entities_dir, exist_ok=True)

    # Group chunks by region file (32×32 chunks per region)
    regions = {}
    for cx in range(cx_min, cx_max):
        for cz in range(cz_min, cz_max):
            rx, rz = cx >> 5, cz >> 5
            regions.setdefault((rx, rz), []).append((cx, cz))

    for (rx, rz), chunks in regions.items():
        out = os.path.join(entities_dir, f"r.{rx}.{rz}.mca")

        # Build sector data for each chunk
        location_table  = bytearray(4096)  # 1024 × 4 bytes
        timestamp_table = bytearray(4096)  # 1024 × 4 bytes
        sector_data     = bytearray()

        sector_offset = 2  # first two sectors are the header tables

        for cx, cz in chunks:
            # Local chunk position within region
            local_cx = cx & 0x1f
            local_cz = cz & 0x1f
            table_idx = local_cz * 32 + local_cx

            # Minimal entity chunk NBT
            nbt = amulet_nbt.NamedTag(
                amulet_nbt.CompoundTag({
                    "DataVersion": amulet_nbt.IntTag(3953),
                    "Position":   amulet_nbt.IntArrayTag([cx, cz]),
                    "Entities":   amulet_nbt.ListTag([]),
                })
            )
            raw_nbt   = nbt.to_nbt(compressed=False, little_endian=False)
            compressed = zlib.compress(raw_nbt)

            # Chunk payload: 4-byte length + 1-byte compression type + data
            payload = struct.pack(">I", len(compressed) + 1) + b"\x02" + compressed

            # Pad to 4096-byte sector boundary
            pad = (4096 - len(payload) % 4096) % 4096
            payload += b"\x00" * pad
            sectors = len(payload) // 4096

            # Write location entry: 3-byte offset + 1-byte sector count
            loc_bytes = struct.pack(">I", (sector_offset << 8) | sectors)
            location_table[table_idx * 4:(table_idx + 1) * 4] = loc_bytes

            sector_data += payload
            sector_offset += sectors

        with open(out, "wb") as f:
            f.write(bytes(location_table))
            f.write(bytes(timestamp_table))
            f.write(bytes(sector_data))

    print(f"Entity files written: {len(regions)} region(s) in {entities_dir}")


def _void_flat_generator(biome="minecraft:plains"):
    """Return NBT CompoundTag for a flat/void dimension generator."""
    return amulet_nbt.CompoundTag({
        "type": amulet_nbt.StringTag("minecraft:flat"),
        "settings": amulet_nbt.CompoundTag({
            "biome": amulet_nbt.StringTag(biome),
            "features": amulet_nbt.ByteTag(0),
            "lakes": amulet_nbt.ByteTag(0),
            "layers": amulet_nbt.ListTag([]),
            "structure_overrides": amulet_nbt.ListTag([]),
        }),
    })


def patch_level_dat(world_path, world_name, mc_x, mc_y, mc_z):
    """
    Replace the minimal amulet-generated level.dat with a complete, valid one
    that Minecraft 1.21.x will accept.
    """
    dat_path = os.path.join(world_path, "level.dat")
    nbt = amulet_nbt.load(dat_path)
    data = nbt.tag["Data"]

    # --- Spawn ---
    data["SpawnX"] = amulet_nbt.IntTag(mc_x)
    data["SpawnY"] = amulet_nbt.IntTag(mc_y)
    data["SpawnZ"] = amulet_nbt.IntTag(mc_z)
    data["SpawnAngle"] = amulet_nbt.FloatTag(0.0)

    # --- World name and game settings ---
    data["LevelName"] = amulet_nbt.StringTag(world_name)
    data["GameType"]  = amulet_nbt.IntTag(1)        # 1 = creative
    data["Difficulty"] = amulet_nbt.ByteTag(2)       # 2 = normal
    data["allowCommands"] = amulet_nbt.ByteTag(1)
    data["hardcore"]  = amulet_nbt.ByteTag(0)
    data["initialized"] = amulet_nbt.ByteTag(1)
    data["DayTime"]   = amulet_nbt.LongTag(6000)
    data["Time"]      = amulet_nbt.LongTag(0)
    data["rainTime"]  = amulet_nbt.IntTag(0)
    data["raining"]   = amulet_nbt.ByteTag(0)
    data["thunderTime"] = amulet_nbt.IntTag(0)
    data["thundering"] = amulet_nbt.ByteTag(0)

    # --- Version compound (required for 1.18+) ---
    # DataVersion 3953 = Java 1.21.0.  Minecraft will auto-upgrade the world data.
    data["Version"] = amulet_nbt.CompoundTag({
        "Id":       amulet_nbt.IntTag(3953),
        "Name":     amulet_nbt.StringTag("1.21"),
        "Series":   amulet_nbt.StringTag("main"),
        "Snapshot": amulet_nbt.ByteTag(0),
    })

    # --- WorldGenSettings (the missing piece that caused the crash) ---
    # We use void flat generators so Minecraft doesn't try to terrain-gen new chunks
    # on top of our pre-filled area.
    data["WorldGenSettings"] = amulet_nbt.CompoundTag({
        "bonus_chest":       amulet_nbt.ByteTag(0),
        "generate_features": amulet_nbt.ByteTag(0),
        "seed":              amulet_nbt.LongTag(0),
        "dimensions": amulet_nbt.CompoundTag({
            "minecraft:overworld": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:overworld"),
                "generator": _void_flat_generator("minecraft:plains"),
            }),
            "minecraft:the_nether": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:the_nether"),
                "generator": _void_flat_generator("minecraft:nether_wastes"),
            }),
            "minecraft:the_end": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:the_end"),
                "generator": _void_flat_generator("minecraft:the_end"),
            }),
        }),
    })

    nbt.save_to(dat_path)
    print(f"Spawn set: X={mc_x} Y={mc_y} Z={mc_z}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a Minecraft world from OS Terrain 50 data.")
    parser.add_argument("input",          help="Tile .zip, region folder, or data root folder")
    parser.add_argument("--scale",   type=int,   default=1,          help="Blocks per OS cell (default 1)")
    parser.add_argument("--vscale",  type=float, default=0.10,       help="Vertical scale multiplier (default 0.10)")
    parser.add_argument("--biomes",  choices=["default", "elevation"], default="elevation",
                                                                      help="Biome mode (default: elevation)")
    parser.add_argument("--out",     default=None,                   help="Output world folder")
    parser.add_argument("--no-water", action="store_true",
                        help="Skip inland water detection and perimeter rim.")
    parser.add_argument("--tiles-dir", default=None,
                        help="Path to OS raster tiles root (default: '<input-parent>/../tiles').")
    args = parser.parse_args()

    # --- Discover tiles ---
    zip_entries = discover_zips(args.input)
    print(f"Found {len(zip_entries)} tiles.")

    # Derive a short world name from the input path
    raw = os.path.basename(os.path.normpath(args.input))
    # For a single zip like "nn30_OST50GRID_20250529.zip" extract just the tile code
    m = re.match(r"([a-z]{2}\d{2})_", raw.lower())
    short = m.group(1).upper() if m else re.sub(r"[^a-zA-Z0-9]", "", raw)[:8].upper()
    world_name = f"OS_{short}_s{args.scale}_v{args.vscale}"
    out_path   = args.out or os.path.join(os.path.dirname(__file__), "worlds", world_name)

    if os.path.exists(out_path):
        print(f"Removing existing world at {out_path}")
        shutil.rmtree(out_path)
    os.makedirs(out_path, exist_ok=True)

    # --- Load elevation grid ---
    grid, origin_easting, origin_northing_top = load_elevation_grid(zip_entries)
    grid_rows, grid_cols = grid.shape

    # --- Load water mask ---
    water_mask = None
    if not args.no_water:
        # Default tiles dir is a sibling of the data folder: .../OS Map Data/tiles
        if args.tiles_dir:
            tiles_dir = args.tiles_dir
        else:
            first_zip_dir = os.path.dirname(zip_entries[0][0])
            tiles_dir = os.path.normpath(os.path.join(first_zip_dir, "..", "..", "tiles"))
        water_mask = load_water_mask(zip_entries, grid, origin_easting,
                                     origin_northing_top, tiles_dir)

    # --- Create world ---
    print(f"\nCreating world: {world_name}")
    fmt = AnvilFormat(out_path)
    fmt.create_and_open(MC_VERSION_ID, MC_VERSION, overwrite=True)
    fmt.close()

    level = amulet.load_level(out_path)

    # Build universal block palette (once, shared across chunks)
    block_uni = _make_palette(level)

    # --- Determine chunk range ---
    mc_width  = grid_cols * args.scale
    mc_depth  = grid_rows * args.scale
    cx_min, cx_max = 0, ceil(mc_width  / 16)
    cz_min, cz_max = 0, ceil(mc_depth  / 16)
    total_chunks = (cx_max - cx_min) * (cz_max - cz_min)

    print(f"World size: {mc_width} x {mc_depth} blocks  ({total_chunks} chunks)")
    print(f"Scale: --scale {args.scale}  --vscale {args.vscale}  --biomes {args.biomes}")
    print(f"Generating terrain...")

    with tqdm(total=total_chunks, unit="chunk") as pbar:
        for cz in range(cz_min, cz_max):
            for cx in range(cx_min, cx_max):
                chunk = generate_chunk(
                    cx, cz, grid,
                    scale=args.scale,
                    vscale=args.vscale,
                    block_uni=block_uni,
                    biome_mode=args.biomes,
                    water_mask=water_mask,
                    mc_width=mc_width,
                    mc_depth=mc_depth,
                )
                level.put_chunk(chunk, DIMENSION)
                pbar.update(1)

    # --- Save ---
    print("Saving world...")
    level.save()
    level.close()

    # --- Entity storage (required by Minecraft 1.17+) ---
    write_entity_files(out_path, cx_min, cx_max, cz_min, cz_max)

    # --- Patch level.dat (fix WorldGenSettings + spawn) ---
    # Spawn at the centre of the map, 1 block above the actual surface there
    centre_col = grid_cols // 2
    centre_row = grid_rows // 2
    centre_elev = float(grid[centre_row, centre_col])
    spawn_x = (centre_col * args.scale) + (args.scale // 2)
    spawn_z = (centre_row * args.scale) + (args.scale // 2)
    spawn_y = MAP_ZERO_Y + round(centre_elev * args.vscale) + 1
    patch_level_dat(out_path, world_name, spawn_x, spawn_y, spawn_z)

    # --- Summary ---
    region_files = glob.glob(os.path.join(out_path, "region", "*.mca"))
    elev_min = float(np.min(grid))
    elev_max = float(np.max(grid))
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
