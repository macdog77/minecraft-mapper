# OS Data — Height & Map Tile Matching

## Folder Structure

```
OS Map Data/
  data/<region>/<tile>.zip          Height data (ESRI ASCII Grid inside zip)
  tiles/<REGION>/<quadrant>.tif     Raster map imagery (palette-indexed TIFF)
```

**Height data:** `data/hp/hp40_OST50GRID_20250529.zip`
**Map imagery:** `tiles/HP/HP40NE.tif`, `HP40NW.tif`, `HP40SE.tif`, `HP40SW.tif`

## Naming Convention

| Component | Height zip | Map TIFF |
|-----------|-----------|----------|
| Region code | lowercase (`hp`) | uppercase (`HP`) |
| Tile digits | `hp40` = easting 4, northing 0 | `HP40` + quadrant suffix |
| Quadrant | n/a (full 10 km tile) | `NE`, `NW`, `SE`, `SW` (5 km each) |
| Resolution | 50 m/cell, 200x200 cells | 1 m/px, 5000x5000 px per quadrant |

Each height zip covers a 10 km x 10 km area. Each TIFF covers a 5 km x 5 km quadrant — four TIFFs combine to form the full tile footprint.

## Coverage

- **55 regions** in both datasets — identical region coverage
- **2,858 tiles** have both height data and map imagery — every height tile has matching TIFFs
- **0 height tiles without TIFFs** — complete map coverage of all height data
- **5 TIFF-only tiles** with no corresponding height data: NL82, NM10, NO72, SD24, SD25 (each has only 1 quadrant — small coastal slivers not included in Terrain 50)
- **10,591 total TIFF files** across all regions

## TIFF Format

- Format: palette-indexed TIFF (mode `P`, 256-colour palette)
- Size: 5000 x 5000 pixels per quadrant
- Content: OS raster map imagery (topographic map colours — blue for sea, greens/whites for land)
