"""Amulet chunk generation from OS elevation + feature masks."""

import numpy as np
from amulet.api.chunk import Chunk

from ..osdata.features import BUILDING_SUBCELL_M
from ..osdata.tiles import CELL_SIZE_M
from .constants import (
    ARRAY_HEIGHT,
    ARRAY_OFFSET,
    MAP_ZERO_Y,
    SEA_SURFACE_Y,
    Y_MAX,
    Y_MIN,
)
from .palettes import biome_name, surface_and_sub


WATER_MASK_THRESHOLD   = 0.50  # density threshold that turns a block into water at scale > 1
BUILDING_HEIGHT_BLOCKS = 3     # stack of 'bricks' blocks placed above the surface


def generate_chunk(cx, cz, tile_grid, tile_origin, scale, vscale, block_uni, biome_mode,
                   water_mask=None, water_density=None,
                   building_mask=None,
                   world_rows=None, world_cols=None,
                   mc_width=None, mc_depth=None):
    """Build and return an amulet Chunk for chunk coordinates (cx, cz).

    `tile_grid` is the halo-inclusive elevation grid for the owning tile;
    `tile_origin = (row, col)` is the global cell index of tile_grid[0, 0]. The
    halo must be wide enough to cover every cell this chunk reads — see
    chunks_owned_by_tile / load_tile_with_halo.

    `water_mask` / `water_density` share tile_grid's shape. `building_mask` is
    K-times denser (K = CELL_SIZE_M // BUILDING_SUBCELL_M) and covers the same
    halo-inclusive extent.

    `world_rows` / `world_cols` are the global cell dimensions, used to mark
    blocks past the world edge as elev=0. `mc_width` / `mc_depth` are global
    block dimensions used for the perimeter rim.
    """
    chunk = Chunk(cx, cz)
    pal = chunk.block_palette

    ids = {name: pal.get_add_block(blk) for name, blk in block_uni.items()}
    BEDROCK_ID = ids["bedrock"]
    STONE_ID   = ids["stone"]
    DIRT_ID    = ids["dirt"]
    SAND_ID    = ids["sand"]
    SANDS_ID   = ids["sandstone"]
    GRAVEL_ID  = ids["gravel"]
    SNOW_ID    = ids["snow"]
    GRASS_ID   = ids["grass"]
    WATER_ID   = ids["water"]
    BRICKS_ID  = ids["bricks"]

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

    t_row0, t_col0 = tile_origin
    tg_rows, tg_cols = tile_grid.shape
    w_rows = world_rows if world_rows is not None else tg_rows
    w_cols = world_cols if world_cols is not None else tg_cols
    K_BLD = CELL_SIZE_M // BUILDING_SUBCELL_M

    # Full-height column array: shape (16, ARRAY_HEIGHT, 16). 0 = air.
    col_blocks = np.zeros((16, ARRAY_HEIGHT, 16), dtype=np.uint32)
    biome_elevs = np.zeros((16, 16), dtype=np.float32)

    for lx in range(16):
        for lz in range(16):
            gx = cx * 16 + lx
            gz = cz * 16 + lz

            cell_col = gx // scale
            cell_row = gz // scale

            in_bounds = 0 <= cell_row < w_rows and 0 <= cell_col < w_cols

            if not in_bounds:
                elev = 0.0
                is_mask_water_cell = False
            elif scale > 1:
                # Bilinear sampling for both elevation and water mask so neither
                # shows a 2x2/4x4 grid pattern at cell boundaries.
                fx = (gx + 0.5) / scale - 0.5
                fz = (gz + 0.5) / scale - 0.5
                c0 = int(np.floor(fx)); r0 = int(np.floor(fz))
                tx = fx - c0;           tz = fz - r0
                c0c = max(0, min(w_cols - 1, c0))     - t_col0
                c1c = max(0, min(w_cols - 1, c0 + 1)) - t_col0
                r0c = max(0, min(w_rows - 1, r0))     - t_row0
                r1c = max(0, min(w_rows - 1, r0 + 1)) - t_row0
                w00 = (1 - tx) * (1 - tz)
                w01 = tx       * (1 - tz)
                w10 = (1 - tx) * tz
                w11 = tx       * tz
                elev = float(
                    w00 * tile_grid[r0c, c0c] + w01 * tile_grid[r0c, c1c]
                    + w10 * tile_grid[r1c, c0c] + w11 * tile_grid[r1c, c1c]
                )
                if water_density is not None:
                    wm = (w00 * water_density[r0c, c0c] + w01 * water_density[r0c, c1c]
                          + w10 * water_density[r1c, c0c] + w11 * water_density[r1c, c1c])
                    is_mask_water_cell = wm >= WATER_MASK_THRESHOLD
                else:
                    is_mask_water_cell = False
            else:
                lr = cell_row - t_row0
                lc = cell_col - t_col0
                elev = float(tile_grid[lr, lc])
                is_mask_water_cell = (
                    water_mask is not None and bool(water_mask[lr, lc])
                )

            # Buildings use a separate sub-cell mask. Nearest-neighbour lookup
            # per block — no bilinear blur, so isolated buildings stay crisp.
            is_mask_building_cell = False
            if in_bounds and building_mask is not None:
                mr, mc = building_mask.shape
                sub_col = int((gx + 0.5) * CELL_SIZE_M / (scale * BUILDING_SUBCELL_M)) - t_col0 * K_BLD
                sub_row = int((gz + 0.5) * CELL_SIZE_M / (scale * BUILDING_SUBCELL_M)) - t_row0 * K_BLD
                if 0 <= sub_row < mr and 0 <= sub_col < mc:
                    is_mask_building_cell = bool(building_mask[sub_row, sub_col])

            biome_elevs[lx, lz] = elev

            y_surf = int(round(MAP_ZERO_Y + elev * vscale))
            y_surf = max(Y_MIN, min(Y_MAX, y_surf))
            # Force sea floor at least one block below sea surface so water can sit on top.
            if elev <= 0 and y_surf > SEA_SURFACE_Y - 1:
                y_surf = SEA_SURFACE_Y - 1
            arr_surf = y_surf + ARRAY_OFFSET

            col_blocks[lx, 0, lz] = BEDROCK_ID

            stone_end = max(1, arr_surf - 3)
            if stone_end > 1:
                col_blocks[lx, 1:stone_end, lz] = STONE_ID

            surf_key, sub_key = surface_and_sub(elev)
            sub_id  = sub_ids.get(sub_key, STONE_ID)
            surf_id = surf_ids.get(surf_key, GRASS_ID)

            sub_start = max(1, arr_surf - 3)
            sub_end   = arr_surf
            if sub_end > sub_start:
                col_blocks[lx, sub_start:sub_end, lz] = sub_id

            if 0 <= arr_surf < ARRAY_HEIGHT:
                col_blocks[lx, arr_surf, lz] = surf_id

            if elev <= 0 and arr_surf < SEA_SURFACE_Y + ARRAY_OFFSET:
                water_start = arr_surf + 1
                water_end   = SEA_SURFACE_Y + ARRAY_OFFSET + 1
                if water_end > water_start:
                    col_blocks[lx, water_start:water_end, lz] = WATER_ID

            if (is_mask_water_cell
                    and elev > 0
                    and 0 <= arr_surf < ARRAY_HEIGHT):
                col_blocks[lx, arr_surf, lz] = WATER_ID

            if (is_mask_building_cell
                    and not is_mask_water_cell
                    and elev > 0):
                bld_start = arr_surf + 1
                bld_end   = min(ARRAY_HEIGHT, bld_start + BUILDING_HEIGHT_BLOCKS)
                if bld_end > bld_start:
                    col_blocks[lx, bld_start:bld_end, lz] = BRICKS_ID

            if (mc_width is not None and mc_depth is not None
                    and (gx == 0 or gx == mc_width - 1
                         or gz == 0 or gz == mc_depth - 1)):
                is_mask_water = is_mask_water_cell and elev > 0
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

    for si in range(-4, 20):   # sections covering Y -64..319
        arr_start = si * 16 + ARRAY_OFFSET
        arr_end   = arr_start + 16
        if arr_end <= 0 or arr_start >= ARRAY_HEIGHT:
            continue
        section_data = col_blocks[:, arr_start:arr_end, :]
        if np.any(section_data != 0):
            chunk.blocks.add_section(si, section_data)

    if biome_mode == "elevation":
        chunk.biomes.convert_to_3d()
        avg_elev = float(np.mean(biome_elevs))
        biome_id = chunk.biome_palette.get_add_biome(biome_name(avg_elev))

        for si in range(-4, 20):
            sec = np.full((4, 4, 4), biome_id, dtype=np.uint32)
            chunk.biomes.add_section(si, sec)

    return chunk
