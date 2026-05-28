"""Wire models for the ``apply-smart-submit-plan`` workflow primitive.

Code-ifies Step 4c-B of pro's ``submit.md``: takes a base submit
spec and a verbatim ``score-submit-plan`` envelope (the structured
output of pro's ``plan-submit`` primitive) and applies the
auto-pick + auto-apply rules. Returns either a refined spec ready
to hand to the submit primitive, or — when the planner surfaced a
``walltime_split`` recommendation — an escalation entry in
``decisions`` so the caller knows the executor's checkpoint posture
must be confirmed before chaining.

The shape of the score-submit-plan envelope is documented in
:class:`hpc_agent_pro._wire.queries.plan_submit.PlanSubmitResult`.
Field names used here (``recommended_tuple.predicted_eta_sec``,
``array_reshape.recommended_max_array_size``, ``walltime_split``)
match that source of truth.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApplySmartSubmitPlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="apply-smart-submit-plan input spec")

    submit_spec: dict[str, Any] = Field(
        description=(
            "The base submit spec the agent has built so far — a dict matching "
            "``SubmitFlowSpec``. The workflow returns it refined in place; "
            "the original is not mutated."
        ),
    )
    score_submit_plan_envelope: dict[str, Any] = Field(
        description=(
            "The verbatim ``data`` field of a prior ``score-submit-plan`` (a.k.a. "
            "``plan-submit``) envelope. Field names track "
            ":class:`PlanSubmitResult` — see "
            "``hpc_agent_pro._wire.queries.plan_submit``."
        ),
    )


class ApplySmartSubmitPlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="apply-smart-submit-plan output data")

    refined_spec: dict[str, Any] = Field(
        description="The submit spec with auto-applied refinements layered in.",
    )
    decisions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "One entry per planner signal the workflow acted on (or escalated). "
            "Shape: ``{point, outcome, why, ...}``. ``outcome='pending'`` marks "
            "an escalation that the caller must confirm before proceeding."
        ),
    )
