"""Block and biome palettes, plus helpers that map elevation to palette entries.

Edit `THRESHOLDS` / `BIOME_THRESHOLDS` here to change world appearance.
"""

import amulet
from amulet.api.block import Block

from .constants import MC_VERSION, MC_VERSION_ID


# (min_elev_m, surface_name, surface_props, subsurface_name)
THRESHOLDS = [
    (1100, "snow_block",   {},                          "stone"),
    ( 600, "stone",        {},                          "stone"),
    (  10, "grass_block",  {"snowy": "false"},          "dirt"),
    (   0, "sand",         {},                          "sandstone"),
    (None, "gravel",       {},                          "stone"),   # sea floor (elev <= 0)
]

BIOME_THRESHOLDS = [
    (1100, "minecraft:frozen_peaks"),
    ( 600, "minecraft:stony_peaks"),
    ( 300, "minecraft:windswept_hills"),
    (   0, "minecraft:plains"),
    (None, "minecraft:ocean"),
]

_SURFACE_KEY = {
    "snow_block":  "snow",
    "stone":       "stone",
    "grass_block": "grass",
    "sand":        "sand",
    "gravel":      "gravel",
}


def make_block_palette(level):
    """Return {name: universal Block} for every block the chunk generator uses."""
    ver = level.level_wrapper.translation_manager.get_version(MC_VERSION_ID, MC_VERSION)

    def to_uni(name, str_props=None):
        props = {k: amulet.StringTag(v) for k, v in (str_props or {}).items()}
        u, _, _ = ver.block.to_universal(Block("minecraft", name, props))
        return u

    return {
        "air":       to_uni("air"),
        "bedrock":   to_uni("bedrock"),
        "stone":     to_uni("stone"),
        "dirt":      to_uni("dirt"),
        "grass":     to_uni("grass_block", {"snowy": "false"}),
        "sand":      to_uni("sand"),
        "sandstone": to_uni("sandstone"),
        "gravel":    to_uni("gravel"),
        "snow":      to_uni("snow_block"),
        "water":     to_uni("water", {"level": "0"}),
        "bricks":    to_uni("bricks"),
    }


def surface_and_sub(elev_m):
    """Return (surface_palette_key, sub_block_key) for a given elevation."""
    for threshold, surf, _, sub in THRESHOLDS:
        if threshold is None or elev_m >= threshold:
            return _SURFACE_KEY[surf], sub
    return "gravel", "stone"


def biome_name(elev_m):
    """Return the biome name for a given elevation."""
    for threshold, biome in BIOME_THRESHOLDS:
        if threshold is None or elev_m >= threshold:
            return biome
    return "minecraft:ocean"
