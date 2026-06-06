"""Composite tests for the #299 auto-resume auto-fire (``maybe_auto_resume``).

The pure gate (:func:`decide_auto_resume`) is exhaustively covered in
``test_auto_resume.py``. This file pins the *composite* that turns a
``"resume"`` verdict into an actual resubmit: the ``resubmit_flow`` call is
mocked, so these tests assert the wiring (which ids, which flags, the cap
counter, dedup) without touching a cluster.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.ops.auto_resume_flow import maybe_auto_resume
from hpc_agent.state import run_record
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import run_sidecar_path

_RUN_ID = "20260606-120000-aaa"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_record(experiment_dir: Path, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-06-06T12:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "script": ".hpc/templates/cpu_array.sh",
        "backend": "slurm",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        "auto_resume_on_kill": True,
        "max_auto_resumes": 2,
        "auto_resume_count": 0,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _write_sidecar(
    experiment_dir: Path, *, preempted: list[int], other: dict | None = None
) -> None:
    tasks: dict[str, dict] = {
        str(i): {"preempt": {"at": f"2026-06-06T12:0{i}:00Z", "grace_sec": 25}} for i in preempted
    }
    if other:
        tasks.update(other)
    path = run_sidecar_path(experiment_dir, _RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tasks": tasks, "task_count": 4}), encoding="utf-8")


class _Recorder:
    """Records resubmit() calls and returns a stub result."""

    def __init__(self, *, deduped: bool = False, new_job_ids: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._deduped = deduped
        self._new_job_ids = new_job_ids or ["9100"]

    def __call__(self, experiment_dir: Path, run_id: str, **kwargs: Any) -> Any:
        self.calls.append({"experiment_dir": experiment_dir, "run_id": run_id, **kwargs})

        class _Result:
            deduped = self._deduped
            cluster_submitted = True
            new_job_ids = list(self._new_job_ids)

        return _Result()


# ── opt-in OFF (default) ──────────────────────────────────────────────────


def test_opt_in_off_never_resubmits(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment, auto_resume_on_kill=False)
    _write_sidecar(experiment, preempted=[0, 1])
    rec = _Recorder()

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)

    assert outcome.action == "escalate"
    assert "not enabled" in outcome.reason
    assert rec.calls == []
    # Counter untouched.
    assert load_run(experiment, _RUN_ID).auto_resume_count == 0


# ── opt-in ON + preempted + under cap → resume ────────────────────────────


def test_resume_fires_with_exactly_preempted_ids(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    # tasks 0,2 preempted; task 1 OOM (no mark) → only 0,2 resume.
    _write_sidecar(experiment, preempted=[0, 2], other={"1": {"exit_code": 137}})
    rec = _Recorder(new_job_ids=["9100", "9101"])

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)

    assert outcome.action == "resume"
    assert outcome.resubmitted is True
    assert outcome.task_ids == (0, 2)
    assert outcome.new_job_ids == ["9100", "9101"]

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["failed_task_ids"] == [0, 2]
    assert call["category"] == "preempted"
    assert call["from_checkpoint"] is True
    assert call["submit_to_cluster"] is True
    assert call["bypass_preempt_throttle"] is True
    assert call["script"] == ".hpc/templates/cpu_array.sh"
    assert call["backend"] == "slurm"
    assert call["job_name"] == "myjob"
    assert call["job_env"] == {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"}

    # Counter incremented exactly once.
    assert load_run(experiment, _RUN_ID).auto_resume_count == 1
    assert outcome.auto_resume_count == 1


# ── OOM / executor error → escalate, never resubmit ───────────────────────


def test_oom_only_escalates_never_resubmits(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    _write_sidecar(experiment, preempted=[], other={"0": {"exit_code": 137}})
    rec = _Recorder()

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)

    assert outcome.action == "escalate"
    assert "not a resumable kill" in outcome.reason
    assert rec.calls == []
    assert load_run(experiment, _RUN_ID).auto_resume_count == 0


# ── cap reached → escalate ────────────────────────────────────────────────


def test_cap_reached_escalates(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment, max_auto_resumes=2, auto_resume_count=2)
    _write_sidecar(experiment, preempted=[0])
    rec = _Recorder()

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)

    assert outcome.action == "escalate"
    assert "cap reached (2/2)" in outcome.reason
    assert rec.calls == []
    assert load_run(experiment, _RUN_ID).auto_resume_count == 2


# ── missing sidecar → escalate (no marks = not a resumable kill) ──────────


def test_missing_sidecar_escalates(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    # no sidecar written
    rec = _Recorder()

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)

    assert outcome.action == "escalate"
    assert "not a resumable kill" in outcome.reason
    assert rec.calls == []


# ── dedup re-entry: same preempt generation must not burn a cap slot ──────


def test_deduped_replay_does_not_increment_count(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    _write_sidecar(experiment, preempted=[0, 1])
    # resubmit_flow reports a dedup (the new array isn't visible yet, the
    # monitor re-entered FAILED on the same preempt marks).
    rec = _Recorder(deduped=True)

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)

    # Still a "resume" verdict (the run is live from the prior submit), but
    # nothing new fired and the cap counter is untouched.
    assert outcome.action == "resume"
    assert outcome.resubmitted is False
    assert len(rec.calls) == 1
    assert load_run(experiment, _RUN_ID).auto_resume_count == 0


def test_request_id_stable_for_same_marks_distinct_for_new(
    journal_home: Path, experiment: Path
) -> None:
    """Two resumes on the SAME marks share a request_id (→ dedup); a fresh
    preempt generation (new timestamps) mints a distinct id (→ fires)."""
    # High cap so the counter never gates this determinism check.
    _seed_record(experiment, max_auto_resumes=10)
    _write_sidecar(experiment, preempted=[0, 1])
    rec = _Recorder()

    maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)
    rid_first = rec.calls[0]["request_id"]

    # Same marks → same request_id.
    maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)
    assert rec.calls[1]["request_id"] == rid_first

    # New preemption generation (different timestamps) → different id.
    path = run_sidecar_path(experiment, _RUN_ID)
    path.write_text(
        json.dumps(
            {
                "tasks": {
                    "0": {"preempt": {"at": "2026-06-06T13:00:00Z"}},
                    "1": {"preempt": {"at": "2026-06-06T13:00:00Z"}},
                },
                "task_count": 4,
            }
        ),
        encoding="utf-8",
    )
    maybe_auto_resume(experiment, _RUN_ID, resubmit=rec)
    assert rec.calls[2]["request_id"] != rid_first


# ── no journal record → escalate gracefully ───────────────────────────────


def test_no_record_escalates(journal_home: Path, experiment: Path) -> None:
    rec = _Recorder()
    outcome = maybe_auto_resume(experiment, "nonexistent-run", resubmit=rec)
    assert outcome.action == "escalate"
    assert "no journal record" in outcome.reason
    assert rec.calls == []
