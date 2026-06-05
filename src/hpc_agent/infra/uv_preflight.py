"""Shared cluster-side ``uv``-on-PATH preflight probe.

ONE implementation of the ``command -v uv`` check, in ``infra`` so both
subjects that need it can call it without a cross-subject import (#275):

* ``ops/submit_flow`` runs it before the canary qsub (raises ``SpecInvalid``
  so a ``runtime=uv`` job with no ``uv`` aborts before reaching the cluster).
* ``ops/preflight/check`` runs it from ``check-preflight --cluster --runtime
  uv`` so the same guard is reachable at the worker's Step 6b gate (#275
  Fix 1), surfacing a uv-less env as a check rather than a mid-submit abort.

``ops/preflight`` may not import ``ops/submit_flow`` (the subject-import
boundary), so the canonical probe lives here; both callers import it from
``infra``. The probe reproduces the cluster preamble's activation sequence
(``module load`` → ``source $CONDA_SOURCE`` → ``conda activate $CONDA_ENV``)
then ``command -v uv``, so a green probe means uv is present on the SAME PATH
the job will see.
"""

from __future__ import annotations

from hpc_agent import errors
from hpc_agent.infra.remote import ssh_run


def preflight_runtime_check(
    ssh_target: str,
    *,
    job_env: dict[str, str],
    skip: bool,
) -> None:
    """When ``HPC_RUNTIME=uv``, verify ``uv`` is on PATH after activation.

    Reproduces the job preamble's ``module load $MODULES`` → ``source
    $CONDA_SOURCE`` → ``conda activate $CONDA_ENV`` → ``command -v uv``
    sequence over a single SSH round-trip at submit time, turning "all 100
    tasks fail with ``[template] HPC_RUNTIME=uv but 'uv' not on PATH``" into a
    single :class:`errors.SpecInvalid` with an actionable remediation.

    Reads activation fields from *job_env*. A no-op when ``HPC_RUNTIME`` is not
    ``"uv"`` (no other runtime triggers a binary-availability constraint) or
    when *skip* is set. Raises :class:`errors.SpecInvalid` when ``uv`` is
    absent; returns ``None`` on success.
    """
    if skip or job_env.get("HPC_RUNTIME") != "uv":
        return

    modules = (job_env.get("MODULES") or "").strip()
    conda_source = (job_env.get("CONDA_SOURCE") or "").strip()
    conda_env = (job_env.get("CONDA_ENV") or "").strip()

    parts: list[str] = []
    if modules:
        parts.append(f"module load {modules}")
    if conda_source:
        parts.append(f"source {conda_source}")
    if conda_env:
        parts.append(f"conda activate {conda_env}")
    parts.append("command -v uv")
    cmd = " && ".join(parts)

    probe = ssh_run(cmd, ssh_target=ssh_target)
    if probe.returncode != 0 or not (probe.stdout or "").strip():
        env_hint = (
            f"~/.conda/envs/{conda_env}/bin/pip install uv" if conda_env else "pip install uv"
        )
        raise errors.SpecInvalid(
            f"preflight: runtime=uv but `uv` was not found on PATH after activating "
            f"the cluster env on {ssh_target}. Without it, every task fails "
            f"`[template] HPC_RUNTIME=uv but 'uv' not on PATH`. Install uv into the "
            f"env (e.g. `{env_hint}`) and resubmit, OR drop `runtime: uv` from the "
            f"spec if the repo doesn't actually need uv. "
            f"Activation command attempted: `{cmd}` (exit {probe.returncode}; "
            f"stderr: {(probe.stderr or '').strip()[:200]})."
        )
