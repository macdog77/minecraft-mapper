# Minecraft UK — World Generator

Converts Ordnance Survey Terrain 50 elevation data into playable Minecraft Java Edition 1.21 worlds.

---

## Installation

Requires Python 3.10+. Install all dependencies in one command:

```bash
pip install amulet-core tqdm numpy pillow pyproj mapbox-vector-tile
```

| Package | Version tested | Used by |
|---------|---------------|---------|
| `amulet-core` | 1.9.39 | `generate.py` — writes Minecraft world files |
| `numpy` | 1.26+ | `generate.py`, `mesh.py`, `stitch.py` — grid maths |
| `tqdm` | 4.67+ | `generate.py`, `mesh.py` — progress bars |
| `pillow` | 12+ | `generate.py`, `heightmap.py`, `mesh.py`, `stitch.py` — image I/O |
| `pyproj` | 3.6+ | `generate.py`, `locate.py` — WGS84 ↔ British National Grid conversion |
| `mapbox-vector-tile` | 2.1+ | `generate.py` — decodes OS Open Rivers MVT tiles. Optional; rivers are silently skipped if absent. |

---

## Scripts

### `generate.py` — Build a Minecraft world

Reads OS Terrain 50 tiles and writes a playable Minecraft Java Edition 1.21 world.

```bash
# Single 10 km tile (fast, good for testing)
python generate.py "OS Map Data/data/nt/nt27_OST50GRID_20250529.zip"

# Full 100 km region
python generate.py "OS Map Data/data/nn"

# Entire UK (all regions stitched into one world)
python generate.py "OS Map Data/data"
```

Output is saved to `./worlds/<name>/`. Copy that folder into your Minecraft saves directory and open it in-game.

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--scale N` | `1` | Horizontal blocks per OS cell. Each OS cell is 50 m, so `--scale 1` = 1 block per 50 m, `--scale 2` = 1 block per 25 m, `--scale 4` = 1 block per 12.5 m. |
| `--vscale F` | `0.10` | Vertical multiplier applied to elevation in metres. `Y = 64 + round(elevation × F)`. |
| `--biomes MODE` | `elevation` | `elevation` assigns biomes by height (ocean → plains → windswept hills → frozen peaks). `default` sets everything to plains. |
| `--spawn LAT,LON` | map centre | Spawn at a WGS84 coordinate, e.g. `"56.974586,-3.396210"`. If the point is outside the generated map, a warning is printed and the spawn falls back to the map centre. |
| `--no-water` | off | Skip inland water detection and the perimeter rim. |
| `--no-rivers` | off | Skip OS Open Rivers rasterization. Auto-disabled at `--scale < 4`. |
| `--buildings` | off | Detect the light-peach building-fill colour (#f8d8b8) on the OS raster tiles and place a 3-block stack of bricks on each flagged cell. Auto-disabled at `--scale < 8` (one OS cell is smaller than one block). |
| `--void` | off | Use inline flat-void generators for `WorldGenSettings` so the area outside the pre-filled OS map stays as void. Triggers Minecraft's "Worlds using Experimental Settings are not supported" warning on load. Default (off) writes vanilla preset-based generators, which means chunks beyond the OS area fill with vanilla terrain but no warning appears. |
| `--tiles-dir PATH` | `<input>/../../tiles` | Root of the OS raster TIFF folder. Only needed if the tile directory isn't a sibling of the data folder. |
| `--rivers-path PATH` | `<input>/../../rivers/Data/oprvrs_gb.mbtiles` | Path to the OS Open Rivers MBTiles file. Only needed if it isn't in the default location. |
| `--out PATH` | `./worlds/<name>` | Output world folder path. |

**Water and rim**

Inland water (lochs, reservoirs, rivers) is detected from two signals, OR-combined:

1. **Map TIFFs** — cells whose matching 50 m × 50 m block in the OS raster tile is mostly blue.
2. **Flat elevation** — cells whose local 5 × 5 window has an elevation range below 0.05 m (water surfaces report a single flat height).

For each flagged cell with elevation > 0 m, the top block is replaced with a water source block (`level=0`, so it stays put). Sea (elevation ≤ 0 m) is handled by the existing ocean fill up to Y = 63.

A **perimeter rim** of stone is added wherever water touches the world edge, capped at one block above the local water level. Dry edges are left as natural terrain.

**Rivers**

If `mapbox-vector-tile` is installed and the OS Open Rivers MBTiles file is present (default `OS Map Data/rivers/Data/oprvrs_gb.mbtiles`), `generate.py` rasterises river, canal, and tidal-river centrelines from the z14 vector tiles into the same water mask as the lochs and reservoirs. Coordinates are reprojected Web Mercator → WGS84 → BNG and lines are drawn one cell wide using Bresenham.

Rivers are only generated at `--scale 4` or higher — at smaller scales a single-cell line is ≥50 m wide, which makes every stream look like a small lake. After the water mask is blurred for rounded edges, river cells are pinned to full density so a 1-cell-wide line still survives the threshold while small lochs keep softened corners.

If `mapbox-vector-tile` is missing, or the MBTiles file isn't found, rivers are skipped silently and a note is printed; everything else still works.

**Buildings** (`--buildings`, `--scale 8`+)

OS Explorer raster tiles paint the built-up-area background with a pale-peach fill (#f8d8b8). `--buildings` walks the same quadrant TIFFs as the water detector and matches palette entries within ±8 per channel of that colour.

To keep buildings close to street level rather than whole 50 m OS cells, the detector aggregates pixels into a **5 m sub-cell mask** (10× denser than the elevation grid) and flags each sub-cell when ≥30% of its 5 × 5 pixels are peach. During chunk generation each block looks up its own sub-cell (nearest-neighbour, no blur), so isolated buildings stay crisp instead of merging into super-blocks. Flagged blocks get a 3-block-tall stack of `minecraft:bricks` on top of the surface. Tunables (`BUILDING_SUBCELL_M`, `TIFF_BUILDING_THRESHOLD`, `BUILDING_HEIGHT_BLOCKS`) live at the top of `generate.py`; use `building_mask_preview.py` (below) to preview what will be flagged without generating a world.

Buildings lose to water — blocks flagged as both (e.g. harbours, river banks) stay water. At `--scale < 8` a 50 m cell resolves to less than one block, so the feature auto-disables with a note.

**Spawn point** (`--spawn`)

By default the spawn is placed at the geographical centre of the generated map, one block above the local surface. Passing `--spawn "lat,lon"` converts the WGS84 coordinate to BNG (via `locate.wgs84_to_bng`), maps it to a grid cell, and spawns there instead. If the coordinate lies outside the generated tiles, a warning is printed and the spawn falls back to the map centre.

**Scale reference**

Minecraft's build limit is Y = 319 and sea level is pinned to Y = 64, leaving 255 blocks of vertical headroom. Anything above Y = 319 is clipped. Run `max_vscale.py` (below) against your input to get the exact largest safe `--vscale`.

| `--vscale` | Ben Nevis (1345 m) → Y | Best used for |
|------------|------------------------|---------------|
| `0.10` | 199 | Large regions, whole-UK maps |
| `0.15` | 266 | Single regions |
| `0.189` | 318 | Single tiles or small areas (near max for a Ben Nevis tile) |

| `--scale` | 1 tile (10 km) | Full region (100 km) |
|-----------|----------------|----------------------|
| `1` | 200 × 200 blocks | 2,000 × 2,000 blocks |
| `2` | 400 × 400 blocks | 4,000 × 4,000 blocks |
| `4` | 800 × 800 blocks | 8,000 × 8,000 blocks |

**World settings:** creative mode, spawn at the geographical centre of the map (or wherever `--spawn` points), one block above the surface. By default the world uses vanilla preset-based generators so chunks outside the OS area fill with normal Minecraft terrain; pass `--void` to keep those chunks as void at the cost of triggering the "Experimental Settings" warning on load.

---

### `mesh.py` — Export a textured 3D mesh

Builds a Wavefront OBJ mesh from the elevation grid and drapes the OS raster map tiles over it as a texture. Open in Blender, MeshLab, or any 3D viewer.

```bash
# Single tile
python mesh.py "OS Map Data/data/ng/ng42_OST50GRID_20250529.zip"

# Whole region, halved detail, true vertical proportions
python mesh.py "OS Map Data/data/nn" --step 2 --vscale 1.0

# Low-detail whole-UK preview
python mesh.py "OS Map Data/data" --step 4 --texture 16384
```

Output is saved to `./meshes/<name>/` as `<name>.obj`, `<name>.mtl`, and `<name>_texture.png`.

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--vscale F` | `1.0` | Vertical exaggeration applied to metres. `1.0` keeps true proportions; try `2.0`–`5.0` for a punchier landscape. |
| `--step N` | `1` | Use every Nth elevation cell. `--step 2` has 4× fewer triangles; useful for large regions. |
| `--texture N` | `8192` | Maximum texture dimension in pixels. Reduce for small viewers, increase for close-up detail. |
| `--out PATH` | `./meshes/<name>` | Output directory. |

Coordinate system: X = east, Y = elevation (metres × `--vscale`), Z = south.

---

### `locate.py` — Find which tile contains a location

Converts a WGS84 lat/lon coordinate to the British National Grid tile code and shows the data file path.

```bash
python locate.py <lat> <lon>
```

```bash
python locate.py 55.944546 -3.184685   # Edinburgh (Arthur's Seat)  → NT27
python locate.py 57.270356 -5.525787   # Skye Cuillin Ridge         → NG82
python locate.py 56.498406 -3.806473   # Loch Rannoch               → NN83
```

Output:
```
Coordinates:  55.944546, -3.184685
BNG:          326,106 E  673,023 N
100 km square:NT
10 km tile:   NT27
Data file:    OS Map Data\data\nt\nt27_OST50GRID_20250529.zip
```

You can chain it with `generate.py` to go straight from a coordinate to a world:

```bash
# Find the tile, then generate it
python locate.py 55.944546 -3.184685
python generate.py "OS Map Data/data/nt/nt27_OST50GRID_20250529.zip" --scale 4 --vscale 0.2
```

---

### `max_vscale.py` — Find the largest safe `--vscale` for your input

Scans the elevation grid of a tile, region, or whole-UK dataset and reports the highest point plus the largest `--vscale` that keeps every peak at or below Minecraft's Y = 319 build limit. The recommended value is truncated to 4 decimal places so the peak lands as close to Y = 319 as possible without clipping.

```bash
python max_vscale.py <tile.zip|region_dir|data_root>

python max_vscale.py "OS Map Data/data/nn/nn16_OST50GRID_20250529.zip"   # Ben Nevis tile
python max_vscale.py "OS Map Data/data/nn"                               # NN region
python max_vscale.py "OS Map Data/data"                                  # whole UK
```

Output:
```
Highest point: 1344.8 m  (tile NN16, BNG 216675E 771325N)
MC headroom:   255 blocks  (Y=64 sea level -> Y=319 build limit)

Max safe --vscale: 0.189618
Suggested (4 dp):  0.1896  -> peak lands at Y = 319
```

Feed the suggested value directly into `generate.py --vscale`.

---

### `heightmap.py` — Generate a single-tile heightmap image

Reads one tile (`.zip` or `.asc`) and saves a greyscale PNG. Black = sea level (0 m), white = Ben Nevis (1345 m) by default.

```bash
python heightmap.py <tile.zip>
python heightmap.py <tile.zip> [max_elev_m] [--tiff]

python heightmap.py "OS Map Data/data/ng/ng42_OST50GRID_20250529.zip"
python heightmap.py "OS Map Data/data/ng/ng42_OST50GRID_20250529.zip" 1000
python heightmap.py "OS Map Data/data/ng/ng42_OST50GRID_20250529.zip" --tiff
```

The `--tiff` flag composites the OS raster map quadrants for this tile over the heightmap at 50% opacity, so coastlines, roads, and labels show through the relief shading.

Output is saved alongside the input file as `<tilename>_heightmap.png` (or `<tilename>_heightmap_tiff.png` with `--tiff`).

---

### `stitch.py` — Stitch a full region into one heightmap image

Reads all tiles in a region folder and combines them into a single map image. North is up.

```bash
python stitch.py <region_dir>
python stitch.py <region_dir> [max_elev_m] [--tiff] [--grid]

python stitch.py "OS Map Data/data/nn"
python stitch.py "OS Map Data/data/nn" 1345 --tiff
python stitch.py "OS Map Data/data/nn" 1345 --tiff --grid
```

The `--tiff` flag composites the OS raster map tiles over the heightmap at 33% opacity, so towns, roads, and water labels show through the relief shading. The `--grid` flag overlays a red grid with the tile code (e.g. NN30) labelled in each cell — useful for identifying which tile to pass to `generate.py` or `locate.py`. Layers are applied in order: heightmap → TIFF → grid.

Output is saved in the current directory as `<REGION>_heightmap.png`, with `_tiff`, `_grid`, or `_tiff_grid` appended when overlays are enabled.

---

### `uk_map.py` — Composite the full UK raster map

Stitches every OS raster TIFF under `OS Map Data/tiles/` into a single UK-wide PNG, scaled to a target height, with the 100 km BNG grid and region letters overlaid in red. Useful as an overview image to plan which regions to generate.

```bash
python uk_map.py
python uk_map.py --height 4000 --out UK_map_highres.png
python uk_map.py --no-grid               # plain map without overlay
python uk_map.py --label-scale 0.3       # smaller region letters
```

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--height N` | `2000` | Output height in pixels. |
| `--out PATH` | `./UK_map.png` | Output PNG path. |
| `--no-grid` | off | Skip the red grid and region labels. |
| `--label-scale F` | `0.5` | Region-letter size as fraction of a 100 km square. |

---

## Utilities

Diagnostic and tuning helpers used when extending the feature-detection logic in `generate.py`.

### `sample_palette.py` — Inspect an OS raster palette

Dumps the palette of a single TIFF quadrant with an ANSI colour swatch per entry and the pixel count / percentage for each used index. Handy for identifying the palette index of a feature before writing a colour filter.

```bash
python sample_palette.py "OS Map Data/tiles/NT/NT27NW.tif" --only-used
python sample_palette.py "OS Map Data/tiles/NT/NT27NW.tif" --filter "r>240,g>200,b>170"
```

### `building_mask_preview.py` — Preview the building mask for one TIFF

Runs `generate._is_building_color` (or a manual `--r/--g/--b` override) against a single TIFF and writes a PNG where building pixels are black on white. Lets you tune the building filter without running the full world generator.

```bash
python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif"
python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif" --cells
python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif" --overlay
python building_mask_preview.py "OS Map Data/tiles/NT/NT27SW.tif" --r 248,15 --g 216,15 --b 184,20
```

- `--cells` aggregates to 50 m OS cells and thresholds at `TIFF_BUILDING_THRESHOLD`, mirroring what `generate.py --buildings` actually places.
- `--overlay` additionally writes `<stem>_overlay.png` with matched pixels tinted red on the source image.

### `TestWorld.py` — Build-height diagnostic world

Writes a minimal world containing a single stone tower that runs from bedrock (Y = −64) to the build limit (Y = 319), with `gold_block` markers at every 32-block interval (plus Y = 63, 64, 255, 256, 319). Loading it in Minecraft and flying up the tower confirms the save pipeline is producing every vertical section correctly — if the tower ends early, the topmost visible gold marker pinpoints where sections are being dropped.

```bash
python TestWorld.py
python TestWorld.py --out worlds/TowerCheck
```

Output is saved to `./worlds/TestWorld_Tower/` by default. Used when investigating amulet section-write regressions or Minecraft dimension-bounds issues.

### `inspect_leveldat.py` — Dump experimental-flag fields from `level.dat`

Prints the top-level `Data` keys of a generated world's `level.dat` and the full contents of the fields Minecraft checks when deciding whether a world uses experimental settings (`DataPacks`, `enabled_features`, `experiments`, `WorldGenSettings`, `Version`). Used when chasing "Worlds using Experimental Settings are not supported" warnings.

```bash
python inspect_leveldat.py worlds/<name>
python inspect_leveldat.py worlds/<name>/level.dat
```

---

## Appendix — Data Sources

All three datasets are free Ordnance Survey OpenData products. Download from the OS Data Hub and unpack into `OS Map Data/` as described in `OSData.md`.

| Dataset | Used for | Format | Download |
|---------|----------|--------|----------|
| OS Terrain 50 | Elevation grid (50 m / cell) for the whole of Great Britain | Zipped ESRI ASCII Grid (`.asc`) | [osdatahub.os.uk/data/downloads/open/Terrain50](https://osdatahub.os.uk/data/downloads/open/Terrain50) |
| OS OpenMap Local | Water-surface detection via blue-pixel ratio on the raster map | GeoTIFF, full colour | [osdatahub.os.uk/data/downloads/open/OpenMapLocal](https://osdatahub.os.uk/data/downloads/open/OpenMapLocal) |
| OS Open Rivers | River, canal, and tidal-river centrelines (work in progress) | Vector Tiles (`.mbtiles`) | [osdatahub.os.uk/data/downloads/open/OpenRivers](https://osdatahub.os.uk/data/downloads/open/OpenRivers) |

---

## Appendix — OS Region Codes

Each region is a 100 km × 100 km British National Grid square. Tile count indicates how many 10 km tiles are present (max 100; lower counts mean significant sea coverage).

### H Squares — Shetland & Orkney

| Code | Tiles | Description |
|------|-------|-------------|
| HP | 6 | Unst & Fetlar — northernmost Shetland, Britain's most northerly land |
| HT | 2 | Northwest Shetland — mostly open Atlantic, small coastal slivers |
| HU | 41 | Central & South Shetland — Lerwick, Mainland, Scalloway |
| HW | 4 | St Kilda — remote Atlantic archipelago, dramatic sea stacks |
| HX | 2 | North Rona / Sula Sgeir — tiny rocky outposts, mainly open sea |
| HY | 31 | Orkney Islands — Kirkwall, Stromness, Ring of Brodgar, Scapa Flow |
| HZ | 4 | Fair Isle & South Shetland fringe — Fair Isle midway between Orkney and Shetland |

### N Squares — Scotland

| Code | Tiles | Description |
|------|-------|-------------|
| NA | 9 | Northwest Lewis coast — Butt of Lewis, open Atlantic coast, largely sea |
| NB | 35 | Lewis & Harris (Outer Hebrides) — Stornoway, Harris hills, sea lochs |
| NC | 70 | Sutherland — Cape Wrath, Tongue, Durness, Ben Hope, Kyle of Tongue |
| ND | 32 | Caithness — Wick, John o'Groats, Duncansby Head, flat flow country |
| NF | 32 | South Uist & Benbecula (Outer Hebrides) — machair, lochs, shallow Atlantic coast |
| NG | 78 | Skye & Wester Ross — Cuillin Ridge, Torridon, Applecross, Portree |
| NH | 99 | Inverness & the Great Glen — Loch Ness, Cairngorms north, Black Isle |
| NJ | 72 | Moray & Speyside — Elgin, Aviemore, Cairngorm plateau, Speyside distilleries |
| NK | 8 | Northeast Aberdeenshire coast — Fraserburgh, Peterhead, Buchan Ness |
| NL | 8 | Tiree, Colonsay & southern Outer Hebrides — low Atlantic islands, white sand beaches |
| NM | 79 | Mull, Ardnamurchan & Loch Linnhe — Ben More (Mull), Fort William coast, Tobermory |
| NN | 100 | Central Highlands — Ben Nevis, Rannoch Moor, Ben Lomond, Loch Tay, Loch Lomond north |
| NO | 80 | Tayside & Angus — Perth, Dundee, Cairngorms south, Grampians, Glen Shee |
| NR | 61 | Islay, Jura & Kintyre — southern Hebrides, Mull of Kintyre, Islay distilleries |
| NS | 97 | Glasgow & the Clyde — Glasgow, Loch Lomond south, Ayrshire, Arran |
| NT | 93 | Edinburgh & the Borders — Edinburgh, Pentland Hills, Cheviot Hills, Tweed valley |
| NU | 15 | Northumberland coast — Berwick-upon-Tweed, Holy Island, Bamburgh, Farne Islands |
| NW | 3 | Rhins of Galloway — Mull of Galloway, Britain's most southwesterly Scottish coast |
| NX | 69 | Dumfries & Galloway — Dumfries, Solway Firth, Galloway Forest Park, Cairnsmore |
| NY | 100 | Northern Lake District & Carlisle — Scafell Pike, Helvellyn, Hadrian's Wall, Eden valley |
| NZ | 61 | County Durham & Tyneside — Newcastle, Durham Cathedral, Teesside, North Pennines |

### O Squares — North Sea

| Code | Tiles | Description |
|------|-------|-------------|
| OV | 1 | North Sea offshore — small coastal sliver east of Tyne/Tees, mostly open sea |

### S Squares — England & Wales

| Code | Tiles | Description |
|------|-------|-------------|
| SD | 84 | Lancaster & Lake District south — Morecambe Bay, Bowland Fells, Yorkshire Dales west |
| SE | 100 | Yorkshire — York, Leeds, Bradford, Yorkshire Dales east, Humber estuary |
| SH | 68 | North Wales & Snowdonia — Snowdon, Anglesey, Llŷn Peninsula, Caernarfon |
| SJ | 99 | Cheshire & North Midlands — Chester, Stoke-on-Trent, Wrexham, Shropshire north |
| SK | 100 | Peak District & East Midlands — Kinder Scout, Nottingham, Derby, Leicester |
| SM | 18 | Pembrokeshire coast — St Davids, Pembroke, Milford Haven, mostly coastal |
| SN | 81 | Mid Wales — Aberystwyth, Brecon Beacons north, Cambrian Mountains, Ceredigion |
| SO | 100 | Welsh Marches — Hereford, Worcester, Malvern Hills, Shropshire, Offa's Dyke |
| SP | 100 | Midlands & Cotswolds — Oxford, Stratford-upon-Avon, Northampton, Cotswold plateau |
| SR | 2 | South Pembrokeshire offshore — largely sea south of Pembrokeshire |
| SS | 60 | North Devon & Gower — Exmoor, Barnstaple, Swansea, Gower Peninsula |
| ST | 98 | Bristol & Somerset — Bath, Mendip Hills, Glastonbury, Quantocks, Cardiff fringe |
| SU | 100 | Hampshire & Berkshire — Salisbury Plain, New Forest, Winchester, Reading |
| SV | 4 | Isles of Scilly — St Mary's and outer islands, 45 km southwest of Land's End |
| SW | 33 | West Cornwall — Land's End, Penzance, St Ives, Lizard Peninsula |
| SX | 63 | South Devon — Dartmoor, Plymouth, Torquay, Kingsbridge estuary |
| SY | 26 | Dorset coast — Weymouth, Chesil Beach, Lyme Bay, Jurassic Coast west |
| SZ | 19 | Isle of Wight & Bournemouth — Needles, Ventnor, Christchurch harbour |

### T Squares — East & Southeast England

| Code | Tiles | Description |
|------|-------|-------------|
| TA | 34 | East Yorkshire coast — Bridlington, Flamborough Head, Spurn Point, Humber north |
| TF | 79 | Lincolnshire & The Wash — Boston, Skegness, Spalding fens, Norfolk northwest |
| TG | 24 | Norfolk — Norwich, Norfolk Broads, Cromer, North Norfolk coast |
| TL | 100 | East of England — Cambridge, Hertfordshire, Essex north, Ely, Bedford |
| TM | 45 | Suffolk & Essex coast — Ipswich, Felixstowe, Dedham Vale, Southwold |
| TQ | 99 | Greater London & Home Counties — Central London, Surrey Hills, North Downs, Kent north |
| TR | 27 | Kent — Canterbury, Dover, Folkestone, White Cliffs, Channel coast |
| TV | 3 | East Sussex coast — Eastbourne, Beachy Head, Seven Sisters cliffs |
