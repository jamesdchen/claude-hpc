"""PBS family (pbspro / torque) engine behaviour.

Curated from the PBS Pro/OpenPBS + TORQUE man pages, pbs-drmaa state
mapping, and Nextflow's PbsExecutor/PbsProExecutor. The two variants are
distinct families because they diverge structurally (array flag, index
env var, resource grammar, finished-state token, history query).
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.backends import get_backend, get_backend_class


def _noop_ssh(_cmd):
    from types import SimpleNamespace

    return SimpleNamespace(stdout="", stderr="", returncode=0)


def _backend(family, **over):
    kw = dict(script="cpu.pbs", ssh_run=_noop_ssh, remote_repo="/r", pass_env_keys=("K",))
    kw.update(over)
    return get_backend(family, **kw)


# --- class metadata --------------------------------------------------------


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_metadata(family):
    cls = get_backend_class(family)
    assert cls.scheduler_name == family
    assert cls.template_ext == ".pbs"
    assert cls.supports_test_only_eta is False
    # job-id regex captures the numeric sequence from <seq>.<server> / <seq>[].<server>
    assert cls.JOB_ID_REGEX.search("12345.pbsserver").group(1) == "12345"
    assert cls.JOB_ID_REGEX.search("12346[].hpcnode0").group(1) == "12346"


# --- submit command shape (the canonical fork split) -----------------------


def test_pbspro_submit_uses_J_and_joins_streams():
    b = _backend("pbspro")
    assert b._build_command("1-10", "job", {"K": "V"}) == [
        "qsub",
        "-J",
        "1-10",
        "-N",
        "job",
        "-o",
        "/r/logs",
        "-j",
        "oe",
        "-v",
        "K=V",
        "cpu.pbs",
    ]


def test_torque_submit_uses_t():
    b = _backend("torque")
    cmd = b._build_command("1-10", "job", {"K": "V"})
    assert cmd[:3] == ["qsub", "-t", "1-10"]
    assert "-J" not in cmd
    assert cmd[-1] == "cpu.pbs"


def test_pbs_v_comma_guard():
    from hpc_agent import errors

    b = _backend("pbspro", pass_env_keys=("MODULES",))
    with pytest.raises(errors.SpecInvalid, match="','"):
        b._build_command("1-1", "job", {"MODULES": "python/3.11,gcc/11"})


def test_pbs_dependency_flag():
    for fam in ("pbspro", "torque"):
        b = _backend(fam)
        assert b._build_dependency_flag(["12.s", "13.s"]) == ["-W", "depend=afterany:12.s:13.s"]
        assert b._build_dependency_flag([]) == []


# --- resource flags (the second fork split) --------------------------------


def _res(**kw):
    from hpc_agent._wire.workflows.submit_flow import SubmitResources

    return SubmitResources(**kw)


def test_pbspro_resource_select_syntax():
    b = _backend("pbspro")
    assert b.resource_flags(_res(cpus=8, mem_mb=4096, walltime_sec=7200)) == [
        "-l",
        "select=1:ncpus=8:mem=4096mb",
        "-l",
        "walltime=02:00:00",
    ]
    assert b.resource_flags(_res()) == []  # opt-in


def test_torque_resource_nodes_ppn_syntax():
    b = _backend("torque")
    assert b.resource_flags(_res(cpus=8, mem_mb=4096, walltime_sec=7200)) == [
        "-l",
        "nodes=1:ppn=8,mem=4096mb,walltime=02:00:00",
    ]
    assert b.resource_flags(_res()) == []


# --- state classification (live qstat tokens) ------------------------------

_PBS_CLASSIFY = [
    ("Q", "alive"),
    ("R", "alive"),
    ("E", "alive"),
    ("B", "alive"),
    ("T", "alive"),
    ("W", "alive"),
    ("M", "alive"),
    ("H", "held"),
    ("S", "held"),
    ("U", "held"),
]


@pytest.mark.parametrize(("state", "bucket"), _PBS_CLASSIFY)
@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_classify(family, state, bucket):
    assert get_backend_class(family).classify_scheduler_state(state) == bucket


# --- qstat -u parsing (id is <seq>.<server>[<idx>]) ------------------------

_QSTAT = (
    "Job id            Name   User  Time Use S Queue\n"
    "----------------  -----  ----  -------- - -----\n"
    "12345.pbsserver   job    a     01:00:00 R workq\n"
    "12347.pbsserver   prep   a     00:00:00 H workq\n"
    "12346[].pbsserver arr    a     10:00:00 B workq\n"
)


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_parse_alive_strips_server_and_brackets(family):
    cls = get_backend_class(family)
    alive = cls.parse_alive_output(_QSTAT, ["12345", "12346", "12347", "99999"])
    assert alive == {"12345", "12347", "12346"}


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_parse_states(family):
    cls = get_backend_class(family)
    states = cls.parse_scheduler_states(_QSTAT, ["12345", "12346", "12347"])
    assert states == {"12345": "R", "12347": "H", "12346": "B"}
    assert cls.classify_scheduler_state(states["12347"]) == "held"


# --- log paths reuse the SGE .o<id>.<idx> layout ---------------------------


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_log_paths(family):
    cls = get_backend_class(family)
    assert cls.stderr_log_path("/repo", "job", "555", 0) == "/repo/logs/job.o555.1"


# --- history (qstat -xf -> Exit_status) + minimal inspect snapshot ---------


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_query_jobs_dispatches_without_raising(family):
    from unittest.mock import patch

    # query_pbs shells out to qstat; with no real cluster it returns an empty
    # task map + a diagnostic error rather than raising.
    with patch(
        "hpc_agent.infra.backends.query.subprocess.run",
        side_effect=FileNotFoundError("qstat"),
    ):
        out = get_backend_class(family).query_jobs(["12345"])
    assert out["tasks"] == {}
    assert any(e["code"] == "qstat_unavailable" for e in out["errors"])


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_inspect_returns_minimal_snapshot(family):
    snap = get_backend_class(family).inspect_cluster("c", {})
    d = snap.to_dict()
    assert d["scheduler_kind"] == family
    assert d["nodes"] == []
    assert any(e["code"] == "pbs_inspect_minimal" for e in d["errors"])
