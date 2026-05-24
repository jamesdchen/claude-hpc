"""Wire models for the ``plan-resubmit-overrides`` primitive.

Promotes the existing
:func:`hpc_agent_pro.planning.resubmit_planner.plan_resubmit_overrides`
free function to a registered pro primitive: the spec mirrors the
function's keyword arguments, the result mirrors
:class:`~hpc_agent_pro.planning.resubmit_planner.PlannedResubmitOverrides`.

Pulled in by ``smart-resubmit-flow`` (the cross-package compose proof:
``composes=["plan-resubmit-overrides", "resubmit-failed"]`` resolves
the first via this pro module and the second via the core registry
post-PR-#108's lazy resolution).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanResubmitOverridesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="plan-resubmit-overrides input spec")

    profile: str = Field(min_length=1, description="Runtime-prior bucket key (profile).")
    cluster: str = Field(min_length=1, description="Runtime-prior bucket key (cluster).")
    base_overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The caller's requested overrides (typically ``{mem_mb, walltime_sec}``). "
            "``None`` is treated as an empty dict: the planner still emits a "
            "cold-start verdict but has nothing to adjust."
        ),
    )


class PlanResubmitOverridesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="plan-resubmit-overrides output data")

    overrides: dict[str, Any] = Field(
        description=(
            "The adjusted override dict to hand to ``resubmit-failed``. "
            "Keys in ``base_overrides`` the planner did not touch pass through unchanged."
        ),
    )
    rationales: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "One short-string explanation per knob the planner touched. "
            "Suitable for surfacing in the resubmit response envelope."
        ),
    )
    cold_start: bool = Field(
        default=False,
        description=(
            "True when the (profile, cluster) pair has fewer than "
            "``MIN_PRIOR_SAMPLES`` successful samples — the survival "
            "atoms only fire on cold-start."
        ),
    )
    daisy_chain_required: bool = Field(
        default=False,
        description=(
            "Advisory flag: the post-arbitrage walltime ask exceeds the "
            "cluster's hard scheduler ceiling, so segmented submission is required."
        ),
    )
