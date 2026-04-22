"""OS Open Rivers MBTiles rasterisation.

Reads the z14 Mapbox Vector Tiles layer, reprojects centrelines from
Web Mercator → WGS84 → BNG, and draws them one cell wide via Bresenham.
"""

import gzip
import math
import sqlite3

import numpy as np
from pyproj import Transformer

from .tiles import CELL_SIZE_M


def draw_line_cells(mask, c0, r0, c1, r1):
    """Rasterize a single-cell-wide line into a boolean mask (Bresenham)."""
    rows, cols = mask.shape
    dc = abs(c1 - c0)
    dr = -abs(r1 - r0)
    sc = 1 if c0 < c1 else -1
    sr = 1 if r0 < r1 else -1
    err = dc + dr
    while True:
        if 0 <= r0 < rows and 0 <= c0 < cols:
            mask[r0, c0] = True
        if c0 == c1 and r0 == r1:
            return
        e2 = 2 * err
        if e2 >= dr:
            err += dr
            c0 += sc
        if e2 <= dc:
            err += dc
            r0 += sr


def rasterize_rivers_for_tile(mask_shape, local_min_east, local_max_north, mbtiles_path,
                              forms=("canal", "inlandRiver", "tidalRiver"),
                              conn=None):
    """Rasterize OS Open Rivers centrelines into a boolean mask.

    The mask covers the BNG rectangle starting at (local_min_east,
    local_max_north) with mask_shape = (rows, cols). Typically called per-tile
    with the tile's halo-inclusive bbox so the mask aligns with tile_grid.

    Returns None if mapbox-vector-tile is missing, otherwise a boolean array of
    `mask_shape`. Pass `conn` to reuse an open sqlite connection across tiles
    (caller is responsible for opening/closing).
    """
    try:
        import mapbox_vector_tile
    except ImportError:
        return None

    mask_rows, mask_cols = mask_shape
    local_max_east  = local_min_east + mask_cols * CELL_SIZE_M
    local_min_north = local_max_north - mask_rows * CELL_SIZE_M

    to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    to_bng   = Transformer.from_crs("EPSG:4326",  "EPSG:27700", always_xy=True)

    zoom = 14
    n_tiles = 2 ** zoom

    def lonlat_to_tile(lon, lat):
        tx = int((lon + 180) / 360 * n_tiles)
        lat_rad = math.radians(lat)
        ty = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n_tiles)
        return tx, ty

    samples = []
    for f_e in np.linspace(local_min_east, local_max_east, 5):
        for f_n in np.linspace(local_min_north, local_max_north, 5):
            lon, lat = to_wgs84.transform(f_e, f_n)
            samples.append(lonlat_to_tile(lon, lat))
    txs, tys = zip(*samples)
    tx_min, tx_max = min(txs), max(txs)
    ty_min, ty_max = min(tys), max(tys)

    tms_y_min = n_tiles - 1 - ty_max
    tms_y_max = n_tiles - 1 - ty_min

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(mbtiles_path)
        close_conn = True
    cur = conn.cursor()
    cur.execute(
        "SELECT tile_column, tile_row, tile_data FROM tiles "
        "WHERE zoom_level=? AND tile_column BETWEEN ? AND ? "
        "AND tile_row BETWEEN ? AND ?",
        (zoom, tx_min, tx_max, tms_y_min, tms_y_max),
    )
    tile_rows = cur.fetchall()
    if close_conn:
        conn.close()

    mask = np.zeros(mask_shape, dtype=bool)

    for tile_col, tile_row, raw in tile_rows:
        tx = tile_col
        ty = n_tiles - 1 - tile_row
        try:
            pbf = gzip.decompress(raw)
        except OSError:
            pbf = raw
        try:
            decoded = mapbox_vector_tile.decode(pbf, default_options={"y_coord_down": True})
        except Exception:
            continue
        layer = decoded.get("watercourse_link")
        if not layer:
            continue
        extent = layer.get("extent", 4096)

        all_lines = []
        for feat in layer["features"]:
            form = feat.get("properties", {}).get("form")
            if form not in forms:
                continue
            geom = feat["geometry"]
            gtype = geom["type"]
            if gtype == "LineString":
                all_lines.append(geom["coordinates"])
            elif gtype == "MultiLineString":
                all_lines.extend(geom["coordinates"])
        if not all_lines:
            continue

        flat_lon, flat_lat, segments = [], [], []
        for line in all_lines:
            start = len(flat_lon)
            for px, py in line:
                lon = (tx + px / extent) / n_tiles * 360.0 - 180.0
                y_frac = (ty + py / extent) / n_tiles
                lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y_frac))))
                flat_lon.append(lon)
                flat_lat.append(lat)
            segments.append((start, len(flat_lon)))

        es, ns = to_bng.transform(flat_lon, flat_lat)
        cs = np.floor((np.asarray(es) - local_min_east)  / CELL_SIZE_M).astype(np.int32)
        rs = np.floor((local_max_north - np.asarray(ns)) / CELL_SIZE_M).astype(np.int32)

        for start, end in segments:
            if end - start < 2:
                continue
            for i in range(start, end - 1):
                draw_line_cells(mask, int(cs[i]), int(rs[i]),
                                int(cs[i + 1]), int(rs[i + 1]))

    return mask
