# -*- coding: utf-8 -*-
"""
Runtime configuration for OmniMHXY.

Canonical env names use the MHXY_ prefix. The container entrypoint also exports
legacy names for compatibility, but application code should read from here.
"""
from __future__ import annotations

import os
from pathlib import Path



PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = (PROJECT_ROOT / "data" / "mhxy").resolve()
CONFIG_DIR = (DATA_DIR / "config").resolve()

PORT_RANGE = range(5555, 5581)

DEFAULT_RESOLUTION = (1600, 900)
QWEN_VL_PLUS_MODEL = os.getenv("MHXY_VL_MODEL", "qwen3-vl-plus")

INSTANCES_JSON = CONFIG_DIR / "instances.json"
ELEMENT_LIBRARY_JSON = CONFIG_DIR / "element_library.json"
