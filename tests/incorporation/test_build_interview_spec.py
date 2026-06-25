"""Tests for the ``build-interview-spec`` primitive (Surface 2).

Pins the REQUIRED_CALLER_FIELDS contract and the explicit tasks_py_mode:

* ``goal`` is required (min_length=1) → schema rejection on absence;
* ``task_generator`` is required BY MODE — ``generator`` needs it,
  ``validate`` forbids it — never inferred from absence (the trap that
  would break the sanctioned hand-written-tasks.py path);
* the assembled spec round-trips through InterviewSpec and is persisted to
  .hpc/interview_spec.json.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._wire.actions.build_interview_spec import BuildInterviewSpecInput
from hpc_agent._wire.actions.interview import InterviewSpec
from hpc_agent.incorporation.build_interview_spec import build_interview_spec

_TG = {"kind": "items_x_seeds", "params": {"seeds": [0, 1, 2]}}
_PRODUCED_BY = {"kind": "agent", "session_sha": "deadbeef"}


def _input(**overrides: Any) -> BuildInterviewSpecInput:
    base: dict[str, Any] = {
        "goal": "train a forecaster",
        "task_count": 3,
        "produced_by": _PRODUCED_BY,
        "tasks_py_mode": "generator",
        "task_generator": _TG,
    }
    base.update(overrides)
    return BuildInterviewSpecInput.model_validate(base)


def _read_spec(tmp_path) -> dict[str, Any]:
    return json.loads((tmp_path / ".hpc" / "interview_spec.json").read_text(encoding="utf-8"))


def test_generator_mode_writes_spec(tmp_path) -> None:
    out = build_interview_spec(tmp_path, spec=_input())
    assert out.wrote is True
    assert out.tasks_py_mode == "generator"
    assert out.has_task_generator is True
    doc = _read_spec(tmp_path)
    assert doc["goal"] == "train a forecaster"
    assert doc["task_generator"]["kind"] == "items_x_seeds"
    # The persisted spec validates as a real InterviewSpec (single-author guarantee).
    InterviewSpec.model_validate(doc)


def test_validate_mode_writes_spec_without_generator(tmp_path) -> None:
    out = build_interview_spec(
        tmp_path,
        spec=BuildInterviewSpecInput.model_validate(
            {
                "goal": "hand-written sweep",
                "task_count": 5,
                "produced_by": _PRODUCED_BY,
                "tasks_py_mode": "validate",
            }
        ),
    )
    assert out.tasks_py_mode == "validate"
    assert out.has_task_generator is False
    doc = _read_spec(tmp_path)
    assert "task_generator" not in doc
    InterviewSpec.model_validate(doc)


def test_absent_goal_rejected_at_schema() -> None:
    with pytest.raises(ValidationError, match="goal"):
        BuildInterviewSpecInput.model_validate(
            {"task_count": 3, "produced_by": _PRODUCED_BY, "tasks_py_mode": "validate"}
        )


def test_empty_goal_rejected_at_schema() -> None:
    with pytest.raises(ValidationError, match="goal"):
        _input(goal="")


def test_generator_mode_without_task_generator_rejected() -> None:
    """generator mode REQUIRES task_generator — the framework can't invent it."""
    with pytest.raises(ValidationError, match="task_generator"):
        BuildInterviewSpecInput.model_validate(
            {
                "goal": "g",
                "task_count": 3,
                "produced_by": _PRODUCED_BY,
                "tasks_py_mode": "generator",
            }
        )


def test_validate_mode_with_task_generator_rejected() -> None:
    """validate mode FORBIDS task_generator — it would silently regenerate tasks.py."""
    with pytest.raises(ValidationError, match="task_generator"):
        BuildInterviewSpecInput.model_validate(
            {
                "goal": "g",
                "task_count": 3,
                "produced_by": _PRODUCED_BY,
                "tasks_py_mode": "validate",
                "task_generator": _TG,
            }
        )


def test_mode_never_inferred_from_absence() -> None:
    """tasks_py_mode is a required field — absence is a schema error, not 'validate'."""
    with pytest.raises(ValidationError, match="tasks_py_mode"):
        BuildInterviewSpecInput.model_validate(
            {"goal": "g", "task_count": 3, "produced_by": _PRODUCED_BY}
        )


def test_entry_point_passed_through(tmp_path) -> None:
    out = build_interview_spec(
        tmp_path,
        spec=_input(entry_point={"kind": "register_run", "run_name": "train"}),
    )
    assert out.has_entry_point is True
    doc = _read_spec(tmp_path)
    # fixed_params defaults to {} on register_run; exclude_none keeps the empty dict.
    assert doc["entry_point"]["kind"] == "register_run"
    assert doc["entry_point"]["run_name"] == "train"


def test_data_axis_hint_on_register_run_is_rejected(tmp_path) -> None:
    """The InterviewSpec entry-point union rejects data_axis_hint on register_run (#260)
    — surfacing as spec_invalid at assembly, not two verbs later."""
    # register_run's model forbids data_axis_hint via extra='forbid'.
    with pytest.raises(ValidationError):
        _input(
            entry_point={
                "kind": "register_run",
                "run_name": "train",
                "data_axis_hint": {"kind": "independent"},
            }
        )


def test_task_count_mismatch_is_not_checked_here(tmp_path) -> None:
    """build-interview-spec assembles; the interview primitive cross-checks counts.
    A task_count that disagrees with the recipe is still a valid spec to persist."""
    out = build_interview_spec(tmp_path, spec=_input(task_count=99))
    assert out.task_count == 99


def test_assembled_spec_invalid_raises_spec_invalid(tmp_path, monkeypatch) -> None:
    """If assembly produces something InterviewSpec rejects, it's spec_invalid."""
    spec = _input()
    # Force the re-validation to fail by monkeypatching InterviewSpec.model_validate.
    import hpc_agent.incorporation.build_interview_spec as bis

    def boom(_data: Any) -> Any:
        raise ValueError("synthetic assembly failure")

    monkeypatch.setattr(bis.InterviewSpec, "model_validate", staticmethod(boom))
    with pytest.raises(errors.SpecInvalid, match="valid InterviewSpec"):
        build_interview_spec(tmp_path, spec=spec)
