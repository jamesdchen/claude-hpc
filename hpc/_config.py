"""Configuration loaders for clusters.yaml and project.yaml."""

from __future__ import annotations

__all__ = ["load_clusters_config", "load_project_config"]

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


def load_project_config(path: Path | None = None) -> dict[str, Any]:
    """Load a project config from project.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``project.yaml`` in the current working directory
    """
    if path is None:
        path = Path.cwd() / "project.yaml"
    with open(path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result
