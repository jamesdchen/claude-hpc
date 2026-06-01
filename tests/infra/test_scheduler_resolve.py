"""Unit tests for the auto-resolve loop (probe -> seed -> author ->
offline-validate -> canary), all driven through a stubbed cluster/LLM."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra import scheduler_resolve as sr
from hpc_agent.infra.backends.profile import SGE_PROFILE, SLURM_PROFILE


def _cp(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class FakeSsh:
    """Command-dispatching fake ``ssh_run``."""

    def __init__(self, *, present=(), submit_stdout="", alive_stdout="", log_ok=True):
        self.present = set(present)  # binaries that `command -v` finds
        self.submit_stdout = submit_stdout
        self.alive_stdout = alive_stdout
        self.log_ok = log_ok
        self.calls: list[str] = []

    def __call__(self, cmd: str):
        self.calls.append(cmd)
        if cmd.startswith("command -v "):
            b = cmd.split()[-1]
            return _cp(f"/usr/bin/{b}" if b in self.present else "")
        if "--version" in cmd or "-help" in cmd or cmd.endswith("-V"):
            return _cp("scheduler 1.2.3")
        if "sbatch" in cmd or "qsub" in cmd:
            return _cp(self.submit_stdout)
        if "squeue" in cmd or "qstat" in cmd:
            return _cp(self.alive_stdout)
        if "test -f" in cmd:
            return _cp("OK" if self.log_ok else "")
        return _cp("")


# --- Phase 1: probe --------------------------------------------------------


def test_probe_detects_slurm():
    p = sr.probe_cluster(FakeSsh(present=("sbatch",)))
    assert p.family == "slurm"
    assert "sbatch" in p.binaries


def test_probe_detects_sge():
    p = sr.probe_cluster(FakeSsh(present=("qsub",)))
    assert p.family == "sge"


def test_probe_unknown_when_no_known_binary():
    p = sr.probe_cluster(FakeSsh(present=("bsub",)))
    assert p.family is None  # lsf is not an engine family


# --- Phase 2: seed ---------------------------------------------------------


def test_seed_slurm_and_sge():
    assert sr.seed_profile_for_probe(sr.ProbeResult(family="slurm")) is SLURM_PROFILE
    assert sr.seed_profile_for_probe(sr.ProbeResult(family="sge")) is SGE_PROFILE


def test_seed_raises_without_family():
    with pytest.raises(errors.SpecInvalid, match="no sbatch/qsub"):
        sr.seed_profile_for_probe(sr.ProbeResult(family=None))


# --- Phase 3: author + offline-validate ------------------------------------


def test_author_merges_overrides_and_pins_structure():
    probe = sr.ProbeResult(family="slurm", raw={"x": "y"})

    def llm(_prompt):
        return json.dumps({"name": "discovery2", "error_states": ["FAILED", "BOOM"]})

    prof = sr.author_profile(SLURM_PROFILE, probe, llm)
    assert prof.name == "discovery2"
    assert "BOOM" in prof.error_states
    assert prof.family == "slurm"  # structural fields pinned to seed
    assert prof.scripts["cpu"] == SLURM_PROFILE.scripts["cpu"]


def test_author_ignores_disallowed_family_override():
    def llm(_p):
        return json.dumps({"family": "sge", "name": "x"})

    prof = sr.author_profile(SLURM_PROFILE, sr.ProbeResult(family="slurm"), llm)
    assert prof.family == "slurm"  # family override ignored


def test_author_rejects_non_json():
    with pytest.raises(errors.SpecInvalid, match="valid JSON"):
        sr.author_profile(SLURM_PROFILE, sr.ProbeResult(family="slurm"), lambda _p: "not json")


def test_validate_offline_passes_golden():
    assert sr.validate_profile_offline(SLURM_PROFILE) == []
    assert sr.validate_profile_offline(SGE_PROFILE) == []


def test_validate_offline_flags_nonmatching_regex():
    from hpc_agent.infra.backends.profile import SchedulerProfile

    bad = SchedulerProfile.from_dict({**SLURM_PROFILE.to_dict(), "job_id_regex": r"NOPE (\d+)"})
    problems = sr.validate_profile_offline(bad)
    assert any("does not capture" in p for p in problems)


def test_validate_offline_flags_uncompilable_regex():
    from hpc_agent.infra.backends.profile import SchedulerProfile

    bad = SchedulerProfile.from_dict({**SLURM_PROFILE.to_dict(), "job_id_regex": r"("})
    problems = sr.validate_profile_offline(bad)
    assert problems and "compile" in problems[0]


# --- Phase 4: canary -------------------------------------------------------


def test_canary_passes_with_parseable_submit_and_log():
    ssh = FakeSsh(submit_stdout="Submitted batch job 999", alive_stdout="999", log_ok=True)
    res = sr.canary_validate(SLURM_PROFILE, ssh_run=ssh, remote_repo="/scratch/u")
    assert res.ok and res.parsed and res.job_id == "999" and res.log_found


def test_canary_fails_when_jobid_unparseable():
    ssh = FakeSsh(submit_stdout="garbage with no id", log_ok=True)
    res = sr.canary_validate(SLURM_PROFILE, ssh_run=ssh, remote_repo="/scratch/u")
    assert not res.ok and not res.parsed


def test_canary_fails_when_log_missing():
    ssh = FakeSsh(submit_stdout="Submitted batch job 7", log_ok=False)
    res = sr.canary_validate(SLURM_PROFILE, ssh_run=ssh, remote_repo="/scratch/u")
    assert not res.ok and res.parsed and not res.log_found


# --- orchestrator ----------------------------------------------------------


def test_resolve_unknown_end_to_end():
    ssh = FakeSsh(present=("sbatch",), submit_stdout="Submitted batch job 42", log_ok=True)

    def llm(_p):
        return json.dumps({"name": "newslurm"})

    prof = sr.resolve_unknown_scheduler(
        "newslurm", ssh_run=ssh, llm=llm, remote_repo="/scratch/u", run_canary=True
    )
    assert prof.name == "newslurm" and prof.family == "slurm"


def test_resolve_unknown_without_llm_uses_seed():
    ssh = FakeSsh(
        present=("qsub",),
        submit_stdout='Your job-array 5.1-1:1 ("j") has been submitted',
    )
    prof = sr.resolve_unknown_scheduler(
        "weirdsge", ssh_run=ssh, llm=None, remote_repo="/scratch/u", run_canary=True
    )
    assert prof is SGE_PROFILE  # seed used verbatim


def test_resolve_unknown_canary_requires_remote_repo():
    ssh = FakeSsh(present=("sbatch",), submit_stdout="Submitted batch job 1")
    with pytest.raises(errors.SpecInvalid, match="remote_repo"):
        sr.resolve_unknown_scheduler("x", ssh_run=ssh, llm=None, run_canary=True)


# --- resolve_for_setup (CLI entry) -----------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Restore the global backend registry after each test (resolve_for_setup
    registers resolved profiles)."""
    from hpc_agent.infra.backends import _REGISTRY, _populate_registry

    _populate_registry()
    snap = dict(_REGISTRY)
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(snap)


def _slurm_cfg(**extra):
    return {"scheduler": "slurm", "scratch": "/scratch/u", **extra}


def _custom_slurm_dict(name="discovery2"):
    # Differs from golden by name (enough to be "custom"); keeps the golden
    # error vocabulary so it passes offline validation.
    return {**SLURM_PROFILE.to_dict(), "name": name}


def test_setup_known_slurm_resolves_without_canary():
    ssh = FakeSsh(present=("sbatch",), submit_stdout="Submitted batch job 1")
    res = sr.resolve_for_setup("c", "/tmp/exp", cfg=_slurm_cfg(), ssh_run=ssh)
    assert res["status"] == "resolved"
    assert res["custom"] is False and res["canaried"] is False
    # A standard golden cluster submits NO canary job.
    assert not any("sbatch" in c for c in ssh.calls)


def test_setup_authored_profile_canaries_and_pins(tmp_path):
    ssh = FakeSsh(present=("sbatch",), submit_stdout="Submitted batch job 5", log_ok=True)
    res = sr.resolve_for_setup(
        "c",
        tmp_path,
        cfg=_slurm_cfg(),
        ssh_run=ssh,
        scheduler_profile_json=json.dumps(_custom_slurm_dict("disc")),
    )
    assert res["status"] == "resolved" and res["custom"] and res["canaried"]

    from hpc_agent.infra.backends import get_backend_class

    assert get_backend_class("disc").profile.name == "disc"
    meta = json.loads((tmp_path / "experiment_meta.json").read_text())
    assert meta["scheduler_profile"]["name"] == "disc"


def test_setup_authored_invalid_regex_reports_invalid():
    ssh = FakeSsh(present=("sbatch",))
    bad = {**_custom_slurm_dict("x"), "job_id_regex": r"NOPE (\d+)"}
    res = sr.resolve_for_setup(
        "c", "/tmp/exp", cfg=_slurm_cfg(), ssh_run=ssh, scheduler_profile_json=json.dumps(bad)
    )
    assert res["status"] == "invalid"


def test_setup_authored_canary_failure_escalates():
    ssh = FakeSsh(present=("sbatch",), submit_stdout="Submitted batch job 5", log_ok=False)
    res = sr.resolve_for_setup(
        "c",
        "/tmp/exp",
        cfg=_slurm_cfg(),
        ssh_run=ssh,
        scheduler_profile_json=json.dumps(_custom_slurm_dict("disc")),
    )
    assert res["status"] == "needs_authoring" and "canary" in res["reason"]
    assert res["seed"]["family"] == "slurm" and res["prompt"]


def test_setup_cfg_pin_canaries():
    ssh = FakeSsh(present=("sbatch",), submit_stdout="Submitted batch job 9", log_ok=True)
    res = sr.resolve_for_setup(
        "c",
        "/tmp/exp",
        cfg=_slurm_cfg(scheduler_profile=_custom_slurm_dict("pinned")),
        ssh_run=ssh,
    )
    assert res["status"] == "resolved" and res["custom"] and res["canaried"]


def test_setup_skipped_without_derivable_ssh_target():
    res = sr.resolve_for_setup("c", "/tmp/exp", cfg={"scheduler": "slurm"})
    assert res["status"] == "skipped"
