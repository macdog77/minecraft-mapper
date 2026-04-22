"""ESRI ASCII Grid parsing (OS Terrain 50 `.asc`)."""

import os
import zipfile


def parse_asc(fileobj):
    """Parse an ESRI ASCII Grid file. Returns (header dict, list-of-rows of floats)."""
    header = {}
    rows = []
    for raw in fileobj:
        line = raw.decode("ascii").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        parts = line.split()
        if parts[0].lower() in ("ncols", "nrows", "xllcorner", "yllcorner",
                                 "xllcenter", "yllcenter", "cellsize", "nodata_value"):
            key = parts[0].lower()
            val = float(parts[1]) if "." in parts[1] else int(parts[1])
            header[key] = val
        else:
            rows.append([float(v) for v in parts])
    return header, rows


def read_asc_from_zip(zip_path):
    """Return (header, rows) from the first .asc inside a zip."""
    with zipfile.ZipFile(zip_path) as zf:
        asc_names = [n for n in zf.namelist()
                     if n.lower().endswith(".asc") and not n.lower().endswith(".aux.xml")]
        if not asc_names:
            raise ValueError(f"No .asc found in {zip_path}")
        with zf.open(asc_names[0]) as f:
            return parse_asc(f)


def read_asc_from_file(asc_path):
    """Return (header, rows) from an .asc file on disk."""
    with open(asc_path, "r") as f:
        return parse_asc(f)


def load_tile(path):
    """Load an .asc from a path that is either a .zip or .asc file.

    Returns ((header, rows), stem) where stem is the filename without extension.
    Prints a one-line status message for CLI use.
    """
    path = os.path.abspath(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            asc_names = [n for n in zf.namelist() if n.lower().endswith(".asc")
                         and not n.lower().endswith(".asc.aux.xml")]
            if not asc_names:
                raise ValueError(f"No .asc file found inside {path}")
            asc_name = asc_names[0]
            print(f"Reading {asc_name} from {os.path.basename(path)}")
            with zf.open(asc_name) as f:
                return parse_asc(f), stem
    if path.lower().endswith(".asc"):
        print(f"Reading {os.path.basename(path)}")
        with open(path, "r") as f:
            return parse_asc(f), stem
    raise ValueError("Input must be a .zip or .asc file")
