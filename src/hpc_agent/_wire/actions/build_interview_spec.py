"""Pydantic models for the ``build-interview-spec`` action.

``build-interview-spec`` assembles an :class:`InterviewSpec` from
*discrete* args — the LLM passes values, it never composes the spec JSON.
This kills incident 1b (an autonomous agent hand-authoring the spec and
fabricating a ``task_generator`` inside it): the spec is built in code
from typed fields, and ``goal`` + ``task_generator`` are required.

The discriminated-union shapes (``_EntryPoint`` / ``_TaskGenerator``) and
``_Provenance`` are reused verbatim from
:mod:`hpc_agent._wire.actions.interview` so the assembled
``interview_spec.json`` is byte-identical to what the ``interview``
primitive's schema enforces — there is no second definition to drift.

The ``tasks_py_mode`` flag is the load-bearing discriminator. It is
ALWAYS explicit, never inferred from the absence of ``task_generator``
(the trap a naive "refuse if absent" gate falls into, which would break
the sanctioned hand-written-tasks.py path at
``ops/memory/interview.py:256-288``):

* ``generator`` — the typed recipe regenerates ``.hpc/tasks.py``.
  REQUIRES ``task_generator``.
* ``validate`` — the caller already wrote ``.hpc/tasks.py`` by hand; the
  interview only validates it. FORBIDS ``task_generator``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Reuse the exact discriminated-union / provenance shapes the interview
# primitive validates against — one definition, no drift.
from hpc_agent._wire.actions.interview import (
    _AbortIfSpec,
    _BudgetSpec,
    _ClusterTargetSpec,
    _EntryPoint,
    _Provenance,
    _TaskGenerator,
    _TranscriptTurn,
)


class BuildInterviewSpecInput(BaseModel):
    """Discrete fields the framework assembles into an ``InterviewSpec``.

    Every field here is a value the LLM *relays* (free-text goal, a typed
    recipe node, a provenance dict) — never a hand-composed spec object.
    ``build-interview-spec`` validates these, assembles the
    :class:`InterviewSpec`, and writes ``.hpc/interview_spec.json``.
    """

    model_config = ConfigDict(extra="forbid", title="build-interview-spec input")

    goal: str = Field(
        min_length=1,
        description=(
            "Free-text campaign goal (~one sentence). REQUIRED — genuine "
            "judgment the LLM relays; absence is a spec_invalid, never an "
            "auto-resolution (REQUIRED_CALLER_FIELDS)."
        ),
    )
    task_count: int = Field(
        ge=1,
        description=(
            "Expected number of tasks. The interview materializer "
            "cross-checks tasks.total() == task_count before any disk write."
        ),
    )
    produced_by: _Provenance = Field(
        description="Who/what produced this intent (agent|human + provenance).",
    )
    tasks_py_mode: Literal["generator", "validate"] = Field(
        description=(
            "How tasks.py is produced — ALWAYS explicit, never inferred from "
            "task_generator's absence. 'generator' regenerates .hpc/tasks.py "
            "from the typed recipe (REQUIRES task_generator); 'validate' "
            "validates a hand-written .hpc/tasks.py the caller already wrote "
            "(FORBIDS task_generator). The explicit flag preserves the "
            "sanctioned hand-written-tasks.py path that a 'refuse if absent' "
            "gate would silently break."
        ),
    )
    task_generator: _TaskGenerator | None = Field(
        default=None,
        description=(
            "Typed sweep recipe (one of five shapes). REQUIRED when "
            "tasks_py_mode='generator'; FORBIDDEN when tasks_py_mode='validate'. "
            "REQUIRED_CALLER_FIELDS member — the framework cannot invent it; "
            "absence in generator mode is a spec_invalid, not a fabrication."
        ),
    )
    entry_point: _EntryPoint | None = Field(
        default=None,
        description=(
            "Optional entry-point declaration (register_run | python_module | "
            "shell_command). Passed through to the assembled InterviewSpec."
        ),
    )
    task_kind: str | None = Field(
        default=None,
        description="Free-text tag grouping related campaigns for recall.",
    )
    budget: _BudgetSpec | None = Field(default=None, description="Optional soft caps.")
    abort_if: _AbortIfSpec | None = Field(
        default=None, description="Optional early-stop criterion."
    )
    cluster_target: _ClusterTargetSpec | None = Field(
        default=None, description="Optional pre-resolved cluster target."
    )
    transcript: list[_TranscriptTurn] | None = Field(
        default=None, description="Optional interview Q/A turns."
    )
    notes: str | None = Field(default=None, description="Optional free-form notes.")

    @model_validator(mode="after")
    def _check_tasks_py_mode(self) -> BuildInterviewSpecInput:
        # The explicit-mode invariant. 'generator' needs the recipe;
        # 'validate' must NOT carry one (it would silently regenerate over
        # the caller's hand-written tasks.py). Enforced here so the seam is
        # the same whether the spec arrives via CLI --spec or in-process.
        if self.tasks_py_mode == "generator" and self.task_generator is None:
            raise ValueError(
                "tasks_py_mode='generator' requires task_generator; the typed recipe "
                "is what regenerates .hpc/tasks.py. task_generator is a "
                "REQUIRED_CALLER_FIELDS member — the framework cannot invent it."
            )
        if self.tasks_py_mode == "validate" and self.task_generator is not None:
            raise ValueError(
                "tasks_py_mode='validate' forbids task_generator; validate mode "
                "consumes the caller's hand-written .hpc/tasks.py. Passing a "
                "task_generator would silently regenerate over it — use "
                "tasks_py_mode='generator' if you want the recipe to win."
            )
        return self


class BuildInterviewSpecResult(BaseModel):
    """Shape of the ``data`` block on a ``build-interview-spec`` envelope."""

    model_config = ConfigDict(extra="forbid", title="build-interview-spec output")

    spec_path: str = Field(
        description="Absolute path to the written .hpc/interview_spec.json.",
    )
    tasks_py_mode: Literal["generator", "validate"] = Field(
        description="The explicit mode the spec was assembled under.",
    )
    goal: str = Field(description="The relayed goal, echoed back.")
    task_count: int = Field(ge=1, description="The declared task_count, echoed back.")
    has_task_generator: bool = Field(
        description="Whether the assembled spec carries a task_generator.",
    )
    has_entry_point: bool = Field(
        description="Whether the assembled spec carries an entry_point.",
    )
    wrote: bool = Field(description="Always true on success (the spec file was written).")
