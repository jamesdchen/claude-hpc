"""Wire models for the ``run-pre-submit-gates`` workflow primitive.

Code-ifies Steps 6b / 6c / 6d of pro's ``submit.md``: run
``check-preflight`` (environment readiness), ``validate-campaign``
(campaign spec sanity), and ``predict-start-time`` (queue-wait
forecast) in sequence with short-circuit on any gate failure.

Each gate's input arrives as a dict so the wire shape stays flat;
inner shapes are documented by the host's / pro's existing specs
for the composed primitives. A ``None`` value for any gate means
"skip this gate" — useful when (e.g.) the caller has already run
``check-preflight`` recently and only needs the campaign-validation
+ start-time forecast halves.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RunPreSubmitGatesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="run-pre-submit-gates input spec")

    preflight: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inputs for ``check-preflight``. Shape: ``{cluster: str | None}``. "
            "``None`` means skip the preflight gate."
        ),
    )
    validate_campaign: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inputs for ``validate-campaign`` (any field of "
            "``ValidateCampaignSpec``; profile + cluster required). "
            "``None`` means skip the campaign-validation gate."
        ),
    )
    predict_start_time: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inputs for ``predict-start-time`` (any field of "
            "``PredictStartTimeSpec``). ``None`` means skip the "
            "start-time forecast."
        ),
    )


class _GateOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "skipped", "failed"]
    envelope: dict[str, Any] | None = Field(
        default=None,
        description="The gate's structured output (when status==ok) or failure detail (when status==failed).",
    )


class RunPreSubmitGatesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="run-pre-submit-gates output data")

    gates: dict[str, _GateOutcome] = Field(
        description=(
            "Per-gate outcome keyed by gate name "
            "(``check-preflight``, ``validate-campaign``, ``predict-start-time``)."
        ),
    )
    overall: Literal["ok", "blocked", "skipped"] = Field(
        description=(
            "``ok`` when every non-skipped gate passed; ``blocked`` when "
            "any gate failed (and the rest short-circuited); ``skipped`` "
            "when every gate was skipped."
        ),
    )
