"""Keel core library — zero external dependencies (Python 3 stdlib only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PLUGIN_ROOT / "config"
PRESETS_DIR = CONFIG_DIR / "presets"
SCHEMAS_DIR = CONFIG_DIR / "schemas"


class KeelError(RuntimeError):
    """Actionable operational or validation error surfaced cleanly to CLI callers."""


def load_defaults() -> dict[str, Any]:
    path = CONFIG_DIR / "defaults.json"
    return json.loads(path.read_text(encoding="utf-8"))
