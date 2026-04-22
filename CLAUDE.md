# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Python scripts that convert Ordnance Survey Terrain 50 elevation data (and OS raster map tiles) into Minecraft Java Edition 1.21 worlds, 3D OBJ meshes, or greyscale heightmap PNGs.

## Environment

- Python 3.10+
- Install: `pip install amulet-core tqdm numpy pillow pyproj mapbox-vector-tile`
- `mapbox-vector-tile` is only required when `generate.py` reads rivers (`OS Map Data/rivers/Data/oprvrs_gb.mbtiles`); without it, rivers are silently skipped.
- `amulet-core` 1.9.x is the Minecraft world writer; it is sensitive to Minecraft version and writes an incomplete `level.dat` that `minecraft_uk.minecraft.world.patch_level_dat` fixes afterwards (see below).
- No build step, no lint config, no test suite. Changes are validated by running the scripts end-to-end — typically starting with a single 10 km tile (`generate.py "OS Map Data/data/nt/nt27_*.zip"`) because full-region/full-UK runs are slow. Memory is bounded per-tile thanks to the streaming pipeline (see below), so overnight whole-UK runs are feasible.

## Common commands

```bash
python generate.py "<tile.zip | region_dir | data_root>" [--scale N] [--vscale F] [--biomes elevation|default] [--no-water] [--halo N] [--flush-every N] [--out PATH]
python mesh.py     "<tile.zip | region_dir | data_root>" [--vscale F] [--step N] [--texture N] [--out PATH]
python locate.py   <lat> <lon>                             # WGS84 → BNG tile code + path
python heightmap.py <tile.zip|asc> [max_elev_m] [--tiff]   # single-tile PNG; --tiff overlays the OS raster at 50%
python stitch.py   <region_dir> [max_elev_m] [--tiff] [--grid]   # whole-region PNG; layered heightmap → tiff @ 33% → grid
```

`readme.md` is the user-facing documentation; keep its option tables and examples in sync with script behaviour.

## Package layout

Library code lives under `minecraft_uk/`; top-level scripts at the repo root are thin CLI wrappers that import from it. Standard Python package layout — no `sys.path` hacks (a single `sys.path.insert(0, os.path.dirname(__file__))` in each CLI lets `python mesh.py` work from any cwd; remove it and scripts still work when launched from the repo root).

```
minecraft_uk/
├── common/
│   └── paths.py              # REPO_ROOT, DATA_ROOT, TILES_ROOT, RIVERS_PATH, WORLDS_DIR, MESHES_DIR
├── osdata/
│   ├── asc.py                # parse_asc, read_asc_from_zip/file, load_tile (CLI helper)
│   ├── bng.py                # wgs84_to_bng, bng_to_tile, region_origin, square_code_at, find_tile_zip
│   ├── discovery.py          # discover_zips, find_tiles_by_pos, TILE_NAME_RE
│   ├── tiles.py              # CELL_SIZE_M, scan_headers, compute_global_extent, tile_global_offset,
│   │                         # build_tile_index, load_tile_elev, load_tile_with_halo,
│   │                         # chunks_owned_by_tile, resolve_spawn_elev
│   ├── tiff.py               # QUADRANTS, TILE_PX, SEA_COLOUR, find_tiffs, is_water_color,
│   │                         # is_building_color, tile_palette_fraction, iter_neighbour_tiffs,
│   │                         # TIFF_* thresholds
│   ├── features.py           # flat_area_mask, water_mask_for_tile, building_mask_for_tile,
│   │                         # compute_water_density, FLAT_*, BUILDING_SUBCELL_M, MIN_BUILDING_SCALE
│   └── rivers.py             # rasterize_rivers_for_tile, draw_line_cells
├── minecraft/
│   ├── constants.py          # MC_VERSION, DIMENSION, MAP_ZERO_Y, SEA_SURFACE_Y, Y_MIN, Y_MAX,
│   │                         # ARRAY_OFFSET, ARRAY_HEIGHT
│   ├── palettes.py           # THRESHOLDS, BIOME_THRESHOLDS, make_block_palette,
│   │                         # surface_and_sub, biome_name
│   ├── chunks.py             # generate_chunk, WATER_MASK_THRESHOLD, BUILDING_HEIGHT_BLOCKS
│   └── world.py              # patch_level_dat, write_entity_files, _void_flat_generator,
│                             # _vanilla_generator
└── rendering/
    ├── heightmap.py          # make_heightmap, overlay_tiff, BEN_NEVIS_M, TIFF_OPACITY
    └── mesh.py               # stitch_texture, write_mtl, stream_obj, WRITE_BATCH
```

### Dependency direction

`rendering` and `minecraft` → `osdata` → `common`. Never upward.

`osdata` has **no** amulet import. This means `mesh.py`, `heightmap.py`, `stitch.py`, `max_vscale.py`, and `uk_map.py` do not pull in amulet or PyMCTranslate (~1 s of startup noise on stdout). Only `generate.py` and `TestWorld.py` touch amulet.

## Streaming pipeline (`generate.py` + `mesh.py`)

Both CLIs process one 10 km tile at a time rather than stitching the whole region into memory. This is the only reason whole-UK runs fit in RAM.

- **Ownership rule:** a Minecraft chunk is emitted by exactly one tile — whichever tile's core contains the chunk's SW block corner `(cx*16, cz*16)`. `osdata.tiles.chunks_owned_by_tile(core_row0, core_col0, nrows, ncols, scale)` yields these without overlap or gaps. Do not iterate chunks globally and look up their owner; always iterate tiles in the outer loop.
- **Halo:** each tile is loaded with a ring of `halo` cells copied from its 8 neighbours so edge effects (bilinear sampling at `--scale > 1`, the 5×5 flat-area detector, and chunk blocks that spill past the tile edge) don't seam at tile boundaries. Default halo is `ceil(16 / scale) + 3` (= 19 at scale 1, 5 at scale 8). `--halo` overrides it.
- **`tile_origin` convention:** `minecraft.chunks.generate_chunk(..., tile_origin=(row, col), ...)` passes the global cell index of `tile_grid[0, 0]`, i.e. `(core_row0 - halo, core_col0 - halo)`. All masks (water, water_density, building) are sized to match `tile_grid`.
- **Amulet flush:** after each batch of `--flush-every N` tiles (default 1), `generate.main()` calls `level.save()` then `level.unload()` to persist chunks and drop amulet's in-memory cache. Without this, chunks accumulate until the end and OOM.
- **LRU cache:** `osdata.tiles.load_tile_elev` is `@lru_cache(maxsize=16)`. Halo reads from a neighbour don't re-parse its zip when the neighbour later becomes the core tile.
- **Per-tile masks:** `osdata.features.water_mask_for_tile` / `building_mask_for_tile` / `osdata.rivers.rasterize_rivers_for_tile` all walk the same tile + 8 neighbours so halo masks stay seamless across tiles. `rasterize_rivers_for_tile` accepts an optional `conn` to reuse a single SQLite connection across tiles.
- **Mesh streaming:** `rendering.mesh.stream_obj` emits vertices + faces tile-by-tile. Each tile uses `load_tile_with_halo(halo=1)` and outputs its core cells plus a 1-cell east/south border (only if a neighbour exists). Border vertices are duplicated between adjacent tiles (cheap) but face count is exact; no two-pass vertex-index lookup required.

## Grid and coordinate conventions

These conventions are consistent across all scripts; breaking them produces silently flipped or offset output:

- Elevation grid: `grid[row, col]`, row 0 = northernmost, col 0 = westernmost. Matches ASC file ordering, so no flip is needed when loading.
- OS cell size: `osdata.tiles.CELL_SIZE_M = 50` (50 m × 50 m per cell, 200 × 200 cells per 10 km tile).
- BNG: EPSG:27700 easting/northing in metres; `compute_global_extent` returns `(min_east, max_north, total_rows, total_cols)` — note the **top** (north) edge, not the SW corner used inside individual ASC headers. `tile_global_offset(hdr, min_east, max_north)` converts a tile header into its `(row_offset, col_offset)` in the global grid.
- Minecraft mapping (in `minecraft.chunks.generate_chunk`): `Y = MAP_ZERO_Y + round(elev_m × vscale)` where `MAP_ZERO_Y = 64`, `SEA_SURFACE_Y = 63`. Sea floors are forced at least one block below `SEA_SURFACE_Y` so water has somewhere to sit. The Y range used is `[-64, 319]` with `ARRAY_OFFSET = 64`.
- Mesh axes (in `rendering.mesh.stream_obj`): X = east, Y = elevation (× vscale), Z = south.

## OS dataset layout

Paths follow a fixed convention (see `OSData.md`):

```
OS Map Data/data/<region>/<tile>.zip          # lowercase region; .asc elevation inside zip
OS Map Data/tiles/<REGION>/<tile><QUAD>.tif   # UPPERCASE region; 5000×5000 palette-indexed TIFF
```

Each elevation `.zip` covers 10 km × 10 km (50 m/cell). Each TIFF covers one 5 km quadrant (NW/NE/SW/SE) at 1 m/px. Five TIFFs have no matching elevation tile (listed in `OSData.md`); never assume 1:1 coverage.

All BNG letter-table lookups (`_MAJOR`, `MINOR_LETTERS`, `_MAJOR_REVERSE`) live in `minecraft_uk.osdata.bng` — single source of truth for `locate.py`, `mesh.py`, `uk_map.py`.

## Minecraft world-writing quirks (`minecraft_uk.minecraft.world`)

Amulet writes a minimal world that Minecraft 1.21 will reject or corrupt. `minecraft_uk.minecraft.world` works around this in three places — do not remove these without understanding why:

1. **`patch_level_dat`** rewrites `level.dat` after `level.save()` to add a valid `WorldGenSettings` (vanilla preset generators by default, void-flat only when the user passes `--void`), a `Version` compound with `DataVersion 3953` (Java 1.21.0), spawn coords, and creative/allowCommands flags. The default (vanilla preset) avoids Minecraft's "Experimental Settings" warning on load.
2. **`write_entity_files`** manually writes `entities/r.X.Z.mca` region files containing a minimal NBT per chunk (DataVersion, Position, empty Entities list). Without these, Minecraft 1.17+ throws `NoSuchElementException` in `EntityStorage` and refuses to load any chunk. This code writes the Anvil region format directly (4096-byte sectors, location+timestamp tables, zlib-compressed payloads) because Amulet does not produce entity files.
3. **Water handling** (`osdata.features.water_mask_for_tile`) is a two-signal OR: blue-pixel fraction per cell from OS raster TIFFs (`osdata.tiff.tile_water_fraction`) plus a flat-elevation detector (`osdata.features.flat_area_mask` — lochs/reservoirs report a single elevation over a 5×5 window). A perimeter stone rim is added by `minecraft.chunks.generate_chunk` wherever water touches the world edge (checked against `mc_width`/`mc_depth`) to stop it leaking into the void beyond the OS coverage area.

The block/biome palettes live in `minecraft_uk.minecraft.palettes` as `THRESHOLDS` and `BIOME_THRESHOLDS` lists of `(min_elev_m, ...)` tuples with a sentinel `None` threshold for the catch-all. Edit these to change the appearance.

## `mesh.py` specifics

- `rendering.mesh.stitch_texture` composites TIFF quadrants onto a single PIL canvas, then downsamples to `--texture` max-dim; sea gaps are filled with `osdata.tiff.SEA_COLOUR`.
- `stream_obj` writes `v`/`vt`/`f` lines tile-by-tile in 50k-line batches via `f.writelines`. Vertices and UVs are interleaved (one v-line per vt-line) so they share the same 1-based index — no post-pass reindexing.
- `--step` subsampling is applied per-tile (local stride from tile cell 0), so `step` values that don't divide 200 may cause minor alignment seams at tile boundaries. Stick to step ∈ {1, 2, 4, 5, 8, 10, 20, 25, 40, 50, ...} for clean results.

## Conventions when editing

- Add new OS-data helpers to `osdata/` and new Minecraft helpers to `minecraft/`. Never import upward: `osdata` must not depend on `minecraft` or `rendering`.
- When adding a new CLI that needs elevation data, import the streaming helpers from `osdata.tiles` rather than reimplementing tile discovery, header parsing, or halo stitching. See `max_vscale.py` for a minimal streaming consumer.
- Keep the "accepts single tile / region folder / data root" input pattern consistent across scripts — users chain these together (e.g. `locate.py` output fed into `generate.py`).
- When a new script overlays OS raster TIFFs over a heightmap, reuse `osdata.tiff.find_tiffs` + `QUADRANTS`: composite quadrants onto a `TILE_PX`-sized RGB canvas filled with `SEA_COLOUR`, then `Image.LANCZOS`-resize to the target. Per-script opacity is an intentional knob — `stitch.py` uses 0.33, `heightmap.py` uses 0.50.
- Output conventions: worlds go under `./worlds/<NAME>/` (use `common.paths.WORLDS_DIR`), meshes under `./meshes/<NAME>/` (use `MESHES_DIR`), PNGs alongside the input (`heightmap.py`) or in CWD (`stitch.py`).
