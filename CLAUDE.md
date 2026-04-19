# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Python scripts that convert Ordnance Survey Terrain 50 elevation data (and OS raster map tiles) into Minecraft Java Edition 1.21 worlds, 3D OBJ meshes, or greyscale heightmap PNGs.

## Environment

- Python 3.10+
- Install: `pip install amulet-core tqdm numpy pillow pyproj mapbox-vector-tile`
- `mapbox-vector-tile` is only required when `generate.py` reads rivers (`OS Map Data/rivers/Data/oprvrs_gb.mbtiles`); without it, rivers are silently skipped.
- `amulet-core` 1.9.x is the Minecraft world writer; it is sensitive to Minecraft version and writes an incomplete `level.dat` that `generate.py` patches afterwards (see below).
- No build step, no lint config, no test suite. Changes are validated by running the scripts end-to-end — typically starting with a single 10 km tile (`generate.py "OS Map Data/data/nt/nt27_*.zip"`) because full-region/full-UK runs are slow and memory-heavy.

## Common commands

```bash
python generate.py "<tile.zip | region_dir | data_root>" [--scale N] [--vscale F] [--biomes elevation|default] [--no-water] [--out PATH]
python mesh.py     "<tile.zip | region_dir | data_root>" [--vscale F] [--step N] [--texture N] [--out PATH]
python locate.py   <lat> <lon>                             # WGS84 → BNG tile code + path
python heightmap.py <tile.zip|asc> [max_elev_m] [--tiff]   # single-tile PNG; --tiff overlays the OS raster at 50%
python stitch.py   <region_dir> [max_elev_m] [--tiff] [--grid]   # whole-region PNG; layered heightmap → tiff @ 33% → grid
```

`README.md` is the user-facing documentation; keep its option tables and examples in sync with script behaviour.

## Pipeline and shared modules

The three "heavy" scripts (`generate.py`, `mesh.py`, `stitch.py`) all share parsing code via `sys.path` hacks that insert the repo root so sibling imports resolve:

- `heightmap.parse_asc(fileobj)` — canonical ESRI ASCII Grid parser, used by `generate.py` (via `_load_zip`), `stitch.py`, and `heightmap.py` itself. Returns `(header_dict, rows_of_floats)`.
- `generate.discover_zips(input_path)` and `generate.load_elevation_grid(zip_entries)` — reused by `mesh.py`. These accept a single `.zip`, a region folder, or the parent `data/` folder and return a stitched float32 numpy grid plus the BNG origin.
- `mesh._find_tiffs`, `QUADRANTS`, `TILE_PX`, and `SEA_COLOUR` — also imported by `stitch.py` (top of file) and `heightmap.py` (lazily inside `overlay_tiff`) for the `--tiff` overlay feature. The lazy import in `heightmap.py` is deliberate: `mesh` → `generate` → `amulet` is a heavy import chain (~1 s plus PyMCTranslate noise on stdout) and the non-tiff path of `heightmap.py` shouldn't pay that cost. Preserve the lazy import if you refactor.

If you change either of those, audit all callers — there is no package structure.

## Grid and coordinate conventions

These conventions are consistent across all scripts; breaking them produces silently flipped or offset output:

- Elevation grid: `grid[row, col]`, row 0 = northernmost, col 0 = westernmost. Matches ASC file ordering, so no flip is needed when loading.
- OS cell size: `CELL_SIZE_M = 50` (50 m × 50 m per cell, 200 × 200 cells per 10 km tile).
- BNG: EPSG:27700 easting/northing in metres; `load_elevation_grid` returns `(origin_easting, origin_northing_top)` — note the **top** (north) edge, not the SW corner used inside individual ASC headers.
- Minecraft mapping (in `generate.py`): `Y = MAP_ZERO_Y + round(elev_m × vscale)` where `MAP_ZERO_Y = 64`, `SEA_SURFACE_Y = 63`. Sea floors are forced at least one block below `SEA_SURFACE_Y` so water has somewhere to sit. The Y range used is `[-64, 319]` with `ARRAY_OFFSET = 64`.
- Mesh axes (in `mesh.py`): X = east, Y = elevation (× vscale), Z = south.

## OS dataset layout

Paths follow a fixed convention (see `OSData.md`):

```
OS Map Data/data/<region>/<tile>.zip          # lowercase region; .asc elevation inside zip
OS Map Data/tiles/<REGION>/<tile><QUAD>.tif   # UPPERCASE region; 5000×5000 palette-indexed TIFF
```

Each elevation `.zip` covers 10 km × 10 km (50 m/cell). Each TIFF covers one 5 km quadrant (NW/NE/SW/SE) at 1 m/px. Five TIFFs have no matching elevation tile (listed in `OSData.md`); never assume 1:1 coverage.

`locate.py` has the forward BNG letter-code tables (`_MAJOR`, `_MINOR_LETTERS`); `mesh.py` has the reverse lookup (`_MAJOR_REVERSE`, same `_MINOR_LETTERS`). Keep these in sync if you touch either.

## generate.py — non-obvious Minecraft specifics

Amulet writes a minimal world that Minecraft 1.21 will reject or corrupt. `generate.py` works around this in three places — do not remove these without understanding why:

1. **`patch_level_dat`** rewrites `level.dat` after `level.save()` to add a valid `WorldGenSettings` (void-flat generators for all three dimensions so Minecraft does not terrain-gen over our chunks), a `Version` compound with `DataVersion 3953` (Java 1.21.0), spawn coords, and creative/allowCommands flags.
2. **`write_entity_files`** manually writes `entities/r.X.Z.mca` region files containing a minimal NBT per chunk (DataVersion, Position, empty Entities list). Without these, Minecraft 1.17+ throws `NoSuchElementException` in `EntityStorage` and refuses to load any chunk. This code writes the Anvil region format directly (4096-byte sectors, location+timestamp tables, zlib-compressed payloads) because Amulet does not produce entity files.
3. **Water handling** is a two-signal OR: blue-pixel fraction per cell from OS raster TIFFs (`_tile_water_fraction`) plus a flat-elevation detector (`_flat_area_mask` — lochs/reservoirs report a single elevation over a 5×5 window). A perimeter stone rim is added wherever water touches the world edge to stop it leaking into the void beyond the OS coverage area.

The block/biome palettes live at the top of `generate.py` as `THRESHOLDS` and `BIOME_THRESHOLDS` lists of `(min_elev_m, ...)` tuples with a sentinel `None` threshold for the catch-all. Edit these to change the appearance.

## mesh.py specifics

- Uses `generate.discover_zips` + `generate.load_elevation_grid` for parity with the Minecraft output.
- Texture is stitched from the TIFF quadrants onto a single PIL canvas, then downsampled to `--texture` max-dim; sea gaps are filled with `SEA_COLOUR`.
- OBJ writer batches vertices/UVs/faces in 50k-line chunks via `f.writelines` generator expressions to keep memory bounded on whole-UK meshes.

## Conventions when editing

- When adding new scripts that need elevation data, import `parse_asc` from `heightmap` and `discover_zips` + `load_elevation_grid` from `generate` rather than reimplementing tile discovery or grid stitching.
- Keep the "accepts single tile / region folder / data root" input pattern consistent across scripts — users chain these together (e.g. `locate.py` output fed into `generate.py`).
- When a new script overlays OS raster TIFFs over a heightmap, reuse `mesh._find_tiffs` + `QUADRANTS`: composite quadrants onto a `TILE_PX`-sized RGB canvas filled with `SEA_COLOUR`, then `Image.LANCZOS`-resize to the target. Per-script opacity is an intentional knob — `stitch.py` uses 0.33, `heightmap.py` uses 0.50.
- Output conventions: worlds go under `./worlds/<NAME>/`, meshes under `./meshes/<NAME>/`, PNGs alongside the input (`heightmap.py`) or in CWD (`stitch.py`).
