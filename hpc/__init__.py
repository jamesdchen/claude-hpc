"""claude-hpc: Personal HPC orchestrator for Claude Code.

Provides pluggable HPC backends (SGE, SLURM), remote execution utilities,
job lifecycle tracking, GPU selection, and experiment-agnostic grid dispatch —
all configurable via clusters.yaml and per-project project.yaml or hpc.yaml files.
"""

__all__ = [
    "load_clusters_config",
    "load_project_config",
    "build_stage_env",
    "detect_project_type",
    "get_template_path",
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "deploy_runtime",
    "log_event",
    "read_events",
    "check_results",
    "report_status",
    "detect_scheduler",
    "pick_gpu",
    "collect",
    # Chunking protocol
    "ChunkContext",
    "chunk_context",
    "collect_chunks",
    # Manifest / grid API
    "load_manifest",
    "manifest_exists",
    "validate_manifest",
    "build_manifest_env",
    "resolve_template",
    "expand_grid",
    "build_task_manifest",
    "total_tasks",
]

from pathlib import Path
from typing import Any

from hpc._config import (
    _PACKAGE_ROOT,
    build_stage_env,
    detect_project_type,
    load_clusters_config,
    load_project_config,
)
from hpc.chunking import ChunkContext, chunk_context, collect_chunks
from hpc.gpu import pick_gpu
from hpc.grid import build_task_manifest, expand_grid, total_tasks
from hpc.lifecycle import check_results, detect_scheduler, log_event, read_events, report_status
from hpc.manifest import (
    build_manifest_env,
    load_manifest,
    manifest_exists,
    resolve_template,
    validate_manifest,
)
from hpc.remote import deploy_runtime, rsync_pull, rsync_push, ssh_run


def __getattr__(name: str) -> Any:
    if name == "collect":
        from hpc.collect import collect

        return collect
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_template_path(scheduler: str, template: str) -> Path:
    """Return the absolute path to a job template shipped with claude-hpc.

    Parameters
    ----------
    scheduler : ``"sge"`` or ``"slurm"``
    template : template name without extension (e.g. ``"cpu_array"``, ``"gpu_array"``)

    Returns
    -------
    Path to the template file.

    Raises
    ------
    FileNotFoundError
        If the resolved template does not exist on disk.
    """
    ext = ".sh" if scheduler == "sge" else ".slurm"
    path = _PACKAGE_ROOT / "templates" / scheduler / f"{template}{ext}"
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path
