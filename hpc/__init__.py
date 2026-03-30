"""claude-hpc: Personal HPC orchestrator for Claude Code.

Provides pluggable HPC backends (SGE, SLURM), remote execution utilities,
job lifecycle tracking, and GPU selection — all configurable via
clusters.yaml and per-project project.yaml files.
"""

__all__ = [
    "load_clusters_config",
    "load_project_config",
    "build_stage_env",
    "get_template_path",
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "log_event",
    "read_events",
    "check_results",
    "report_status",
    "detect_scheduler",
    "pick_gpu",
    "collect",
]

from pathlib import Path

from hpc._config import _PACKAGE_ROOT, build_stage_env, load_clusters_config, load_project_config
from hpc.collect import collect
from hpc.gpu import pick_gpu
from hpc.lifecycle import check_results, detect_scheduler, log_event, read_events, report_status
from hpc.remote import rsync_pull, rsync_push, ssh_run


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
