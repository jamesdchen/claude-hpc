"""Load, validate, and build environment from hpc.yaml experiment manifests."""

from __future__ import annotations

__all__ = [
    "load_manifest",
    "manifest_exists",
    "validate_manifest",
    "build_manifest_env",
    "resolve_template",
]

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from hpc._config import load_clusters_config
from hpc.grid import total_tasks


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """Load ``hpc.yaml`` from *path* (default: cwd)."""
    if path is None:
        path = Path.cwd() / "hpc.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    with open(path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def manifest_exists(path: Path | None = None) -> bool:
    """Return True if ``hpc.yaml`` exists at *path* (default: cwd)."""
    if path is None:
        path = Path.cwd() / "hpc.yaml"
    return path.exists()


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Validate a parsed manifest dict. Return list of error strings (empty = valid)."""
    errors: list[str] = []

    # Required top-level keys
    required = {"project", "cluster", "remote_path", "run", "grid", "resources"}
    missing = required - manifest.keys()
    if missing:
        errors.append(f"Missing required top-level keys: {sorted(missing)}")

    # grid
    grid = manifest.get("grid")
    if grid is not None:
        if not isinstance(grid, dict) or not grid:
            errors.append("'grid' must be a non-empty dict")
        elif not all(isinstance(v, list) for v in grid.values()):
            errors.append("Each value in 'grid' must be a list")

    # resources
    resources = manifest.get("resources")
    if resources is not None:
        for key in ("mem", "walltime"):
            if key not in resources:
                errors.append(f"'resources' missing required key '{key}'")

    # env (optional)
    env = manifest.get("env")
    if env is not None and not isinstance(env, dict):
        errors.append("'env' must be a dict")

    # chunking (optional)
    chunking = manifest.get("chunking")
    if chunking is not None:
        total = chunking.get("total")
        if not isinstance(total, int) or total < 1:
            errors.append("'chunking.total' must be a positive int")

    # results (optional)
    results = manifest.get("results")
    if results is not None:
        for key in ("dir", "pattern"):
            if key not in results:
                errors.append(f"'results' missing required key '{key}'")

    return errors


def build_manifest_env(manifest: dict[str, Any]) -> dict[str, str]:
    """Build template env vars from a manifest for job submission."""
    clusters = load_clusters_config()
    cluster = clusters[manifest["cluster"]]
    env_cfg = manifest.get("env", {})

    chunks = 1
    chunking = manifest.get("chunking")
    if chunking:
        chunks = chunking.get("total", 1)

    result: dict[str, str] = {
        "EXECUTOR": "python3 _hpc_dispatch.py",
        "HPC_MANIFEST": "_hpc_dispatch.json",
        "REPO_DIR": manifest["remote_path"],
        "MODULES": env_cfg.get("modules", ""),
        "TOTAL_CHUNKS": str(total_tasks(manifest["grid"], chunks)),
    }

    conda_env = env_cfg.get("conda_env")
    if conda_env:
        result["CONDA_SOURCE"] = cluster["conda_source"]
        result["CONDA_ENV"] = conda_env

    return result


def resolve_template(manifest: dict[str, Any]) -> str:
    """Determine job template name from manifest resources."""
    if "gpus" in manifest.get("resources", {}):
        return "gpu_array"
    return "cpu_array"
