"""Canonical filesystem paths for the repository layout.

All paths are derived from the repo root (two levels above this file), so the
scripts work regardless of the current working directory.
"""

import os


_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(_PKG_DIR)

OS_DATA_DIR = os.path.join(REPO_ROOT, "OS Map Data")
DATA_ROOT   = os.path.join(OS_DATA_DIR, "data")
TILES_ROOT  = os.path.join(OS_DATA_DIR, "tiles")
RIVERS_PATH = os.path.join(OS_DATA_DIR, "rivers", "Data", "oprvrs_gb.mbtiles")

WORLDS_DIR  = os.path.join(REPO_ROOT, "worlds")
MESHES_DIR  = os.path.join(REPO_ROOT, "meshes")
