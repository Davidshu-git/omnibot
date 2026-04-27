# -*- coding: utf-8 -*-
"""
Runtime configuration for OmniMHXY.

Canonical env names use the MHXY_ prefix. The container entrypoint also exports
legacy names for compatibility, but application code should read from here.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = (PROJECT_ROOT / "data" / "mhxy").resolve()
CONFIG_DIR = (DATA_DIR / "config").resolve()

ADB_PATH = os.getenv("MHXY_ADB_PATH", r"C:\Program Files\Netease\MuMu\nx_main\adb.exe")
REMOTE_MODE = _env_bool("MHXY_REMOTE_MODE", _env_bool("REMOTE_MODE", False))
REMOTE_HOST = os.getenv("MHXY_REMOTE_HOST") or os.getenv("REMOTE_HOST", "")
REMOTE_USER = os.getenv("MHXY_REMOTE_USER") or os.getenv("REMOTE_USER", "")
PORT_RANGE = range(5555, 5581)

DEFAULT_RESOLUTION = (1600, 900)
QWEN_VL_PLUS_MODEL = os.getenv("MHXY_VL_MODEL", "qwen3-vl-plus")

INSTANCES_JSON = CONFIG_DIR / "instances.json"
ELEMENT_LIBRARY_JSON = CONFIG_DIR / "element_library.json"
