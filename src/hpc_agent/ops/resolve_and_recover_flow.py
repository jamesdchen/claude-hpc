"""Resolve-and-recover composite — the #240 flow-wiring of the #234 resolver.

The deterministic resolver (:func:`hpc_agent.ops.recover.resolve.resolve`) and
the escalation funnel primitives are already built and tested; #234 is the
*resolution logic*. This module is its **buildable wiring** (#240): the one
auto-fire composite that turns a per-cluster :class:`Resolution` into either an
automatic resubmit (``decided_by="code"``) or a parked escalation
(``decided_by="judgement"``), modelled closely on the blessed
:mod:`hpc_agent.ops.auto_resume_flow` template.

The structural parallel to ``auto_resume_flow`` is deliberate — same layer,
same shape:

* a ``fetch_failures`` query (injected for testability),
* a pure decide gate — **but the general** :func:`resolve` keyed on the widened
  ``(error_class, temporal_context, resource_spec)`` evidence vector, in place
  of auto-resume's preempted-only ``decide_auto_resume_from_ids``,
* the :func:`hpc_agent.ops.recover_flow.resubmit_flow` action,
* escalate = a no-op surfaced as escalation-as-data (#231/#234), plus a *park*
  side-effect (``mark_pending_verdict``) so the held run drops out of the
  campaign loop's not-done set while everything else keeps progressing.

Policy (the #240 architectural decisions, implemented verbatim):

* ``decided_by="code"`` → auto-resubmit via ``resubmit_flow`` with the
  resolver's refined overrides (``Resolution.action``), bumping the run's
  ``auto_recover_count`` against ``max_auto_recovers``.
* ``decided_by="judgement"`` → ``mark_pending_verdict`` (park) and surface the
  :class:`Escalation`. NEVER blocks: one parked cluster does not stop the loop
  from resubmitting another cluster — or another run.

Safety (mirrors auto-resume's idiom exactly):

* **Opt-in, default OFF** — a run whose record did not set
  ``auto_recover_on_failure`` computes its verdict but takes **no** side effect
  (no resubmit, no park). The verdict is still surfaced as data (#283: no
  agent-facing field bypasses a safety step).
* **Hard-capped** — ``auto_recover_count < max_auto_recovers`` is the backstop;
  a code verdict over the cap parks instead of resubmitting.

``preempted`` clusters are SKIPPED: they keep routing through the existing
``auto_resume_flow`` path (and the resolver's ``_DETERMINISTIC`` set
deliberately excludes ``preempted``), so this composite never double-handles
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.ops.recover.failures_atom import fetch_failures as _fetch_failures
from hpc_agent.ops.recover.features_glue import (
    build_escalation_cluster,
    build_failure_features,
)
from hpc_agent.ops.recover.resolve import resolve as _resolve
from hpc_agent.ops.recover_flow import resubmit_flow as _resubmit_flow
from hpc_agent.state.journal import load_run, mark_pending_verdict, update_run_status

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hpc_agent._wire.fixtures.escalation import Escalation
    from hpc_agent.state.run_record import RunRecord

__all__ = [
    "ClusterOutcome",
    "ResolveAndRecoverOutcome",
    "maybe_resolve_and_recover",
]


@dataclass(frozen=True)
class ClusterOutcome:
    """The disposition of one failure cluster after resolve-and-recover.

    ``disposition`` is one of:

    * ``"resubmitted"`` — a ``decided_by="code"`` verdict auto-acted (a
      ``resubmit_flow`` fired with the refined overrides).
    * ``"held"`` — a ``decided_by="judgement"`` verdict (or a code verdict the
      cap/opt-out blocked from acting) was parked via ``mark_pending_verdict``.
    * ``"verdict_only"`` — opt-in OFF: the verdict was computed and surfaced but
      no side effect was taken (no resubmit, no park).
    * ``"skipped"`` — a ``preempted`` cluster left to the auto-resume path.

    ``decided_by`` mirrors the resolver verdict (``"code"`` | ``"judgement"``)
    for clusters that were resolved; ``None`` for a skipped cluster.
    ``escalation`` carries the :class:`Escalation` block for a held /
    verdict-only judgement cluster so a caller can surface it verbatim.
    """

    fingerprint: str | None
    error_class: str | None
    task_ids: tuple[Any, ...]
    disposition: str
    decided_by: str | None = None
    reason: str = ""
    overrides: dict[str, Any] | None = None
    escalation: Escalation | None = None
    new_job_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolveAndRecoverOutcome:
    """Result of consulting (and possibly firing) the resolve-and-recover composite.

    Mirrors :class:`hpc_agent.ops.auto_resume_flow.AutoResumeOutcome`'s role: a
    structured, side-effect-free-to-read summary the monitor / campaign loop
    routes on. ``clusters`` lists every fetched cluster's disposition;
    ``resubmitted`` / ``held`` / ``skipped`` are convenience projections.
    ``auto_recover_count`` is the run's post-call counter.
    """

    run_id: str
    clusters: tuple[ClusterOutcome, ...] = ()
    reason: str = ""
    auto_recover_count: int = 0

    @property
    def resubmitted(self) -> tuple[ClusterOutcome, ...]:
        return tuple(c for c in self.clusters if c.disposition == "resubmitted")

    @property
    def held(self) -> tuple[ClusterOutcome, ...]:
        return tuple(c for c in self.clusters if c.disposition == "held")

    @property
    def skipped(self) -> tuple[ClusterOutcome, ...]:
        return tuple(c for c in self.clusters if c.disposition == "skipped")


def _fetch_clusters(
    experiment_dir: Path,
    run_id: str,
    failures_fetcher: Callable[..., dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return ``(clusters, reason)`` from the cluster failure report.

    ``(None, reason)`` when the report could not be fetched (SSH / cluster
    error) so the composite surfaces the reason rather than crashing the monitor
    loop — the same graceful-escalate posture as ``auto_resume_flow``.
    """
    try:
        report = failures_fetcher(experiment_dir=experiment_dir, run_id=run_id)
    except (errors.HpcError, OSError, TimeoutError) as exc:
        return None, f"could not fetch cluster failures for auto-recover: {exc}"
    clusters = report.get("clusters") if isinstance(report, dict) else None
    if not isinstance(clusters, list):
        return [], ""
    return clusters, ""


def _read_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Best-effort read of the run sidecar for ``resource_spec`` sourcing.

    A missing / unreadable sidecar is not fatal — the features glue simply
    yields an empty ``resource_spec`` (the resolver then falls back to its
    context-free catalog fix), so we return ``None`` rather than raising.
    """
    try:
        from hpc_agent.state.runs import read_run_sidecar
    except ImportError:  # pragma: no cover - import guard mirrors failures_atom
        return None
    try:
        return read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, ValueError):
        return None


def maybe_resolve_and_recover(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord | None = None,
    max_code_attempts: int = 1,
    resubmit: Callable[..., Any] = _resubmit_flow,
    failures_fetcher: Callable[..., dict[str, Any]] = _fetch_failures,
) -> ResolveAndRecoverOutcome:
    """Resolve every failure cluster for *run_id* and auto-act per the #240 policy.

    Fetches the cluster-authoritative failure report, then for each cluster
    (skipping ``preempted`` — the auto-resume path owns those) builds the #230
    evidence vector, calls :func:`resolve`, and routes the verdict:

    * ``code`` + opt-in ON + under cap → ``resubmit_flow`` with the refined
      overrides, bumping ``auto_recover_count``.
    * ``code`` + over cap → park (the cap is the backstop; we do not loop a fix).
    * ``judgement`` + opt-in ON → ``mark_pending_verdict`` (park) + surface the
      escalation. One parked cluster never blocks resubmit of another.
    * opt-in OFF → compute + surface the verdict, take no side effect (#283).

    *resubmit* and *failures_fetcher* are injection seams for tests, exactly as
    in :func:`hpc_agent.ops.auto_resume_flow.maybe_auto_resume`.
    """
    if record is None:
        record = load_run(experiment_dir, run_id)
    if record is None:
        return ResolveAndRecoverOutcome(run_id, reason=f"no journal record for {run_id!r}")

    clusters, fetch_reason = _fetch_clusters(experiment_dir, run_id, failures_fetcher)
    if clusters is None:
        return ResolveAndRecoverOutcome(
            run_id,
            reason=fetch_reason,
            auto_recover_count=int(record.auto_recover_count),
        )

    opt_in = bool(record.auto_recover_on_failure)
    sidecar = _read_sidecar(experiment_dir, run_id) if clusters else None

    outcomes: list[ClusterOutcome] = []
    count = int(record.auto_recover_count)
    cap = int(record.max_auto_recovers)

    for cluster in clusters:
        error_class = cluster.get("error_class")
        task_ids = tuple(cluster.get("task_ids") or [])
        fingerprint = cluster.get("fingerprint")

        # Preempted clusters keep routing through the existing auto-resume path;
        # the resolver's _DETERMINISTIC set excludes ``preempted`` for the same
        # reason. Never double-handle.
        if error_class == "preempted":
            outcomes.append(
                ClusterOutcome(
                    fingerprint=fingerprint,
                    error_class=error_class,
                    task_ids=task_ids,
                    disposition="skipped",
                    reason="preempted: handled by the auto-resume path",
                )
            )
            continue

        features = build_failure_features(cluster, record=record, sidecar=sidecar)
        esc_cluster = build_escalation_cluster(cluster, run_id=run_id)
        resolution = _resolve(features, cluster=esc_cluster, max_code_attempts=max_code_attempts)

        if resolution.decided_by == "code":
            outcome, count = _act_on_code(
                experiment_dir,
                run_id,
                record=record,
                cluster=cluster,
                resolution_action=resolution.action or {},
                error_class=error_class,
                task_ids=task_ids,
                fingerprint=fingerprint,
                opt_in=opt_in,
                count=count,
                cap=cap,
                resubmit=resubmit,
            )
            outcomes.append(outcome)
            continue

        # judgement verdict — park (or, opt-out, surface only).
        outcomes.append(
            _act_on_judgement(
                experiment_dir,
                run_id,
                escalation=resolution.escalation,
                reason=resolution.reason,
                error_class=error_class,
                task_ids=task_ids,
                fingerprint=fingerprint,
                opt_in=opt_in,
            )
        )

    return ResolveAndRecoverOutcome(
        run_id,
        clusters=tuple(outcomes),
        auto_recover_count=count,
    )


def _act_on_code(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord,
    cluster: dict[str, Any],
    resolution_action: dict[str, Any],
    error_class: str | None,
    task_ids: tuple[Any, ...],
    fingerprint: str | None,
    opt_in: bool,
    count: int,
    cap: int,
    resubmit: Callable[..., Any],
) -> tuple[ClusterOutcome, int]:
    """Route a ``decided_by="code"`` verdict. Returns ``(outcome, new_count)``.

    Opt-in OFF → ``verdict_only`` (surface the refined overrides, no side
    effect). Over cap → ``held`` (park rather than loop a fix). Otherwise →
    ``resubmit_flow`` with the refined overrides, bumping the counter on a real
    (non-deduped) submit.
    """
    # The resolver's action is a ``suggested_fix``-shaped dict
    # ``{"action": <verb>, **params}``; the resubmit ``overrides`` is the params
    # (the verb is carried separately as the rationale). render_overrides_to_extra_flags
    # consumes documented knob keys and silently drops the rest, so passing the
    # action verb through is harmless, but we keep overrides params-only to match
    # the runner's retry-record convention.
    overrides = {k: v for k, v in resolution_action.items() if k != "action"}

    if not opt_in:
        # #283: opt-out still surfaces the verdict-as-data; no resubmit.
        return (
            ClusterOutcome(
                fingerprint=fingerprint,
                error_class=error_class,
                task_ids=task_ids,
                disposition="verdict_only",
                decided_by="code",
                reason="auto_recover_on_failure not enabled",
                overrides=overrides,
            ),
            count,
        )

    if count >= cap:
        # Cap is the backstop — park rather than loop a deterministic fix that
        # the cap says has run its budget.
        return (
            ClusterOutcome(
                fingerprint=fingerprint,
                error_class=error_class,
                task_ids=task_ids,
                disposition="held",
                decided_by="code",
                reason=f"auto-recover cap reached ({count}/{cap})",
                overrides=overrides,
            ),
            count,
        )

    failed_task_ids = [int(t) for t in (cluster.get("task_ids") or [])]
    # request_id folds in the current count so each cap-loop attempt is a
    # distinct request (mirrors auto_resume_flow): two genuine recoveries of the
    # same set must not dedup against each other.
    request_id = f"auto_recover_{run_id}_{count}"
    result = resubmit(
        experiment_dir,
        run_id,
        failed_task_ids=failed_task_ids,
        category=error_class,
        overrides=overrides,
        from_checkpoint=True,
        submit_to_cluster=True,
        script=record.script,
        backend=record.backend,
        job_name=record.job_name,
        job_env=dict(record.job_env),
        request_id=request_id,
    )

    deduped = bool(getattr(result, "deduped", False))
    new_count = count
    if not deduped:
        new_count += 1
        updated = update_run_status(experiment_dir, run_id, auto_recover_count=new_count)
        new_count = int(updated.auto_recover_count)

    return (
        ClusterOutcome(
            fingerprint=fingerprint,
            error_class=error_class,
            task_ids=task_ids,
            disposition="resubmitted",
            decided_by="code",
            reason=f"{error_class}: auto-recovered with refined overrides",
            overrides=overrides,
            new_job_ids=list(getattr(result, "new_job_ids", []) or []),
        ),
        new_count,
    )


def _act_on_judgement(
    experiment_dir: Path,
    run_id: str,
    *,
    escalation: Escalation | None,
    reason: str,
    error_class: str | None,
    task_ids: tuple[Any, ...],
    fingerprint: str | None,
    opt_in: bool,
) -> ClusterOutcome:
    """Route a ``decided_by="judgement"`` verdict.

    Opt-in ON → park via ``mark_pending_verdict`` (the run drops out of the
    campaign loop's not-done set, never blocking other work) and surface the
    escalation. Opt-in OFF → surface the escalation only, no park (#283: the
    decision-as-data is still computed).
    """
    if not opt_in:
        return ClusterOutcome(
            fingerprint=fingerprint,
            error_class=error_class,
            task_ids=task_ids,
            disposition="verdict_only",
            decided_by="judgement",
            reason=reason,
            escalation=escalation,
        )

    if escalation is not None:
        # The state layer is pure I/O — hand it the dumped dict, never the model.
        mark_pending_verdict(experiment_dir, run_id, escalation=escalation.model_dump())

    return ClusterOutcome(
        fingerprint=fingerprint,
        error_class=error_class,
        task_ids=task_ids,
        disposition="held",
        decided_by="judgement",
        reason=reason,
        escalation=escalation,
    )
