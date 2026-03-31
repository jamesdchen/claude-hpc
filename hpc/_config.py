"""Configuration loaders for clusters.yaml."""

from __future__ import annotations

__all__ = ["load_clusters_config", "detect_project_type"]

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def load_clusters_config(path: Path | None = None) -> dict[str, Any]:
    """Load cluster definitions from clusters.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``config/clusters.yaml`` relative to the package root
    """
    if path is None:
        path = _PACKAGE_ROOT / "config" / "clusters.yaml"
    with open(path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def detect_project_type(path: Path | None = None) -> str:
    """Return ``"manifest"`` if ``hpc.yaml`` exists, else ``"none"``."""
    base = path or Path.cwd()
    if (base / "hpc.yaml").exists():
        return "manifest"
    return "none"
