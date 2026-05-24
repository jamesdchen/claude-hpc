"""Tests for ``run-pre-submit-gates`` — Steps 6b/6c/6d of submit.md."""

from __future__ import annotations

from unittest.mock import patch

from hpc_agent_pro._schema_models.workflows.run_pre_submit_gates import (
    RunPreSubmitGatesSpec,
)
from hpc_agent_pro.run_pre_submit_gates import run_pre_submit_gates

# -----------------------------------------------------------------------------
# Helpers — fake envelopes for each composed primitive.
# -----------------------------------------------------------------------------


def _ok_preflight() -> dict:
    return {
        "all_ok": True,
        "checks": [
            {"name": "ssh_auth_sock", "ok": True, "detail": "agent at /tmp/sock"},
            {"name": "ssh_on_path", "ok": True, "detail": "/usr/bin/ssh"},
        ],
    }


def _failing_preflight() -> dict:
    return {
        "all_ok": False,
        "checks": [
            {"name": "ssh_auth_sock", "ok": False, "detail": "SSH_AUTH_SOCK not set"},
        ],
    }


def _make_validate_report(overall: str = "pass"):
    """Build a ValidateCampaignReport-like model_dump output."""
    from hpc_agent._wire.workflows.validate_campaign import ValidateCampaignReport

    return ValidateCampaignReport(overall=overall, findings=[], validators_run=[])


def _make_predict_result():
    """Build a PredictStartTimeResult-like model_dump output."""
    from hpc_agent_pro._schema_models.queries.predict_start_time import (
        PredictStartTimeResult,
    )

    return PredictStartTimeResult(
        best_submit_offset_hours=0.0,
        best_predicted_start_iso="2026-05-25T00:00:00Z",
        best_total_time_sec=120,
        candidates=[],
    )


def _valid_validate_payload() -> dict:
    """Minimum acceptable validate-campaign spec."""
    return {"profile": "ml_ridge", "cluster": "test_cluster"}


def _valid_predict_payload() -> dict:
    """Minimum acceptable predict-start-time spec."""
    return {
        "now_iso": "2026-05-24T00:00:00Z",
        "squeue_text": "",
        "partition": "default",
        "partition_slot_count": 1,
        "your_priority": 0,
        "your_walltime_sec": 1,
    }


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_all_three_gates_pass_returns_overall_ok(tmp_path):
    spec = RunPreSubmitGatesSpec(
        preflight={"cluster": "test_cluster"},
        validate_campaign=_valid_validate_payload(),
        predict_start_time=_valid_predict_payload(),
    )
    with (
        patch(
            "hpc_agent.ops.preflight.check.check_preflight",
            return_value=_ok_preflight(),
        ),
        patch(
            "hpc_agent.meta.validate_campaign.validate_campaign",
            return_value=_make_validate_report("pass"),
        ),
        patch(
            "hpc_agent_pro.atoms.predict_start_time.predict_start_time_primitive",
            return_value=_make_predict_result(),
        ),
    ):
        result = run_pre_submit_gates(tmp_path, spec=spec)

    assert result.overall == "ok"
    assert result.gates["check-preflight"].status == "ok"
    assert result.gates["validate-campaign"].status == "ok"
    assert result.gates["predict-start-time"].status == "ok"


def test_skipped_gate_when_spec_input_is_none(tmp_path):
    """A None entry means skip that gate; overall stays ok when nothing failed."""
    spec = RunPreSubmitGatesSpec(
        preflight={"cluster": "test_cluster"},
        validate_campaign=None,
        predict_start_time=_valid_predict_payload(),
    )
    with (
        patch(
            "hpc_agent.ops.preflight.check.check_preflight",
            return_value=_ok_preflight(),
        ),
        patch(
            "hpc_agent_pro.atoms.predict_start_time.predict_start_time_primitive",
            return_value=_make_predict_result(),
        ),
    ):
        result = run_pre_submit_gates(tmp_path, spec=spec)

    assert result.overall == "ok"
    assert result.gates["check-preflight"].status == "ok"
    assert result.gates["validate-campaign"].status == "skipped"
    assert result.gates["validate-campaign"].envelope is None
    assert result.gates["predict-start-time"].status == "ok"


def test_all_gates_skipped_returns_overall_skipped(tmp_path):
    spec = RunPreSubmitGatesSpec(
        preflight=None,
        validate_campaign=None,
        predict_start_time=None,
    )
    result = run_pre_submit_gates(tmp_path, spec=spec)
    assert result.overall == "skipped"
    assert all(g.status == "skipped" for g in result.gates.values())


def test_preflight_failure_short_circuits_remaining_gates(tmp_path):
    spec = RunPreSubmitGatesSpec(
        preflight={"cluster": "test_cluster"},
        validate_campaign=_valid_validate_payload(),
        predict_start_time=_valid_predict_payload(),
    )
    with patch(
        "hpc_agent.ops.preflight.check.check_preflight",
        return_value=_failing_preflight(),
    ):
        result = run_pre_submit_gates(tmp_path, spec=spec)

    assert result.overall == "blocked"
    assert result.gates["check-preflight"].status == "failed"
    assert result.gates["validate-campaign"].status == "skipped"
    assert result.gates["predict-start-time"].status == "skipped"
    # The short-circuit reason is recorded so callers can debug.
    assert "check-preflight" in result.gates["validate-campaign"].envelope["reason"]


def test_validate_campaign_fail_blocks_predict_start_time(tmp_path):
    spec = RunPreSubmitGatesSpec(
        preflight={"cluster": "test_cluster"},
        validate_campaign=_valid_validate_payload(),
        predict_start_time=_valid_predict_payload(),
    )
    with (
        patch(
            "hpc_agent.ops.preflight.check.check_preflight",
            return_value=_ok_preflight(),
        ),
        patch(
            "hpc_agent.meta.validate_campaign.validate_campaign",
            return_value=_make_validate_report("fail"),
        ),
    ):
        result = run_pre_submit_gates(tmp_path, spec=spec)

    assert result.overall == "blocked"
    assert result.gates["check-preflight"].status == "ok"
    assert result.gates["validate-campaign"].status == "failed"
    assert result.gates["predict-start-time"].status == "skipped"


def test_validate_campaign_warn_does_not_block(tmp_path):
    """``overall='warn'`` is a soft signal — proceed (per validate-campaign's contract)."""
    spec = RunPreSubmitGatesSpec(
        preflight=None,
        validate_campaign=_valid_validate_payload(),
        predict_start_time=None,
    )
    with patch(
        "hpc_agent.meta.validate_campaign.validate_campaign",
        return_value=_make_validate_report("warn"),
    ):
        result = run_pre_submit_gates(tmp_path, spec=spec)
    assert result.overall == "ok"
    assert result.gates["validate-campaign"].status == "ok"


def test_predict_start_time_failure_marks_overall_blocked(tmp_path):
    """A predict-start-time exception blocks the overall — uniform gate semantics."""
    from hpc_agent import errors

    spec = RunPreSubmitGatesSpec(
        preflight=None,
        validate_campaign=None,
        predict_start_time=_valid_predict_payload(),
    )
    with patch(
        "hpc_agent_pro.atoms.predict_start_time.predict_start_time_primitive",
        side_effect=errors.SpecInvalid("bad input"),
    ):
        result = run_pre_submit_gates(tmp_path, spec=spec)
    assert result.overall == "blocked"
    assert result.gates["predict-start-time"].status == "failed"


def test_preflight_hpc_error_is_recorded_as_failed(tmp_path):
    """A raise from check-preflight is captured rather than propagated."""
    from hpc_agent import errors

    spec = RunPreSubmitGatesSpec(
        preflight={"cluster": "x"},
        validate_campaign=None,
        predict_start_time=None,
    )
    with patch(
        "hpc_agent.ops.preflight.check.check_preflight",
        side_effect=errors.ClusterUnknown("x not in clusters.yaml"),
    ):
        result = run_pre_submit_gates(tmp_path, spec=spec)
    assert result.overall == "blocked"
    assert result.gates["check-preflight"].status == "failed"
    assert "x not in clusters.yaml" in result.gates["check-preflight"].envelope["message"]


def test_composes_resolves_to_all_three_gates():
    """``run-pre-submit-gates.composes`` carries all three workflow-graph edges."""
    from hpc_agent._kernel.registry.primitive import get_meta

    meta = get_meta("run-pre-submit-gates")
    assert meta is not None
    assert tuple(c.name for c in meta.composes) == (
        "check-preflight",
        "validate-campaign",
        "predict-start-time",
    )


def test_validate_campaign_invalid_spec_recorded_as_failed(tmp_path):
    """A spec that doesn't satisfy ValidateCampaignSpec is captured (not crashed)."""
    spec = RunPreSubmitGatesSpec(
        preflight=None,
        validate_campaign={"profile": ""},  # missing required ``cluster`` + min_length
        predict_start_time=None,
    )
    result = run_pre_submit_gates(tmp_path, spec=spec)
    assert result.overall == "blocked"
    assert result.gates["validate-campaign"].status == "failed"


def test_predict_start_time_invalid_spec_recorded_as_failed(tmp_path):
    spec = RunPreSubmitGatesSpec(
        preflight=None,
        validate_campaign=None,
        predict_start_time={"now_iso": ""},  # missing required fields
    )
    result = run_pre_submit_gates(tmp_path, spec=spec)
    assert result.overall == "blocked"
    assert result.gates["predict-start-time"].status == "failed"
