"""level.dat patching and entity-file writing for Minecraft 1.21 worlds.

Amulet writes a minimal world that Minecraft 1.21 will reject or corrupt.
These helpers work around that:

1. `patch_level_dat` rewrites level.dat after `level.save()` to add a valid
   WorldGenSettings, Version compound, spawn coords, and creative flags.
2. `write_entity_files` writes `entities/r.X.Z.mca` region files by hand,
   because amulet does not produce entity files and Minecraft 1.17+ refuses
   to load any chunk without them.
"""

import os
import struct
import zlib

import amulet_nbt


def _void_flat_generator(biome="minecraft:plains"):
    """Return a flat/void generator compound.

    Triggers Minecraft's "Worlds using Experimental Settings are not
    supported" warning on load because inline flat-generator settings are
    treated as custom worldgen. Only used when the user opts in via --void.
    """
    return amulet_nbt.CompoundTag({
        "type": amulet_nbt.StringTag("minecraft:flat"),
        "settings": amulet_nbt.CompoundTag({
            "biome": amulet_nbt.StringTag(biome),
            "features": amulet_nbt.ByteTag(0),
            "lakes": amulet_nbt.ByteTag(0),
            "layers": amulet_nbt.ListTag([
                amulet_nbt.CompoundTag({
                    "block":  amulet_nbt.StringTag("minecraft:air"),
                    "height": amulet_nbt.IntTag(1),
                })
            ]),
        }),
    })


def _vanilla_generator(dim):
    """Return the vanilla noise generator for a standard dimension.

    Uses preset references so WorldGenSettings exactly matches the shape
    Minecraft writes for a default-created world. Any inline custom worldgen
    is treated by Minecraft's Codec as a custom dimension and permanently
    flags the world as experimental — surfacing the "Experimental Settings"
    warning on every load.

    Tradeoff: chunks beyond the pre-filled OS area will fill with vanilla
    terrain instead of void. The pre-filled chunks load as-is from the
    region files and are unaffected.
    """
    if dim == "minecraft:overworld":
        return amulet_nbt.CompoundTag({
            "type": amulet_nbt.StringTag("minecraft:noise"),
            "biome_source": amulet_nbt.CompoundTag({
                "type":   amulet_nbt.StringTag("minecraft:multi_noise"),
                "preset": amulet_nbt.StringTag("minecraft:overworld"),
            }),
            "settings": amulet_nbt.StringTag("minecraft:overworld"),
        })
    if dim == "minecraft:the_nether":
        return amulet_nbt.CompoundTag({
            "type": amulet_nbt.StringTag("minecraft:noise"),
            "biome_source": amulet_nbt.CompoundTag({
                "type":   amulet_nbt.StringTag("minecraft:multi_noise"),
                "preset": amulet_nbt.StringTag("minecraft:nether"),
            }),
            "settings": amulet_nbt.StringTag("minecraft:nether"),
        })
    if dim == "minecraft:the_end":
        return amulet_nbt.CompoundTag({
            "type": amulet_nbt.StringTag("minecraft:noise"),
            "biome_source": amulet_nbt.CompoundTag({
                "type": amulet_nbt.StringTag("minecraft:the_end"),
            }),
            "settings": amulet_nbt.StringTag("minecraft:end"),
        })
    raise ValueError(f"Unknown dimension: {dim}")


def patch_level_dat(world_path, world_name, mc_x, mc_y, mc_z, void=False):
    """Replace the minimal amulet-generated level.dat with a complete Minecraft 1.21 one.

    void=False (default): references the vanilla noise generators so
    Minecraft treats the world as a standard one (no experimental warning).
    Chunks beyond the pre-filled area get vanilla terrain.

    void=True: writes inline flat-void generators so areas beyond the
    pre-filled map stay as void. Minecraft will show the
    "Worlds using Experimental Settings are not supported" warning.
    """
    dat_path = os.path.join(world_path, "level.dat")
    nbt = amulet_nbt.load(dat_path)
    data = nbt.tag["Data"]

    data["SpawnX"] = amulet_nbt.IntTag(mc_x)
    data["SpawnY"] = amulet_nbt.IntTag(mc_y)
    data["SpawnZ"] = amulet_nbt.IntTag(mc_z)
    data["SpawnAngle"] = amulet_nbt.FloatTag(0.0)

    data["LevelName"] = amulet_nbt.StringTag(world_name)
    data["GameType"]  = amulet_nbt.IntTag(1)      # 1 = creative
    data["Difficulty"] = amulet_nbt.ByteTag(2)     # 2 = normal
    data["allowCommands"] = amulet_nbt.ByteTag(1)
    data["hardcore"]  = amulet_nbt.ByteTag(0)
    data["initialized"] = amulet_nbt.ByteTag(1)
    data["DayTime"]   = amulet_nbt.LongTag(6000)
    data["Time"]      = amulet_nbt.LongTag(0)
    data["rainTime"]  = amulet_nbt.IntTag(0)
    data["raining"]   = amulet_nbt.ByteTag(0)
    data["thunderTime"] = amulet_nbt.IntTag(0)
    data["thundering"] = amulet_nbt.ByteTag(0)

    # DataVersion 3953 = Java 1.21.0. Minecraft will auto-upgrade the world data.
    data["Version"] = amulet_nbt.CompoundTag({
        "Id":       amulet_nbt.IntTag(3953),
        "Name":     amulet_nbt.StringTag("1.21"),
        "Series":   amulet_nbt.StringTag("main"),
        "Snapshot": amulet_nbt.ByteTag(0),
    })

    # Force vanilla-only DataPacks. Amulet's default enables experimental
    # datapacks (bundle, trade_rebalance, etc.) which trigger the
    # "Experimental Settings" warning. The empty Disabled list must be typed
    # as string (tag id 8); untyped empty ListTag defaults to byte (1) which
    # Minecraft's DataPacks codec rejects.
    data["DataPacks"] = amulet_nbt.CompoundTag({
        "Enabled":  amulet_nbt.ListTag([amulet_nbt.StringTag("vanilla")]),
        "Disabled": amulet_nbt.ListTag([], 8),
    })
    data["enabled_features"] = amulet_nbt.ListTag(
        [amulet_nbt.StringTag("minecraft:vanilla")]
    )

    if void:
        overworld_gen = _void_flat_generator("minecraft:plains")
        nether_gen    = _void_flat_generator("minecraft:nether_wastes")
        end_gen       = _void_flat_generator("minecraft:the_end")
    else:
        overworld_gen = _vanilla_generator("minecraft:overworld")
        nether_gen    = _vanilla_generator("minecraft:the_nether")
        end_gen       = _vanilla_generator("minecraft:the_end")

    data["WorldGenSettings"] = amulet_nbt.CompoundTag({
        "bonus_chest":       amulet_nbt.ByteTag(0),
        "generate_features": amulet_nbt.ByteTag(0),
        "seed":              amulet_nbt.LongTag(0),
        "dimensions": amulet_nbt.CompoundTag({
            "minecraft:overworld": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:overworld"),
                "generator": overworld_gen,
            }),
            "minecraft:the_nether": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:the_nether"),
                "generator": nether_gen,
            }),
            "minecraft:the_end": amulet_nbt.CompoundTag({
                "type":      amulet_nbt.StringTag("minecraft:the_end"),
                "generator": end_gen,
            }),
        }),
    })

    nbt.save_to(dat_path)
    print(f"Spawn set: X={mc_x} Y={mc_y} Z={mc_z}")


def write_entity_files(world_path, cx_min, cx_max, cz_min, cz_max):
    """Create entities/<r.X.Z.mca> files for every chunk in the generated range.

    Minecraft 1.17+ stores entity data separately from chunk data; without
    these files every chunk fails to load with NoSuchElementException in
    EntityStorage. Each chunk entry is an NBT compound with DataVersion,
    Position, and an empty Entities list — the minimum Minecraft needs to
    not error.
    """
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

        location_table  = bytearray(4096)
        timestamp_table = bytearray(4096)
        sector_data     = bytearray()

        sector_offset = 2  # first two sectors are the header tables

        for cx, cz in chunks:
            local_cx = cx & 0x1f
            local_cz = cz & 0x1f
            table_idx = local_cz * 32 + local_cx

            nbt = amulet_nbt.NamedTag(
                amulet_nbt.CompoundTag({
                    "DataVersion": amulet_nbt.IntTag(3953),
                    "Position":   amulet_nbt.IntArrayTag([cx, cz]),
                    "Entities":   amulet_nbt.ListTag([]),
                })
            )
            raw_nbt    = nbt.to_nbt(compressed=False, little_endian=False)
            compressed = zlib.compress(raw_nbt)

            # Chunk payload: 4-byte length + 1-byte compression type + data
            payload = struct.pack(">I", len(compressed) + 1) + b"\x02" + compressed

            # Pad to 4096-byte sector boundary
            pad = (4096 - len(payload) % 4096) % 4096
            payload += b"\x00" * pad
            sectors = len(payload) // 4096

            loc_bytes = struct.pack(">I", (sector_offset << 8) | sectors)
            location_table[table_idx * 4:(table_idx + 1) * 4] = loc_bytes

            sector_data += payload
            sector_offset += sectors

        with open(out, "wb") as f:
            f.write(bytes(location_table))
            f.write(bytes(timestamp_table))
            f.write(bytes(sector_data))

    print(f"Entity files written: {len(regions)} region(s) in {entities_dir}")
