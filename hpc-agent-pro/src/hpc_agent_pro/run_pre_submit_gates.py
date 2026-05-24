"""``run-pre-submit-gates`` — code-ifies Steps 6b/6c/6d of submit.md.

Runs the three pre-submit gates from pro's ``submit.md`` in order:

1. Step 6b — ``check-preflight``: environment readiness (SSH agent,
   transports on PATH, clusters.yaml parses).
2. Step 6c — ``validate-campaign``: campaign spec sanity (executor
   signature, dataset shape, QoS, walltime history, stochastic marker).
3. Step 6d — ``predict-start-time``: queue-wait forecast.

The workflow short-circuits on the first failure: a gate that
returns a failure verdict (``check-preflight``'s ``all_ok=False``,
``validate-campaign``'s ``overall='fail'``) or raises an ``HpcError``
marks that gate as ``failed``, sets ``overall='blocked'``, leaves
subsequent gates with ``status='skipped'`` and an explanatory
envelope, and returns. The caller branches on ``overall`` once.

Gates whose spec input is ``None`` are skipped wholesale (status
``"skipped"``, no envelope).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape, SchemaRef

from hpc_agent_pro._schema_models.workflows.run_pre_submit_gates import (
    RunPreSubmitGatesResult,
    RunPreSubmitGatesSpec,
    _GateOutcome,
)

if TYPE_CHECKING:
    from pathlib import Path


# Names match the registered primitive names so the output keys are
# self-describing (a caller debugging "which gate failed?" sees the
# same name the registry uses).
_GATE_PREFLIGHT = "check-preflight"
_GATE_VALIDATE = "validate-campaign"
_GATE_PREDICT = "predict-start-time"


def _run_preflight(payload: dict[str, Any] | None) -> _GateOutcome:
    """Invoke ``check-preflight`` and map its envelope to an outcome."""
    if payload is None:
        return _GateOutcome(status="skipped", envelope=None)
    from hpc_agent.ops.preflight.check import check_preflight

    try:
        envelope = check_preflight(cluster=payload.get("cluster"))
    except errors.HpcError as exc:
        return _GateOutcome(
            status="failed",
            envelope={"error_code": getattr(exc, "code", "internal"), "message": str(exc)},
        )
    status = "ok" if envelope.get("all_ok") else "failed"
    return _GateOutcome(status=status, envelope=envelope)


def _run_validate_campaign(
    experiment_dir: Path,
    payload: dict[str, Any] | None,
) -> _GateOutcome:
    """Invoke ``validate-campaign`` and map its report to an outcome."""
    if payload is None:
        return _GateOutcome(status="skipped", envelope=None)
    from hpc_agent._wire.workflows.validate_campaign import ValidateCampaignSpec
    from hpc_agent.meta.validate_campaign import validate_campaign

    try:
        spec = ValidateCampaignSpec.model_validate(payload)
    except Exception as exc:
        return _GateOutcome(
            status="failed",
            envelope={
                "error_code": "spec_invalid",
                "message": f"validate-campaign spec is invalid: {exc}",
            },
        )
    try:
        report = validate_campaign(experiment_dir, spec=spec)
    except errors.HpcError as exc:
        return _GateOutcome(
            status="failed",
            envelope={"error_code": getattr(exc, "code", "internal"), "message": str(exc)},
        )
    # ``warn`` proceeds (per validate_campaign's docstring); only
    # ``fail`` blocks the submit.
    status = "failed" if report.overall == "fail" else "ok"
    return _GateOutcome(status=status, envelope=report.model_dump(mode="json"))


def _run_predict_start_time(
    experiment_dir: Path,
    payload: dict[str, Any] | None,
) -> _GateOutcome:
    """Invoke ``predict-start-time`` and map its result to an outcome."""
    if payload is None:
        return _GateOutcome(status="skipped", envelope=None)
    from hpc_agent_pro._schema_models.queries.predict_start_time import (
        PredictStartTimeSpec,
    )
    from hpc_agent_pro.atoms.predict_start_time import predict_start_time_primitive

    try:
        spec = PredictStartTimeSpec.model_validate(payload)
    except Exception as exc:
        return _GateOutcome(
            status="failed",
            envelope={
                "error_code": "spec_invalid",
                "message": f"predict-start-time spec is invalid: {exc}",
            },
        )
    try:
        result = predict_start_time_primitive(experiment_dir, spec=spec)
    except errors.HpcError as exc:
        return _GateOutcome(
            status="failed",
            envelope={"error_code": getattr(exc, "code", "internal"), "message": str(exc)},
        )
    return _GateOutcome(status="ok", envelope=result.model_dump(mode="json"))


@primitive(
    name="run-pre-submit-gates",
    verb="workflow",
    composes=["check-preflight", "validate-campaign", "predict-start-time"],
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Run the three pre-submit gates from submit.md (check-preflight + "
            "validate-campaign + predict-start-time) in sequence with "
            "short-circuit on failure. Returns one unified envelope."
        ),
        spec_arg=True,
        spec_model=RunPreSubmitGatesSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="run_pre_submit_gates"),
    ),
    agent_facing=True,
)
def run_pre_submit_gates(
    experiment_dir: Path,
    *,
    spec: RunPreSubmitGatesSpec,
) -> RunPreSubmitGatesResult:
    """Run the three pre-submit gates with short-circuit on failure.

    Order: preflight → validate-campaign → predict-start-time. A gate
    whose spec input is ``None`` is recorded as ``status='skipped'``.
    The first failure short-circuits subsequent gates to
    ``status='skipped'`` with an explanatory envelope.
    """
    gates: dict[str, _GateOutcome] = {}

    def _short_circuit(reason: str, *remaining: str) -> None:
        for name in remaining:
            gates[name] = _GateOutcome(
                status="skipped",
                envelope={"reason": reason},
            )

    # ── Step 6b — check-preflight ──────────────────────────────────────
    preflight = _run_preflight(spec.preflight)
    gates[_GATE_PREFLIGHT] = preflight
    if preflight.status == "failed":
        _short_circuit(
            "short-circuited by check-preflight failure",
            _GATE_VALIDATE,
            _GATE_PREDICT,
        )
        return RunPreSubmitGatesResult(gates=gates, overall="blocked")

    # ── Step 6c — validate-campaign ────────────────────────────────────
    validate = _run_validate_campaign(experiment_dir, spec.validate_campaign)
    gates[_GATE_VALIDATE] = validate
    if validate.status == "failed":
        _short_circuit(
            "short-circuited by validate-campaign failure",
            _GATE_PREDICT,
        )
        return RunPreSubmitGatesResult(gates=gates, overall="blocked")

    # ── Step 6d — predict-start-time ───────────────────────────────────
    predict = _run_predict_start_time(experiment_dir, spec.predict_start_time)
    gates[_GATE_PREDICT] = predict
    if predict.status == "failed":
        return RunPreSubmitGatesResult(gates=gates, overall="blocked")

    # Overall verdict: ``skipped`` only if every gate was skipped;
    # otherwise ``ok`` (we already returned ``blocked`` on any failure).
    overall = "skipped" if all(g.status == "skipped" for g in gates.values()) else "ok"
    return RunPreSubmitGatesResult(gates=gates, overall=overall)
