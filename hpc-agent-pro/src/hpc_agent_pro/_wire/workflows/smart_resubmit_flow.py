"""Wire models for the ``smart-resubmit-flow`` workflow primitive.

Cross-package compose: pro's
:func:`~hpc_agent_pro.planning.resubmit_planner.plan_resubmit_overrides_primitive`
refines the override dict, then the host's ``resubmit-failed`` primitive
mutates state. The flow surfaces both halves in one envelope:
``refined`` carries the planner verdict (overrides + rationales +
cold-start / daisy-chain flags); ``resubmit`` carries the host
resubmitter's bookkeeping (run_id, deduped, request_id, retries, job_ids).
"""

from __future__ import annotations

from typing import Any

from hpc_agent._wire._shared import FailureCategoryResubmittable
from pydantic import BaseModel, ConfigDict, Field

from hpc_agent_pro._wire.queries.plan_resubmit_overrides import (
    PlanResubmitOverridesResult,
)


class SmartResubmitFlowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="smart-resubmit-flow input spec")

    run_id: str = Field(min_length=1, description="The run_id of the existing run to resubmit.")
    failed_task_ids: list[int] = Field(
        min_length=1,
        description="Per-task ids inside the run that need re-attempting.",
    )
    category: FailureCategoryResubmittable = Field(
        description="Failure category that drives retry policy (same set ``resubmit-failed`` accepts).",
    )
    base_overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional caller-supplied overrides (typically ``{mem_mb, walltime_sec}``). "
            "Threaded through pro's planner before being handed to ``resubmit-failed``."
        ),
    )


class SmartResubmitFlowResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="smart-resubmit-flow output data")

    refined: PlanResubmitOverridesResult = Field(
        description="The planner's verdict — refined overrides + rationales + cold-start/daisy flags.",
    )
    resubmit: dict[str, Any] = Field(
        description=(
            "Serialized ``resubmit-failed`` bookkeeping: "
            "``{run_id, deduped, request_id, retries, job_ids}``."
        ),
    )
