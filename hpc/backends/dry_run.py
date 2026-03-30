"""Dry-run backend — prints what would be submitted without running anything."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc.backends import HPCBackend, register

if TYPE_CHECKING:
    from pathlib import Path


@register("dry-run")
class DryRunBackend(HPCBackend):
    """Print what would be submitted without actually running anything."""

    def __init__(self, **kwargs: object) -> None:
        self.log_dir = ""  # unused, satisfies base class attribute
        pass  # Accept and ignore backend-specific kwargs (e.g. script)

    def _build_command(self, task_range: str, job_name: str, job_env: dict[str, str]) -> list[str]:
        return []  # Not used — submit_array is overridden entirely

    def submit_array(
        self,
        job_name: str,
        total_chunks: int,
        tasks_per_array: int,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> None:
        result_dir = job_env.get("RESULT_DIR", "?")
        print(f"  [DRY RUN] Job: {job_name}")
        print(f"            Chunks: 1-{total_chunks} (batches of {tasks_per_array})")
        print(f"            Output: {result_dir}")
        for k in ("MODEL_TYPE", "EXPERIMENT"):
            if k in job_env and job_env[k]:
                print(f"            {k}: {job_env[k]}")
        extra_args = job_env.get("EXTRA_ARGS", "")
        if extra_args:
            print(f"            Extra args: {extra_args}")
        print()
