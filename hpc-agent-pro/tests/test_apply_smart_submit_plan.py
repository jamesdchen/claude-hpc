"""Tests for ``apply-smart-submit-plan`` — Step 4c-B of submit.md.

Covers all three planner signals:

* ``recommended_tuple.predicted_eta_sec is not None`` → auto-pick.
* ``array_reshape.recommended_max_array_size`` → auto-apply.
* ``walltime_split is not None`` → escalate as a pending decision.

Plus the empty-envelope no-op path.
"""

from __future__ import annotations

import pytest

from hpc_agent_pro._schema_models.workflows.apply_smart_submit_plan import (
    ApplySmartSubmitPlanSpec,
)
from hpc_agent_pro.apply_smart_submit_plan import apply_smart_submit_plan


def _base_spec() -> dict:
    """Plausible base submit spec (kept narrow — body treats it as opaque)."""
    return {
        "profile": "ml_ridge",
        "cluster": "test_cluster",
        "total_tasks": 4,
        "walltime_sec": 14400,
        "mem_mb": 16_000,
        "cpus": 4,
    }


def _candidate(
    constraint: str = "a100",
    *,
    walltime_sec: int = 12000,
    mem_mb: int = 18_000,
    cpus: int = 8,
    predicted_eta_sec: float | None = 120.0,
) -> dict:
    """Build one candidate dict matching the score-submit-plan envelope shape."""
    return {
        "constraint": constraint,
        "recommended_tuple": {
            "constraint": constraint,
            "walltime_sec": walltime_sec,
            "mem_mb": mem_mb,
            "cpus": cpus,
            "predicted_eta_sec": predicted_eta_sec,
            "rationale": f"lattice probe picked {constraint}",
        },
    }


class TestAutoPickTuple:
    def test_applies_tuple_when_predicted_eta_present(self, tmp_path):
        envelope = {
            "candidates": [
                _candidate("a100", walltime_sec=10800, mem_mb=20_000, cpus=8),
            ],
        }
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope=envelope,
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)

        assert result.refined_spec["walltime_sec"] == 10800
        assert result.refined_spec["mem_mb"] == 20_000
        assert result.refined_spec["cpus"] == 8
        assert result.refined_spec["constraint"] == "a100"

        # The decisions list captures the auto-apply with a rationale.
        applies = [d for d in result.decisions if d["point"] == "recommended_tuple"]
        assert len(applies) == 1
        assert applies[0]["outcome"] == "auto_applied"
        assert applies[0]["applied"]["walltime_sec"] == 10800

    def test_no_auto_pick_when_predicted_eta_is_none(self, tmp_path):
        envelope = {
            "candidates": [_candidate("a100", predicted_eta_sec=None)],
        }
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope=envelope,
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)

        # Tuple values pass through unchanged (no auto-pick fired).
        assert result.refined_spec["walltime_sec"] == 14400
        assert result.refined_spec["mem_mb"] == 16_000
        assert all(d["point"] != "recommended_tuple" for d in result.decisions)

    def test_picks_first_candidate_with_eta(self, tmp_path):
        """Multiple candidates qualify → take the first (deterministic auto-pick)."""
        envelope = {
            "candidates": [
                _candidate("v100", walltime_sec=9000, mem_mb=14_000, predicted_eta_sec=None),
                _candidate("a100", walltime_sec=10800, mem_mb=20_000, predicted_eta_sec=120.0),
                _candidate("h100", walltime_sec=8400, mem_mb=24_000, predicted_eta_sec=60.0),
            ],
        }
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope=envelope,
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)
        # First candidate with predicted_eta_sec is not None is "a100".
        assert result.refined_spec["constraint"] == "a100"
        assert result.refined_spec["walltime_sec"] == 10800


class TestAutoApplyArrayReshape:
    def test_applies_when_recommended_max_array_size_present(self, tmp_path):
        envelope = {
            "candidates": [],
            "array_reshape": {
                "current_max_array_size": 1000,
                "recommended_max_array_size": 500,
                "rationale": "halve to fit the 30min backfill window",
            },
        }
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope=envelope,
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)
        assert result.refined_spec["max_array_size"] == 500

        reshape_decisions = [d for d in result.decisions if d["point"] == "array_reshape"]
        assert len(reshape_decisions) == 1
        assert reshape_decisions[0]["outcome"] == "auto_applied"
        assert reshape_decisions[0]["from"] == 1000
        assert reshape_decisions[0]["to"] == 500

    def test_skips_when_recommended_max_array_size_is_none(self, tmp_path):
        envelope = {
            "candidates": [],
            "array_reshape": {
                "current_max_array_size": 1000,
                "recommended_max_array_size": None,
                "rationale": "already sized correctly",
            },
        }
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope=envelope,
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)
        assert "max_array_size" not in result.refined_spec
        assert all(d["point"] != "array_reshape" for d in result.decisions)


class TestWalltimeSplitEscalation:
    def test_surfaces_as_pending_decision(self, tmp_path):
        envelope = {
            "candidates": [],
            "walltime_split": {
                "n_segments": 3,
                "segment_walltime_sec": 1800,
                "total_walltime_sec": 5400,
                "requires_checkpointing": True,
                "rationale": "3x 30min segments fit backfill",
            },
        }
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope=envelope,
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)

        # NOT auto-applied — the spec's walltime_sec is untouched.
        assert result.refined_spec["walltime_sec"] == 14400

        confirm_decisions = [
            d for d in result.decisions if d["point"] == "walltime_split_confirm"
        ]
        assert len(confirm_decisions) == 1
        d = confirm_decisions[0]
        assert d["outcome"] == "pending"
        assert d["split_recommendation"]["n_segments"] == 3
        assert d["split_recommendation"]["requires_checkpointing"] is True


class TestNoOp:
    def test_empty_envelope_returns_spec_unchanged(self, tmp_path):
        base = _base_spec()
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=base,
            score_submit_plan_envelope={"candidates": []},
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)
        assert result.refined_spec == base
        assert result.decisions == []

    def test_envelope_with_none_signals_only_returns_no_decisions(self, tmp_path):
        envelope = {
            "candidates": [_candidate(predicted_eta_sec=None)],
            "array_reshape": None,
            "walltime_split": None,
        }
        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope=envelope,
        )
        result = apply_smart_submit_plan(tmp_path, spec=spec)
        assert result.decisions == []


class TestComposes:
    def test_composes_resolves_to_score_submit_plan(self):
        """``apply-smart-submit-plan.composes`` carries the workflow-graph edge."""
        from hpc_agent._kernel.registry.primitive import get_meta

        meta = get_meta("apply-smart-submit-plan")
        assert meta is not None
        assert tuple(c.name for c in meta.composes) == ("score-submit-plan",)


class TestSpecValidation:
    def test_candidates_not_list_raises(self, tmp_path):
        from hpc_agent import errors

        spec = ApplySmartSubmitPlanSpec(
            submit_spec=_base_spec(),
            score_submit_plan_envelope={"candidates": "not-a-list"},
        )
        with pytest.raises(errors.SpecInvalid, match="must be a list"):
            apply_smart_submit_plan(tmp_path, spec=spec)
