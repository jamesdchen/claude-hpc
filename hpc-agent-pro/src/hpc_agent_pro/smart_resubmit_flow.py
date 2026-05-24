"""``smart-resubmit-flow`` — plugin-composes-core workflow primitive.

Closes the asymmetry between submit-time and resubmit-time survival
logic: today's ``resubmit-failed`` accepts whatever override dict the
caller hands it, while submit-time runs the full survival lattice
(cold-start mem buffer, walltime arbitrage, daisy-chain detection).
``smart-resubmit-flow`` runs the pro planner over the caller's
overrides first, then dispatches to the host's ``resubmit-failed`` so
the resubmit reaches the scheduler with the same survival treatment
the original submit had.

Cross-package compose proof: ``composes=["plan-resubmit-overrides",
"resubmit-failed"]`` resolves the first via the pro registry and the
second via the core registry post-PR-#108's lazy resolution. The
single merged registry is the source of truth for ``name → meta``;
plugin and core primitives compose symmetrically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliShape, SchemaRef

from hpc_agent_pro._schema_models.queries.plan_resubmit_overrides import (
    PlanResubmitOverridesSpec,
)
from hpc_agent_pro._schema_models.workflows.smart_resubmit_flow import (
    SmartResubmitFlowResult,
    SmartResubmitFlowSpec,
)
from hpc_agent_pro.planning.resubmit_planner import plan_resubmit_overrides_primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="smart-resubmit-flow",
    verb="workflow",
    composes=["plan-resubmit-overrides", "resubmit-failed"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (via resubmit-failed)"),
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (per-task retry counters)",
        ),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.JournalCorrupt,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Resubmit a run with planner-aware overrides: pro's "
            "walltime/mem/daisy-chain atoms refine the spec, then the host "
            "resubmits via resubmit-failed."
        ),
        spec_arg=True,
        spec_model=SmartResubmitFlowSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="smart_resubmit_flow"),
    ),
    agent_facing=True,
)
def smart_resubmit_flow(
    experiment_dir: Path,
    *,
    spec: SmartResubmitFlowSpec,
) -> SmartResubmitFlowResult:
    """Refine *spec.base_overrides* via the pro planner, then resubmit.

    Reads the run's journal record to pull ``profile`` / ``cluster`` (the
    planner's bucket keys), runs the survival pass, then hands the
    refined overrides to the host's ``resubmit-failed`` primitive.
    Returns both halves so the caller can surface the planner's
    rationales alongside the resubmit bookkeeping.
    """
    # The planner needs ``profile`` / ``cluster`` to find the right
    # runtime-prior bucket. Both live on the journal record under the
    # run_id, so we read it once up front.
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, spec.run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for run_id={spec.run_id!r}")

    planner_spec = PlanResubmitOverridesSpec(
        profile=record.profile,
        cluster=record.cluster,
        base_overrides=spec.base_overrides,
    )
    refined = plan_resubmit_overrides_primitive(experiment_dir, spec=planner_spec)

    # Hand the refined overrides to the host's resubmit-failed primitive
    # (the one that actually mutates state).
    from hpc_agent._wire.actions.resubmit import ResubmitSpec
    from hpc_agent.ops.recover.runner import resubmit_failed

    resubmit_spec = ResubmitSpec(
        failed_task_ids=list(spec.failed_task_ids),
        category=spec.category,
        overrides=dict(refined.overrides),
    )
    record_after, deduped, request_id = resubmit_failed(
        experiment_dir,
        spec.run_id,
        spec=resubmit_spec,
    )

    return SmartResubmitFlowResult(
        refined=refined,
        resubmit={
            "run_id": record_after.run_id,
            "deduped": deduped,
            "request_id": request_id,
            "retries": dict(record_after.retries or {}),
            "job_ids": list(record_after.job_ids or []),
        },
    )
