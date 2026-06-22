"""Monitor transport over a pure-API backend touches zero SSH (#337 Increment 4).

A pure-API backend (``requires_ssh=False``) has no login node, so reconcile /
status / logs must drive liveness and log-fetch through the backend's *instance*
hooks (``alive_job_ids`` / ``fetch_logs``) instead of SSH. Every test booby-traps
the SSH seams so any stray ``ssh_run`` / reporter call fails loudly; the built-in
SSH backends keep their existing paths (covered by the sibling reconcile/status
suites).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops.monitor import logs_atom
from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.ops.monitor import status as status_mod
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord


def _boom(*a: object, **k: object) -> object:
    raise AssertionError("pure-API monitor path must not touch SSH")


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


@pytest.fixture
def fake_backend():
    """Register a pure-API backend whose hooks stand in for the cluster."""

    class _FakeMon(HPCBackend):
        scheduler_name = "fakemonbackend"
        requires_ssh = False
        alive_return: list[str] = []

        @classmethod
        def from_build_context(cls, ctx: object) -> _FakeMon:
            return cls()

        def _build_command(self, *a: object, **k: object) -> object:
            raise NotImplementedError

        def alive_job_ids(self, job_ids: list[str]) -> list[str]:
            return list(type(self).alive_return)

        def fetch_logs(self, run_id: str, dest_dir: str | None = None) -> str:
            d = Path(dest_dir or ".")
            d.mkdir(parents=True, exist_ok=True)
            f = d / "task-0.log"
            f.write_text("synthetic api log", encoding="utf-8")
            return str(f)

    backends.register("fakemonbackend")(_FakeMon)
    try:
        yield _FakeMon
    finally:
        backends._REGISTRY.pop("fakemonbackend", None)


def _record(run_id: str, *, job_ids=("1", "2")) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=4,
        submitted_at="2026-06-04T00:00:00Z",
        experiment_dir="/exp",
        status="in_flight",
        backend="fakemonbackend",
    )


def _trap_reconcile_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", _boom)
    monkeypatch.setattr(recon, "_ssh_status_report", _boom)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", _boom)
    monkeypatch.setattr(recon.remote, "ssh_run", _boom)


def test_reconcile_marks_abandoned_via_backend_alive_hook(tmp_path, monkeypatch, fake_backend):
    _trap_reconcile_ssh(monkeypatch)
    fake_backend.alive_return = []  # nothing alive → abandoned
    upsert_run(tmp_path, _record("r-abandon"))

    record, alive_failed = recon._reconcile_one(tmp_path, "r-abandon", scheduler="fakemonbackend")

    assert alive_failed is False
    assert record.status == "abandoned"
    # combiner waves untouched (no wave-listing on the pure-API path).
    assert record.combined_waves == []


def test_reconcile_keeps_in_flight_when_jobs_alive(tmp_path, monkeypatch, fake_backend):
    _trap_reconcile_ssh(monkeypatch)
    fake_backend.alive_return = ["1"]  # one job still alive → not abandoned
    upsert_run(tmp_path, _record("r-live"))

    record, alive_failed = recon._reconcile_one(tmp_path, "r-live", scheduler="fakemonbackend")

    assert alive_failed is False
    assert record.status == "in_flight"


def test_record_status_derives_liveness_with_zero_ssh(tmp_path, monkeypatch, fake_backend):
    monkeypatch.setattr(status_mod, "_ssh_status_report", _boom)
    fake_backend.alive_return = ["1"]
    upsert_run(tmp_path, _record("r-status"))

    record = status_mod.record_status(
        tmp_path,
        "r-status",
        ssh_target="u@h",
        remote_path="/remote",
        job_ids=["1", "2"],
        job_name="j",
    )

    summary = record.last_status or {}
    assert summary["in_flight"] is True
    assert summary["alive_job_ids"] == ["1"]
    assert "checked_at" in summary


def test_fetch_logs_pulls_via_backend_hook_with_zero_ssh(tmp_path, monkeypatch, fake_backend):
    monkeypatch.setattr(logs_atom, "fetch_task_logs", _boom)
    monkeypatch.setattr(logs_atom, "_ssh_status_report", _boom)
    upsert_run(tmp_path, _record("r-logs"))

    data = logs_atom.fetch_logs(
        experiment_dir=tmp_path,
        run_id="r-logs",
        task_ids=[0],
    )

    assert data["scheduler"] == "fakemonbackend"
    assert len(data["logs"]) == 1
    written = Path(data["logs"][0]["path"])
    assert written.read_text(encoding="utf-8") == "synthetic api log"
    # journal record is intact (load proves the run still resolves).
    assert load_run(tmp_path, "r-logs") is not None
