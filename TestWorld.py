#!/usr/bin/env python3
"""
TestWorld.py - Diagnostic world with a 384-block-tall stone tower.

Reuses generate.py's save pipeline (AnvilFormat + Chunk + entity files + level.dat
patch) with the simplest possible content: bedrock, a ground plain at Y=63, and a
single-column stone tower from Y=-64 all the way to Y=319 with gold_block markers
every 32 blocks.

If the tower ends before Y=319 in Minecraft, something in our pipeline or amulet
is dropping sections above a certain index. The gold markers make it easy to spot
where the cutoff is.

Usage:
    python TestWorld.py [--out PATH]

Spawn is next to the tower. Fly straight up along the stone column.
"""

import argparse
import os
import shutil
import sys

import amulet
import numpy as np
from amulet.api.block import Block
from amulet.api.chunk import Chunk
from amulet.level.formats.anvil_world import AnvilFormat

sys.path.insert(0, os.path.dirname(__file__))
from minecraft_uk.minecraft.constants import (
    ARRAY_HEIGHT,
    ARRAY_OFFSET,
    DIMENSION,
    MC_VERSION,
    MC_VERSION_ID,
    Y_MAX,
    Y_MIN,
)
from minecraft_uk.minecraft.palettes import make_block_palette
from minecraft_uk.minecraft.world import patch_level_dat, write_entity_files


def main():
    parser = argparse.ArgumentParser(
        description="Build a Y=-64..319 stone tower for diagnostic purposes.",
    )
    parser.add_argument("--out", default=None, help="Output world folder")
    args = parser.parse_args()

    out_path = args.out or os.path.join(os.path.dirname(__file__), "worlds", "TestWorld_Tower")
    out_path = os.path.abspath(out_path)

    if os.path.exists(out_path):
        print(f"Removing existing world at {out_path}")
        shutil.rmtree(out_path)
    os.makedirs(out_path, exist_ok=True)

    print(f"Creating world at {out_path}")
    fmt = AnvilFormat(out_path)
    fmt.create_and_open(MC_VERSION_ID, MC_VERSION, overwrite=True)
    fmt.close()

    # Patch level.dat BEFORE load_level so amulet reads a valid overworld
    # dimension type and sets bounds to Y=-64..319. Otherwise it falls back to
    # DefaultSelection (Y=0..256) and truncates every chunk to 16 sections.
    patch_level_dat(out_path, "TestWorld_Tower", 10, 65, 8, void=False)

    level = amulet.load_level(out_path)
    block_uni = make_block_palette(level)

    # Resolve a gold_block universal for the markers
    ver = level.level_wrapper.translation_manager.get_version(MC_VERSION_ID, MC_VERSION)
    gold_uni, _, _ = ver.block.to_universal(Block("minecraft", "gold_block"))

    cx_range = range(0, 2)
    cz_range = range(0, 2)

    marker_ys = sorted({Y_MIN, -32, 0, 32, 63, 64, 100, 128, 160, 192, 224,
                        255, 256, 288, 300, 319})
    print(f"Gold markers at Y: {marker_ys}")

    for cx in cx_range:
        for cz in cz_range:
            chunk = Chunk(cx, cz)
            pal = chunk.block_palette
            ids = {name: pal.get_add_block(blk) for name, blk in block_uni.items()}
            gold_id = pal.get_add_block(gold_uni)

            col_blocks = np.zeros((16, ARRAY_HEIGHT, 16), dtype=np.uint32)

            # Ground plain: bedrock at Y=-64, stone fill, grass at Y=63
            for lx in range(16):
                for lz in range(16):
                    col_blocks[lx, 0, lz] = ids["bedrock"]
                    col_blocks[lx, 1:127, lz] = ids["stone"]
                    col_blocks[lx, 127, lz] = ids["grass"]

            # Tower at local (8, 8) of chunk (0, 0): stone from Y=-63 to Y=319
            if cx == 0 and cz == 0:
                col_blocks[8, 1:ARRAY_HEIGHT, 8] = ids["stone"]
                for my in marker_ys:
                    arr = my + ARRAY_OFFSET
                    if 0 <= arr < ARRAY_HEIGHT:
                        col_blocks[8, arr, 8] = gold_id

            added = []
            for si in range(-4, 20):
                arr_start = si * 16 + ARRAY_OFFSET
                arr_end   = arr_start + 16
                if arr_end <= 0 or arr_start >= ARRAY_HEIGHT:
                    continue
                sec = col_blocks[:, arr_start:arr_end, :]
                if np.any(sec != 0):
                    chunk.blocks.add_section(si, sec)
                    added.append(si)

            chunk.biomes.convert_to_3d()
            biome_id = chunk.biome_palette.get_add_biome("minecraft:plains")
            for si in range(-4, 20):
                chunk.biomes.add_section(si, np.full((4, 4, 4), biome_id, dtype=np.uint32))

            level.put_chunk(chunk, DIMENSION)
            if cx == 0 and cz == 0:
                print(f"Chunk (0,0) block sections added: {added}")

    print("Saving...")
    level.save()
    level.close()

    write_entity_files(out_path, 0, 2, 0, 2)

    print()
    print(f"Done. Copy {out_path} into your Minecraft saves folder.")
    print("Tower is at block (8, *, 8); spawn is 2 blocks east at (10, 65, 8).")
    print("Fly straight up the tower - it should reach Y=319 with a gold block there.")
    print("If the tower ends early, the highest visible gold marker tells you where.")


if __name__ == "__main__":
    main()
