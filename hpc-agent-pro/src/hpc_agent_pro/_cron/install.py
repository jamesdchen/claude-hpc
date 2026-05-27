"""``install-cron`` primitive — install the wait-predictor crontab entries.

Installs two crontab lines idempotently:

* Snapshot (every 5 minutes) — runs
  ``python -m hpc_agent_pro._cron.snapshot_squeue`` to write a
  column-projected, gzipped squeue snapshot into
  ``<experiment_dir>/.hpc/squeue_snapshots/``.
* Training (daily at 03:00) — runs
  ``python -m hpc_agent_pro._cron.extract_sacct_history`` followed by
  ``python -m hpc_agent_pro._cron.train_wait_predictor`` to refit the
  LightGBM-residual regression from accumulated snapshots + sacct
  history.

Idempotent: each line is fingerprinted with the module path, and an
existing line with the same fingerprint is left alone (the primitive
reports ``status: 'already-installed'`` for that line).

Shipped in the pro wheel — ``pip install hpc-agent-pro`` installs
both this primitive and the three cron-invoked modules it references,
so the cron lines work without an editable repo checkout on disk.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

_SNAPSHOT_MODULE = "hpc_agent_pro._cron.snapshot_squeue"
_TRAIN_MODULE = "hpc_agent_pro._cron.train_wait_predictor"
_SACCT_MODULE = "hpc_agent_pro._cron.extract_sacct_history"


def _read_crontab() -> str:
    """Return the current user's crontab as a string. Empty on no crontab."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, encoding="utf-8"
        )
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            "crontab binary not on PATH — install cron (e.g. `apt install cron`) "
            "or use systemd timers manually"
        ) from exc
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(body: str) -> None:
    subprocess.run(
        ["crontab", "-"],
        input=body,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _python_bin() -> str:
    """The interpreter the cron line should invoke.

    Returns ``sys.executable`` — the venv python pip-installed the pro
    package into. The cron line embeds the absolute path so a user's
    minimal cron environment (no PATH) reaches the right Python.
    """
    return sys.executable


def _install_line(
    crontab: str, line: str, fingerprint: str
) -> tuple[str, str]:
    """Return ``(new_crontab, status)`` after maybe-appending ``line``.

    ``status`` is ``"installed"`` if the line was added, or
    ``"already-installed"`` if a line containing ``fingerprint`` was
    already present.
    """
    if fingerprint in crontab:
        return crontab, "already-installed"
    # Ensure the existing crontab ends with a newline before appending.
    if crontab and not crontab.endswith("\n"):
        crontab += "\n"
    return crontab + line + "\n", "installed"


@primitive(
    name="install-cron",
    verb="scaffold",
    side_effects=[SideEffect("filesystem", "user crontab")],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="ssh_target",
    cli=CliShape(
        help=(
            "Install the wait-predictor cron entries (snapshot every 5 minutes, "
            "training daily at 03:00). Idempotent — re-running detects existing "
            "entries and skips them."
        ),
        args=(
            CliArg(
                "--ssh-target",
                type=str,
                required=True,
                help="SSH target the cron jobs hit (e.g. alice@cluster.example.edu).",
            ),
            CliArg(
                "--experiment-dir",
                type=str,
                default=None,
                help="Experiment directory the snapshots / log files land in (default: cwd).",
            ),
        ),
    ),
    agent_facing=True,
)
def install_cron(
    *,
    ssh_target: str,
    experiment_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Install the snapshot + training crontab entries idempotently.

    *ssh_target*: the SSH destination the cron jobs hit, e.g.
    ``alice@cluster.example.edu``.

    *experiment_dir*: the directory snapshots and log files land in.
    Defaults to ``Path.cwd()`` because cron inherits no shell context;
    pass an explicit path when running setup from outside the
    experiment.

    Returns a dict with per-line status (``installed`` /
    ``already-installed``) and the resolved interpreter path the cron
    lines embed.
    """
    if not ssh_target:
        raise errors.SpecInvalid("--ssh-target is required")

    if shutil.which("crontab") is None:
        raise errors.SpecInvalid(
            "crontab binary not on PATH — install cron (e.g. `apt install cron`) "
            "or use systemd timers manually"
        )

    exp_dir = Path(experiment_dir).expanduser().resolve() if experiment_dir else Path.cwd()
    if not exp_dir.is_dir():
        raise errors.SpecInvalid(f"experiment_dir does not exist: {exp_dir}")

    python = _python_bin()

    # Each line is fingerprinted by the module path; idempotency keys
    # off that. The fingerprint MUST appear in the literal line so the
    # ``in crontab`` substring check finds it.
    snapshot_line = (
        f'*/5 * * * * cd "{exp_dir}" && '
        f'"{python}" -m {_SNAPSHOT_MODULE} '
        f'--ssh-target "{ssh_target}" '
        f'--experiment-dir "{exp_dir}" '
        f'>> .hpc/snapshot_squeue.log 2>&1'
    )
    train_line = (
        f'0 3 * * * cd "{exp_dir}" && '
        f'"{python}" -m {_SACCT_MODULE} '
        f'--ssh-target "{ssh_target}" --since-days 30 '
        f'--out completed_jobs.json && '
        f'"{python}" -m {_TRAIN_MODULE} '
        f'--completed-jobs completed_jobs.json '
        f'--slot-counts slot_counts.json '
        f'--experiment-dir "{exp_dir}" '
        f'>> .hpc/train_wait_predictor.log 2>&1'
    )

    crontab = _read_crontab()
    crontab, snap_status = _install_line(crontab, snapshot_line, _SNAPSHOT_MODULE)
    crontab, train_status = _install_line(crontab, train_line, _TRAIN_MODULE)

    if "installed" in (snap_status, train_status):
        _write_crontab(crontab)

    # The @primitive envelope adds top-level ``ok``; the dict returned
    # here lands under ``data``. Including ``"ok": True`` inside would
    # produce ``{"ok": True, "data": {"ok": True, ...}}`` — a redundant
    # inner duplicate that also leaks into the setup primitive when it
    # ``**``-spreads this dict.
    return {
        "python_interpreter": python,
        "experiment_dir": str(exp_dir),
        "ssh_target": ssh_target,
        "lines": {
            "snapshot": {"status": snap_status, "schedule": "*/5 * * * *"},
            "training": {"status": train_status, "schedule": "0 3 * * *"},
        },
    }
