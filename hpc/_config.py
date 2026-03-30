"""Configuration loaders for clusters.yaml and project.yaml."""

from __future__ import annotations

__all__ = ["load_clusters_config", "load_project_config", "build_stage_env"]

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


def build_stage_env(cluster_name: str, stage_name: str) -> dict[str, str]:
    """Build template env vars for a stage from project.yaml + clusters.yaml.

    Reads ``project.stages[stage_name]`` to find the executor and env_group,
    then looks up ``project.cluster_envs[cluster_name][env_group]`` for
    modules/conda settings, and merges with cluster-level conda_source.

    Returns a dict suitable for passing to a job template::

        {"MODULES": ..., "REPO_DIR": ..., "EXECUTOR": ...,
         "CONDA_SOURCE": ..., "CONDA_ENV": ...}
    """
    clusters = load_clusters_config()
    project = load_project_config()

    cluster = clusters[cluster_name]
    stage = project["stages"][stage_name]
    env_group = stage["env_group"]
    env_cfg = project["cluster_envs"][cluster_name][env_group]

    result: dict[str, str] = {
        "MODULES": env_cfg.get("modules", ""),
        "REPO_DIR": project["remote_path"],
        "EXECUTOR": stage["executor"],
    }

    conda_env = env_cfg.get("conda_env")
    if conda_env is not None:
        result["CONDA_SOURCE"] = cluster["conda_source"]
        result["CONDA_ENV"] = conda_env

    return result
