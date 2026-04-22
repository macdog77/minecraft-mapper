"""Minecraft-specific constants used across the world-writing pipeline."""

MC_VERSION      = (1, 21, 0)
MC_VERSION_ID   = "java"
DIMENSION       = "minecraft:overworld"

MAP_ZERO_Y      = 64    # Minecraft Y that corresponds to 0 m OS elevation
SEA_SURFACE_Y   = 63    # highest water block (one below MAP_ZERO_Y)
Y_MIN           = -64   # Minecraft bedrock level
Y_MAX           = 319   # Minecraft build limit

ARRAY_OFFSET    = -Y_MIN              # array index = Y + ARRAY_OFFSET
ARRAY_HEIGHT    = Y_MAX - Y_MIN + 1   # 384
