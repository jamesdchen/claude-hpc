"""Auto-resolve a :class:`SchedulerProfile` for an unknown scheduler.

This is the live half of the resolver seam that
:func:`hpc_agent.models.mapreduce.reduce.status.resolve_scheduler_profile`
delegates to for an unrecognised scheduler. The loop is:

    probe -> seed-from-nearest-golden -> author (LLM) -> offline-validate
          -> canary (live 1-task submit) -> return (caller pins)

Every step is driven through injected callables (``ssh_run`` for the
cluster, ``llm`` for authoring) so the whole module is unit-testable with
stubs; only a real ``canary_validate`` pass needs an actual cluster.

The ``family`` of any resolved profile is always one of
:data:`hpc_agent.infra.backends.profile.KNOWN_FAMILIES` — the LLM only
customises *data* (regex, scripts, error vocabulary), never the
structural command grammar, so a misauthored profile can't make the
engine emit something it doesn't understand.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.infra.backends.profile import (
    SGE_PROFILE,
    SLURM_PROFILE,
    SchedulerProfile,
)

if TYPE_CHECKING:
    import subprocess

# A callable that runs one shell command on the cluster login node and
# returns its CompletedProcess (the same shape ``infra.remote.ssh_run``
# yields). Injected so tests can stub the cluster.
SshRun = Callable[[str], "subprocess.CompletedProcess[str]"]
# Authoring hook: given a prompt, return a JSON object string of the
# SchedulerProfile fields to override on the seed. Provider-agnostic — an
# in-process LLM client or an agent-driven CLI verb both fit.
Llm = Callable[[str], str]


# ---------------------------------------------------------------------------
# Phase 1 — probe
# ---------------------------------------------------------------------------

# Submit binary -> family. Order matters only for the (rare) cluster that
# ships more than one; sbatch wins because a SLURM site sometimes also has
# a legacy qsub wrapper, not the reverse.
_BIN_FAMILY = (("sbatch", "slurm"), ("qsub", "sge"), ("bsub", "lsf"))


@dataclass(frozen=True)
class ProbeResult:
    """What the login node told us about its scheduler."""

    binaries: dict[str, str] = field(default_factory=dict)  # bin -> path
    family: str | None = None  # inferred known family, else None
    versions: dict[str, str] = field(default_factory=dict)  # bin -> version banner
    raw: dict[str, str] = field(default_factory=dict)  # cmd -> stdout (for the LLM)


def probe_cluster(ssh_run: SshRun) -> ProbeResult:
    """Detect scheduler binaries + versions on the login node.

    Pure I/O via *ssh_run*; the parsing is deterministic and tested.
    """

    def _run(cmd: str) -> str:
        try:
            cp = ssh_run(cmd)
        except Exception:  # noqa: BLE001 — a probe failure is just "absent"
            return ""
        return (getattr(cp, "stdout", "") or "").strip()

    binaries: dict[str, str] = {}
    versions: dict[str, str] = {}
    raw: dict[str, str] = {}
    for bin_name, _fam in _BIN_FAMILY:
        path = _run(f"command -v {bin_name}")
        raw[f"command -v {bin_name}"] = path
        if path:
            binaries[bin_name] = path.splitlines()[0].strip()

    # Version banners (only for binaries that exist) — useful context for
    # the LLM and for distinguishing forks (Univa/SoG/OGS, Slurm vs flux).
    _version_cmd = {"sbatch": "sbatch --version", "qsub": "qsub -help", "bsub": "bsub -V"}
    for bin_name in binaries:
        cmd = _version_cmd.get(bin_name)
        if cmd:
            out = _run(cmd)
            raw[cmd] = out
            if out:
                versions[bin_name] = out.splitlines()[0].strip()

    family: str | None = None
    for bin_name, fam in _BIN_FAMILY:
        if bin_name in binaries and fam in {"slurm", "sge"}:
            family = fam
            break
    return ProbeResult(binaries=binaries, family=family, versions=versions, raw=raw)


# ---------------------------------------------------------------------------
# Phase 2 — seed from nearest golden
# ---------------------------------------------------------------------------


def seed_profile_for_probe(probe: ProbeResult) -> SchedulerProfile:
    """Return the golden profile nearest to *probe* (the LLM's starting point).

    Raises :class:`~hpc_agent.errors.SpecInvalid` when the probe found no
    recognisable submit binary — there is nothing to seed from, and guessing
    a family blind would be worse than failing loudly.
    """
    if probe.family == "slurm":
        return SLURM_PROFILE
    if probe.family == "sge":
        return SGE_PROFILE
    raise errors.SpecInvalid(
        "cluster probe found no sbatch/qsub-family scheduler "
        f"(binaries={sorted(probe.binaries)}); cannot seed a profile. "
        "If this is a known scheduler, pin a 'scheduler_profile' in clusters.yaml."
    )


# ---------------------------------------------------------------------------
# Phase 3 — author (LLM) + offline validation
# ---------------------------------------------------------------------------

# Fields the LLM is allowed to override. ``family`` and ``scripts`` are
# deliberately excluded: family is structural (engine-bound) and the seed's
# scripts already render correctly for the family; an LLM rewriting bash is
# a much larger risk surface than tweaking a regex.
_AUTHORABLE_FIELDS = frozenset(
    {"name", "submit_bin", "job_id_regex", "template_ext", "supports_test_only_eta", "error_states"}
)


def _author_prompt(seed: SchedulerProfile, probe: ProbeResult) -> str:
    return (
        "You are configuring an HPC scheduler profile. Below is a SEED profile "
        f"for the nearest known family ({seed.family!r}) and the raw output of "
        "probing the cluster's login node. Return ONLY a JSON object containing "
        "the fields that DIFFER from the seed for this cluster's scheduler. "
        f"Allowed keys: {sorted(_AUTHORABLE_FIELDS)}. Do not change 'family'.\n\n"
        f"SEED:\n{json.dumps(seed.to_dict(), indent=2, sort_keys=True)}\n\n"
        f"PROBE:\n{json.dumps(probe.raw, indent=2, sort_keys=True)}\n"
    )


def author_profile(seed: SchedulerProfile, probe: ProbeResult, llm: Llm) -> SchedulerProfile:
    """Ask *llm* to customise *seed* for this cluster; merge + rebuild.

    The LLM returns a JSON object of overrides; unknown / disallowed keys are
    ignored, ``family`` and ``scripts`` are pinned to the seed, and the result
    is rebuilt via ``SchedulerProfile.from_dict`` (which validates shape).
    """
    raw = llm(_author_prompt(seed, probe))
    try:
        overrides = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise errors.SpecInvalid(f"profile-authoring LLM did not return valid JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise errors.SpecInvalid("profile-authoring LLM must return a JSON object")

    merged = seed.to_dict()
    for key, value in overrides.items():
        if key in _AUTHORABLE_FIELDS:
            merged[key] = value
    # Pin the non-authorable, structural fields back to the seed.
    merged["family"] = seed.family
    merged["scripts"] = dict(seed.scripts)
    return SchedulerProfile.from_dict(merged)


# Representative submit-stdout + state tokens per family, used as the offline
# oracle so a candidate profile is sanity-checked before any cluster contact.
_FAMILY_SAMPLES = {
    "slurm": {
        "submit": "Submitted batch job 12345",
        "alive_states": ["RUNNING", "PENDING"],
        "error_states": ["FAILED", "TIMEOUT"],
    },
    "sge": {
        "submit": 'Your job-array 12345.1-10:1 ("job") has been submitted',
        "alive_states": ["r", "qw"],
        "error_states": ["Eqw"],
    },
}


def validate_profile_offline(profile: SchedulerProfile) -> list[str]:
    """Return a list of problems with *profile*, empty if it passes.

    The offline gate the canary can't be: a compilable, *matching* job-id
    regex and a state classifier that buckets the family's known tokens
    correctly. Run before any live submission so a broken regex is caught
    without burning a cluster job.
    """
    from hpc_agent.infra.backends import build_backend_class

    problems: list[str] = []
    try:
        rx = re.compile(profile.job_id_regex)
    except re.error as exc:
        return [f"job_id_regex does not compile: {exc}"]

    sample = _FAMILY_SAMPLES.get(profile.family, {})
    submit_line = sample.get("submit")
    if submit_line:
        m = rx.search(submit_line)
        if not m or not m.groups():
            problems.append(
                f"job_id_regex {profile.job_id_regex!r} does not capture a job id "
                f"from a representative submit line: {submit_line!r}"
            )

    cls = build_backend_class(profile, remote=True)
    for state in sample.get("alive_states", []):
        if cls.classify_scheduler_state(state) == "error":
            problems.append(f"state {state!r} misclassified as error (expected alive/held)")
    for state in sample.get("error_states", []):
        if cls.classify_scheduler_state(state) != "error":
            problems.append(f"error state {state!r} not classified as error")
    return problems


# ---------------------------------------------------------------------------
# Phase 4 — canary (live; mockable via ssh_run)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanaryResult:
    ok: bool
    job_id: str | None = None
    parsed: bool = False
    found_alive: bool = False
    log_found: bool = False
    detail: str = ""


# A trivial canary job body — validates the submit/parse/log plumbing
# without depending on the user's executor. Written to the remote repo and
# submitted as a 1-task array.
_CANARY_BODY = '#!/bin/bash\necho "hpc-agent canary ok on $(hostname)"\n'


def canary_validate(
    profile: SchedulerProfile,
    *,
    ssh_run: SshRun,
    remote_repo: str,
    job_name: str = "hpc_canary",
    log_poll: Callable[[str], bool] | None = None,
) -> CanaryResult:
    """Submit ONE trivial task through *profile* and confirm the plumbing.

    Hard gates: the submit stdout must yield a job id via the profile's
    regex, and the task's stderr log must appear on disk. The alive check is
    informational (a sub-second canary may already be gone). Everything runs
    through *ssh_run*, so this is exercised in tests with a stubbed cluster;
    a real *pass* requires an actual login node.

    *log_poll* (injected for testability) decides whether the expected log
    path exists; it defaults to a single ``test -f`` over ssh.
    """
    from hpc_agent.infra.backends import build_backend_class

    backend_cls = build_backend_class(profile, remote=True)
    script_name = f".hpc/{job_name}{profile.template_ext}"
    backend = backend_cls(
        script=script_name,
        ssh_run=ssh_run,
        remote_repo=remote_repo,
    )

    # Materialise the canary script on the remote (heredoc avoids quoting pain).
    ssh_run(f"mkdir -p {remote_repo}/.hpc")
    ssh_run(f"cat > {remote_repo}/{script_name} <<'HPC_EOF'\n{_CANARY_BODY}HPC_EOF")

    # Submit a 1-task array; submit_array_tracked enforces the job-id parse
    # (raises if profile.JOB_ID_REGEX doesn't match the submit stdout).
    try:
        from pathlib import Path

        submissions = backend.submit_array_tracked(
            job_name, total_tasks=1, tasks_per_array=1, job_env={}, cwd=Path(remote_repo)
        )
    except RuntimeError as exc:
        return CanaryResult(ok=False, parsed=False, detail=f"submit/parse failed: {exc}")
    if not submissions:
        return CanaryResult(ok=False, parsed=False, detail="no submission recorded")
    job_id = submissions[0][1]

    # Alive check (informational).
    found_alive = False
    try:
        alive_cmd = backend_cls.build_alive_check_cmd([job_id])
        alive_out = getattr(ssh_run(alive_cmd), "stdout", "") or ""
        found_alive = job_id in backend_cls.parse_alive_output(alive_out, [job_id])
    except Exception:  # noqa: BLE001 — informational only
        found_alive = False

    # Log existence (hard gate). task_id 0 -> on-disk index 1.
    log_path = backend_cls.stderr_log_path(remote_repo, job_name, job_id, 0)
    if log_poll is None:

        def log_poll(path: str) -> bool:
            out = getattr(ssh_run(f"test -f {path} && echo OK"), "stdout", "") or ""
            return "OK" in out

    log_found = bool(log_poll(log_path))
    ok = log_found  # parse already enforced above
    detail = "ok" if ok else f"canary log not found at {log_path}"
    return CanaryResult(
        ok=ok,
        job_id=job_id,
        parsed=True,
        found_alive=found_alive,
        log_found=log_found,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def resolve_unknown_scheduler(
    scheduler: str,
    *,
    ssh_run: SshRun,
    llm: Llm | None = None,
    remote_repo: str | None = None,
    run_canary: bool = True,
) -> SchedulerProfile:
    """Full loop: probe -> seed -> author -> offline-validate -> canary.

    *llm* is required (the authoring step); when absent the seed is used
    verbatim (useful when the nearest golden already fits, e.g. a SLURM fork
    whose submit shape is unchanged). *run_canary* requires *remote_repo*.
    Raises :class:`~hpc_agent.errors.SpecInvalid` on any gate failure so a
    bad profile is never returned (and therefore never pinned).
    """
    probe = probe_cluster(ssh_run)
    seed = seed_profile_for_probe(probe)
    profile = author_profile(seed, probe, llm) if llm is not None else seed

    problems = validate_profile_offline(profile)
    if problems:
        raise errors.SpecInvalid(
            f"resolved profile for {scheduler!r} failed offline validation: " + "; ".join(problems)
        )

    if run_canary:
        if not remote_repo:
            raise errors.SpecInvalid("canary validation requires remote_repo")
        result = canary_validate(profile, ssh_run=ssh_run, remote_repo=remote_repo)
        if not result.ok:
            raise errors.SpecInvalid(
                f"canary validation failed for resolved profile {profile.name!r}: {result.detail}"
            )
    return profile
