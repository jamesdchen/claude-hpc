"""claude-hpc: Personal HPC orchestrator for Claude Code.

Provides pluggable HPC backends (SGE, SLURM), remote execution utilities,
job lifecycle tracking, and GPU selection — all configurable via
clusters.yaml and per-project project.yaml files.
"""

__all__ = [
    "load_clusters_config",
    "load_project_config",
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "log_event",
    "read_events",
    "check_results",
    "report_status",
    "detect_scheduler",
    "pick_gpu",
]

from hpc._config import load_clusters_config, load_project_config
from hpc.gpu import pick_gpu
from hpc.lifecycle import check_results, detect_scheduler, log_event, read_events, report_status
from hpc.remote import rsync_pull, rsync_push, ssh_run
