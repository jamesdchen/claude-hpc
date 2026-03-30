"""Remote SGE backend — submits array jobs via qsub over SSH.

This backend requires a ``remote_repo`` path and an ``ssh_run`` callable
to be provided at construction time (no project-specific imports).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hpc.backends import HPCBackend, register

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from subprocess import CompletedProcess


@register("sge-remote")
class RemoteSGEBackend(HPCBackend):
    """SGE backend that runs qsub on the cluster via SSH.

    Unlike the local ``SGEBackend``, this backend does not call ``qsub``
    directly — it wraps each command in ``ssh_run()`` so submissions
    happen on a remote login node.

    Parameters
    ----------
    script : str
        Path to the job script *on the remote host*.
    ssh_run : callable
        A function ``(cmd: str) -> CompletedProcess`` that executes a
        shell command on the remote host via SSH.
    remote_repo : str
        Absolute path to the project directory on the remote host (used
        as ``cd`` target before ``qsub``).
    log_dir : str | None
        Remote log directory.  Defaults to ``<remote_repo>/logs``.
    pass_env_keys : tuple[str, ...]
        Environment variable names to forward via ``qsub -v``.
    """

    def __init__(
        self,
        script: str | None = None,
        ssh_run: Callable[[str], CompletedProcess[str]] | None = None,
        remote_repo: str | None = None,
        log_dir: str | None = None,
        pass_env_keys: tuple[str, ...] = (),
    ):
        if script is None:
            raise ValueError("RemoteSGEBackend requires a 'script' path")
        if ssh_run is None:
            raise ValueError("RemoteSGEBackend requires an 'ssh_run' callable")
        if remote_repo is None:
            raise ValueError("RemoteSGEBackend requires a 'remote_repo' path")
        self.script = script
        self.ssh_run = ssh_run
        self.remote_repo = remote_repo
        self.log_dir = log_dir or f"{remote_repo}/logs"
        self.pass_env_keys = pass_env_keys

    def _build_command(
        self,
        task_range: str,
        job_name: str,
        job_env: dict[str, str],
    ) -> list[str]:
        """Return qsub command as a single string for SSH execution."""
        parts = [
            "qsub",
            "-t",
            task_range,
            "-N",
            job_name,
            "-o",
            self.log_dir,
            "-j",
            "y",
        ]
        pass_vars = ",".join(f"{k}={v}" for k, v in job_env.items() if k in self.pass_env_keys)
        if pass_vars:
            parts += ["-v", pass_vars]
        parts.append(self.script)
        return parts

    def submit_array(
        self,
        job_name: str,
        total_chunks: int,
        tasks_per_array: int,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> None:
        """Submit array jobs in batches via SSH."""
        self.ssh_run(f"mkdir -p {self.log_dir}")

        start_task = 1
        while start_task <= total_chunks:
            end_task = min(start_task + tasks_per_array - 1, total_chunks)
            task_range = f"{start_task}-{end_task}"
            cmd_parts = self._build_command(task_range, job_name, job_env)
            cmd_str = " ".join(cmd_parts)
            remote_cmd = f"cd {self.remote_repo} && {cmd_str}"
            result = self.ssh_run(remote_cmd)
            if result.returncode != 0:
                stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
                raise RuntimeError(
                    f"Remote job submission failed (exit {result.returncode}) "
                    f"for array {task_range}:\n"
                    f"  command: {cmd_str}\n"
                    f"  stderr:  {stderr_msg}"
                )
            start_task = end_task + 1

    def submit_array_tracked(
        self,
        job_name: str,
        total_chunks: int,
        tasks_per_array: int,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> list[tuple[str, str]]:
        """Like submit_array but returns (task_range, job_id) pairs."""
        self.ssh_run(f"mkdir -p {self.log_dir}")
        submissions: list[tuple[str, str]] = []

        start_task = 1
        while start_task <= total_chunks:
            end_task = min(start_task + tasks_per_array - 1, total_chunks)
            task_range = f"{start_task}-{end_task}"
            cmd_parts = self._build_command(task_range, job_name, job_env)
            cmd_str = " ".join(cmd_parts)
            remote_cmd = f"cd {self.remote_repo} && {cmd_str}"
            result = self.ssh_run(remote_cmd)
            if result.returncode != 0:
                stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
                raise RuntimeError(
                    f"Remote job submission failed (exit {result.returncode}) "
                    f"for array {task_range}:\n"
                    f"  command: {cmd_str}\n"
                    f"  stderr:  {stderr_msg}"
                )
            match = re.search(r"(\d+)", result.stdout)
            if not match:
                raise RuntimeError(f"Could not parse job ID from qsub output: {result.stdout!r}")
            submissions.append((task_range, match.group(1)))
            start_task = end_task + 1

        return submissions
