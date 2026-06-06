"""Auto-resume composite — turn a ``decide_auto_resume`` verdict into a resubmit.

This is the #294 *Layer-2 auto-fire* remainder (#299): the one place a
read-only monitor path is allowed to put work back on the cluster
*automatically*, with no human and no agent judgement in the loop.

It composes two already-landed pieces:

* :func:`hpc_agent.recovery.auto_resume.decide_auto_resume` — the pure,
  exhaustively-tested safety gate. It returns a ``"resume"`` verdict only
  when all three hard gates pass (opt-in ON, at least one *preempted*
  task, and under the resume cap); otherwise ``"escalate"`` with a reason.
* :func:`hpc_agent.ops.recover_flow.resubmit_flow` — the action. On a
  ``"resume"`` verdict we re-submit exactly the preempted task ids
  ``from_checkpoint`` and bump the run's ``auto_resume_count``.

Safety, restated (the gate enforces it; this composite never relaxes it):

* **Opt-in, default OFF** — a run whose record did not set
  ``auto_resume_on_kill`` escalates immediately.
* **Only on an explicit preemption signal** — "resumable" == the
  dispatcher's per-task ``preempt`` mark. OOM / executor errors carry no
  mark and escalate (resuming an OOM just re-OOMs).
* **Hard cap** — ``auto_resume_count < max_auto_resumes`` is the ultimate
  backstop.

On ``"escalate"`` this is a pure no-op that surfaces the reason — the
caller routes it through the existing escalation-as-data path (#234).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent.ops.recover_flow import resubmit_flow as _resubmit_flow
from hpc_agent.recovery.auto_resume import decide_auto_resume
from hpc_agent.state.journal import load_run, update_run_status
from hpc_agent.state.runs import read_run_sidecar

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord

__all__ = ["AutoResumeOutcome", "maybe_auto_resume"]


@dataclass(frozen=True)
class AutoResumeOutcome:
    """Result of consulting (and possibly firing) the auto-resume composite.

    ``action`` mirrors the gate's verdict (``"resume"`` | ``"escalate"``).
    ``resubmitted`` is True only when a cluster resubmit actually fired —
    so a caller can distinguish "the gate said resume and we did it" from
    every escalate / no-op path. ``reason`` always carries the gate's
    rationale so an escalation can be surfaced verbatim.
    """

    action: str
    reason: str
    task_ids: tuple[int, ...] = ()
    resubmitted: bool = False
    new_job_ids: list[str] = field(default_factory=list)
    auto_resume_count: int = 0


def _safe_read_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    """Read the run sidecar, returning ``{}`` when it is missing/unreadable.

    A missing sidecar carries no ``preempt`` marks, so the gate correctly
    escalates ("not a resumable kill") rather than the composite raising.
    """
    try:
        sc = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return sc if isinstance(sc, dict) else {}


def _resume_request_id(run_id: str, sidecar: dict[str, Any], task_ids: list[int]) -> str:
    """Derive a resubmit request_id keyed on the preempt-mark *generation*.

    The dispatcher overwrites a task's ``preempt`` mark (with a fresh ``at``
    timestamp) each time the scheduler bumps it, so hashing the marks gives an
    id that is:

    * **stable** across an immediate monitor re-entry (the new array jobs are
      not yet visible, the sidecar marks are unchanged) → ``resubmit_flow``
      dedups and does NOT double-submit; and
    * **distinct** for a genuinely NEW preemption (the resumed jobs ran and got
      bumped again, so the marks carry new timestamps) → the next resume fires
      and the cap loop advances.

    Without this — e.g. an id folding in only the monotonically-rising count —
    an immediate re-entry would mint a fresh id and re-submit the same work,
    burning the cap on duplicates.
    """
    tasks = sidecar.get("tasks") if isinstance(sidecar.get("tasks"), dict) else {}
    marks: list[tuple[int, str]] = []
    for tid in task_ids:
        entry = tasks.get(str(tid)) if isinstance(tasks, dict) else None
        preempt = entry.get("preempt") if isinstance(entry, dict) else None
        at = preempt.get("at", "") if isinstance(preempt, dict) else ""
        marks.append((int(tid), str(at)))
    payload = json.dumps(
        {"run_id": run_id, "marks": sorted(marks)},
        sort_keys=True,
    )
    return "auto_resume_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def maybe_auto_resume(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord | None = None,
    resubmit: Callable[..., Any] = _resubmit_flow,
) -> AutoResumeOutcome:
    """Consult the auto-resume gate for *run_id* and fire a resubmit if it says so.

    Reads the run's sidecar (preempt marks) and record (opt-in policy +
    count), calls :func:`decide_auto_resume`, and on a ``"resume"`` verdict
    re-submits exactly the preempted task ids ``from_checkpoint`` via
    *resubmit* (defaults to :func:`resubmit_flow`; injectable for tests),
    then increments the run's ``auto_resume_count``. Every other verdict is
    a no-op that returns the gate's escalation reason.

    *record* may be supplied to avoid a redundant journal read (the monitor
    already holds a fresh record); otherwise it is loaded here.
    """
    if record is None:
        record = load_run(experiment_dir, run_id)
    if record is None:
        return AutoResumeOutcome("escalate", f"no journal record for {run_id!r}")

    sidecar = _safe_read_sidecar(experiment_dir, run_id)
    decision = decide_auto_resume(
        sidecar,
        policy_on=bool(record.auto_resume_on_kill),
        count=int(record.auto_resume_count),
        cap=int(record.max_auto_resumes),
    )

    if decision.action != "resume":
        # Escalate: pure no-op. The caller surfaces ``reason`` through the
        # existing escalation-as-data path (#234) — this composite never
        # parallel-submits around the gate.
        return AutoResumeOutcome(
            "escalate",
            decision.reason,
            task_ids=decision.task_ids,
            auto_resume_count=int(record.auto_resume_count),
        )

    # The gate cleared all three hard gates. Re-submit exactly the preempted
    # ids from their latest checkpoint. ``category="preempted"`` is the honest
    # label; ``bypass_preempt_throttle=True`` opts out of the manual
    # "all-preempted → back off" guard (the cap is the backstop here, #299).
    # The request_id is keyed on the preempt-mark generation so an immediate
    # re-entry dedups (no double submit) while a fresh preemption fires again.
    failed_task_ids = list(decision.task_ids)
    request_id = _resume_request_id(run_id, sidecar, failed_task_ids)
    result = resubmit(
        experiment_dir,
        run_id,
        failed_task_ids=failed_task_ids,
        category="preempted",
        from_checkpoint=True,
        submit_to_cluster=True,
        script=record.script,
        backend=record.backend,
        job_name=record.job_name,
        job_env=dict(record.job_env),
        request_id=request_id,
        bypass_preempt_throttle=True,
    )

    deduped = bool(getattr(result, "deduped", False))
    count = int(record.auto_resume_count)
    if not deduped:
        # A real resubmit fired — bump the counter so the gate's "count < cap"
        # backstop tightens with every attempt. A deduped replay (immediate
        # re-entry on the same preempt generation) put nothing new on the
        # cluster, so it must NOT consume a cap slot. Best-effort: a failed
        # counter write leaves the cap stale-by-one, which is safe (it can only
        # escalate sooner, never later).
        count += 1
        updated = update_run_status(experiment_dir, run_id, auto_resume_count=count)
        count = int(updated.auto_resume_count)

    return AutoResumeOutcome(
        "resume",
        decision.reason,
        task_ids=decision.task_ids,
        resubmitted=not deduped,
        new_job_ids=list(getattr(result, "new_job_ids", []) or []),
        auto_resume_count=count,
    )
