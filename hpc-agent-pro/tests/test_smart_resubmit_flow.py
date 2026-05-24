"""Tests for the ``smart-resubmit-flow`` workflow + ``plan-resubmit-overrides`` primitive.

Mechanism check: ``get_meta("smart-resubmit-flow").composes`` resolves
to two entries — ``plan-resubmit-overrides`` (from pro) and
``resubmit-failed`` (from core) — proving the cross-package compose
path works once both pro and core are loaded into the merged registry.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

# -----------------------------------------------------------------------------
# plan-resubmit-overrides — wire-model round-trip
# -----------------------------------------------------------------------------


def test_plan_resubmit_overrides_primitive_wraps_existing_function(tmp_path):
    """The primitive delegates to the existing free function and packs the result."""
    from hpc_agent_pro._schema_models.queries.plan_resubmit_overrides import (
        PlanResubmitOverridesResult,
        PlanResubmitOverridesSpec,
    )
    from hpc_agent_pro.planning import resubmit_planner

    fake_planned = resubmit_planner.PlannedResubmitOverrides(
        overrides={"mem_mb": 18_400, "walltime_sec": 13500},
        rationales={
            "mem_mb": "cold-start +15% buffer",
            "walltime_sec": "cold-start arbitrage",
        },
        cold_start=True,
        daisy_chain_required=False,
    )
    spec = PlanResubmitOverridesSpec(
        profile="ml_ridge",
        cluster="test_cluster",
        base_overrides={"mem_mb": 16_000, "walltime_sec": 14400},
    )
    with patch.object(
        resubmit_planner,
        "plan_resubmit_overrides",
        return_value=fake_planned,
    ) as mock_fn:
        result = resubmit_planner.plan_resubmit_overrides_primitive(tmp_path, spec=spec)

    mock_fn.assert_called_once_with(
        tmp_path,
        profile="ml_ridge",
        cluster="test_cluster",
        base_overrides={"mem_mb": 16_000, "walltime_sec": 14400},
    )
    assert isinstance(result, PlanResubmitOverridesResult)
    assert result.overrides == {"mem_mb": 18_400, "walltime_sec": 13500}
    assert result.rationales["mem_mb"] == "cold-start +15% buffer"
    assert result.cold_start is True
    assert result.daisy_chain_required is False


def test_plan_resubmit_overrides_primitive_serializes_via_model_dump(tmp_path):
    """The result is a Pydantic model so model_dump emits JSON-able output."""
    from hpc_agent_pro._schema_models.queries.plan_resubmit_overrides import (
        PlanResubmitOverridesSpec,
    )
    from hpc_agent_pro.planning import resubmit_planner

    fake_planned = resubmit_planner.PlannedResubmitOverrides(
        overrides={"mem_mb": 16_000},
        rationales={},
        cold_start=False,
        daisy_chain_required=False,
    )
    spec = PlanResubmitOverridesSpec(profile="p", cluster="c", base_overrides={"mem_mb": 16_000})
    with patch.object(
        resubmit_planner,
        "plan_resubmit_overrides",
        return_value=fake_planned,
    ):
        result = resubmit_planner.plan_resubmit_overrides_primitive(tmp_path, spec=spec)
    payload = result.model_dump(mode="json")
    assert payload == {
        "overrides": {"mem_mb": 16_000},
        "rationales": {},
        "cold_start": False,
        "daisy_chain_required": False,
    }


# -----------------------------------------------------------------------------
# smart-resubmit-flow — composes both halves
# -----------------------------------------------------------------------------


def _make_run_record(run_id: str = "r123", profile: str = "ml_ridge", cluster: str = "test"):
    """Build a minimal RunRecord with the fields the workflow reads."""
    from hpc_agent.state.run_record import RunRecord

    return RunRecord(
        run_id=run_id,
        profile=profile,
        cluster=cluster,
        ssh_target="user@host",
        remote_path="/scratch/x",
        job_name="job",
        job_ids=["j1"],
        total_tasks=4,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir="/exp",
    )


def test_smart_resubmit_flow_calls_both_halves(tmp_path):
    """The workflow reads the journal, refines via pro's planner, then resubmits."""
    from hpc_agent_pro import smart_resubmit_flow as srf_mod
    from hpc_agent_pro._schema_models.queries.plan_resubmit_overrides import (
        PlanResubmitOverridesResult,
    )
    from hpc_agent_pro._schema_models.workflows.smart_resubmit_flow import (
        SmartResubmitFlowSpec,
    )

    record = _make_run_record()
    refined = PlanResubmitOverridesResult(
        overrides={"mem_mb": 18_400, "walltime_sec": 13500},
        rationales={"mem_mb": "cold-start +15%"},
        cold_start=True,
        daisy_chain_required=False,
    )
    record_after = _make_run_record()
    record_after.retries = {"7": {"attempts": 1, "category": "system_oom", "overrides": {}}}

    spec = SmartResubmitFlowSpec(
        run_id="r123",
        failed_task_ids=[7, 9],
        category="system_oom",
        base_overrides={"mem_mb": 16_000, "walltime_sec": 14400},
    )

    with (
        patch("hpc_agent.state.journal.load_run", return_value=record) as mock_load,
        patch.object(
            srf_mod, "plan_resubmit_overrides_primitive", return_value=refined
        ) as mock_plan,
        patch(
            "hpc_agent.ops.recover.runner.resubmit_failed",
            return_value=(record_after, False, "rs_abc123"),
        ) as mock_resub,
    ):
        result = srf_mod.smart_resubmit_flow(tmp_path, spec=spec)

    mock_load.assert_called_once_with(tmp_path, "r123")
    # Planner was called with profile/cluster derived from the journal record.
    planner_call_spec = mock_plan.call_args.kwargs["spec"]
    assert planner_call_spec.profile == "ml_ridge"
    assert planner_call_spec.cluster == "test"
    assert planner_call_spec.base_overrides == {"mem_mb": 16_000, "walltime_sec": 14400}

    # resubmit-failed got the *refined* overrides, not the base ones.
    resub_call_spec = mock_resub.call_args.kwargs["spec"]
    assert resub_call_spec.overrides == {"mem_mb": 18_400, "walltime_sec": 13500}
    assert resub_call_spec.failed_task_ids == [7, 9]
    assert resub_call_spec.category == "system_oom"

    assert result.refined.overrides == refined.overrides
    assert result.resubmit == {
        "run_id": "r123",
        "deduped": False,
        "request_id": "rs_abc123",
        "retries": {"7": {"attempts": 1, "category": "system_oom", "overrides": {}}},
        "job_ids": ["j1"],
    }


def test_smart_resubmit_flow_raises_when_no_journal_record(tmp_path):
    """A missing run_id surfaces as SpecInvalid — caller's bug, not internal."""
    from hpc_agent import errors

    from hpc_agent_pro import smart_resubmit_flow as srf_mod
    from hpc_agent_pro._schema_models.workflows.smart_resubmit_flow import (
        SmartResubmitFlowSpec,
    )

    spec = SmartResubmitFlowSpec(
        run_id="missing",
        failed_task_ids=[1],
        category="system_oom",
        base_overrides=None,
    )
    with (
        patch("hpc_agent.state.journal.load_run", return_value=None),
        pytest.raises(errors.SpecInvalid, match="no journal record"),
    ):
        srf_mod.smart_resubmit_flow(tmp_path, spec=spec)


# -----------------------------------------------------------------------------
# Cross-package compose mechanism check
# -----------------------------------------------------------------------------


def test_smart_resubmit_flow_composes_cross_package():
    """``smart-resubmit-flow``'s composes tuple resolves to one pro + one core entry."""
    from hpc_agent._kernel.registry.primitive import get_meta

    meta = get_meta("smart-resubmit-flow")
    assert meta is not None
    names = tuple(c.name for c in meta.composes)
    # Two entries, in declared order.
    assert names == ("plan-resubmit-overrides", "resubmit-failed")
    # Sanity: both are registered as their own primitives in the merged registry.
    assert get_meta("plan-resubmit-overrides") is not None
    assert get_meta("resubmit-failed") is not None


def test_core_registry_visible_in_subprocess():
    """A subprocess can import the core registry and see ``resubmit-failed``.

    This is the smoke-test variant of the "core baseline without pro"
    contract: when pro is installed in the same venv the host's
    entry-point loader pulls it in too, so we can't gate on a 56-vs-68
    count here. The dedicated baseline check ("uninstall pro then
    pytest -q") is run by the parent agent after all P-items land.
    """
    code = (
        "from hpc_agent import register_primitives;"
        " register_primitives();"
        " from hpc_agent._kernel.registry.primitive import get_registry;"
        " r = get_registry();"
        " print('resubmit-failed' in r);"
        " print('plan-resubmit-overrides' in r);"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    # ``resubmit-failed`` is core — must always be there.
    assert lines[0] == "True"
