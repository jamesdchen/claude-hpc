"""Pre-submit spec verification dump (#212).

The earliest a researcher can today eyeball the *exact* parameters a Tier 2
job will run with is after it has already cost an rsync + qsub + canary
cycle. ``submit-flow --dry-run`` validates a spec and reports
``would_launch`` but leaves nothing on disk to read. This module closes that
gap: the dry-run gate writes the full resolved spec to a JSON file in a
designated folder so a human can open it, verify the grid / cluster /
resources / walltime, and only *then* re-run without ``--dry-run`` to submit.
That is the human-in-the-loop gate the issue asks for — a verification file
that exists **before** any cluster compute is spent on a bad parameter grid.

A *real* submit writes the same file too (best-effort, in the flow's
pre-rsync prelude — see ``submit_flow._dump_submit_spec``), recording the
effective spec actually sent as a provenance artifact keyed by run_id.

It complements the local *execution* gate (``dry-run-local`` / #205): that
one proves the executor *runs*; this one shows *what* it will run.

Designated folder
-----------------
The dump is kept OUT of the experiment tree so it never rides the rsync
bundle to the cluster. Default: ``<journal_home>/<repo_hash>/submit_specs/``
(the per-repo agent state, i.e. ``~/.claude/hpc/...``). Set
``HPC_SPEC_DUMP_DIR`` to redirect the files somewhere more visible (e.g. a
shared review folder); the absolute path is always reported back on the
dry-run envelope (``spec_dump_path``) so it is discoverable wherever it lands.

The file is named ``<run_id>.json`` and is the spec *verbatim* — a valid,
re-submittable ``submit-flow`` spec, so a researcher can review it, hand-edit
a parameter, and feed it straight back to ``submit-flow``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["spec_dump_dir", "write_spec_dump"]


def spec_dump_dir(experiment_dir: Path) -> Path:
    """Return (and create) the designated folder for pre-submit spec dumps.

    ``HPC_SPEC_DUMP_DIR`` wins when set to a non-empty value; otherwise the
    per-repo journal home's ``submit_specs/`` subdir — never inside
    *experiment_dir*, so the dump is not swept into the rsync deploy bundle.
    """
    from pathlib import Path as _Path

    override = os.environ.get("HPC_SPEC_DUMP_DIR")
    if override and override.strip():
        target = _Path(override).expanduser()
    else:
        from hpc_agent.state.run_record import journal_dir

        target = journal_dir(experiment_dir) / "submit_specs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_spec_dump(experiment_dir: Path, *, run_id: str, spec: dict[str, Any]) -> Path:
    """Write *spec* to ``<spec_dump_dir>/<run_id>.json`` and return the path.

    *run_id* is validated against the filesystem-safe run_id guard
    (:func:`hpc_agent.state.runs.validate_run_id`) before it becomes a file
    name, so a malformed id can't traverse out of the dump folder. The write
    is atomic and the JSON is pretty-printed with sorted keys (via
    :func:`hpc_agent.infra.io.atomic_write_json`) so a reviewer reads stable,
    diff-friendly output.
    """
    from hpc_agent.infra.io import atomic_write_json
    from hpc_agent.state.runs import validate_run_id

    validate_run_id(run_id)
    path = spec_dump_dir(experiment_dir) / f"{run_id}.json"
    atomic_write_json(path, dict(spec))
    return path
