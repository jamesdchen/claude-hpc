"""``build-interview-spec`` primitive — assemble an InterviewSpec in CODE.

Surface 2, incident 1b. The LLM stops hand-authoring the interview spec
JSON: it passes *discrete* values (goal, a typed task_generator node,
provenance, an explicit tasks_py_mode), and this primitive assembles the
:class:`~hpc_agent._wire.actions.interview.InterviewSpec` and writes it to
``.hpc/interview_spec.json``. Because the spec is built from typed fields
in code, there is no JSON for the LLM to fabricate a ``task_generator``
inside.

``goal`` and ``task_generator`` are the REQUIRED_CALLER_FIELDS:

* ``goal`` is required by the input model (``min_length=1``) — absence is
  a schema rejection mapped to ``spec_invalid``.
* ``task_generator`` is required *by mode*: ``tasks_py_mode='generator'``
  needs it; ``tasks_py_mode='validate'`` forbids it. The mode is ALWAYS
  explicit — never inferred from absence — which preserves the sanctioned
  hand-written-tasks.py path (``ops/memory/interview.py:256-288``) that a
  "refuse if task_generator absent" gate would break.

This primitive only *assembles and persists* the spec. The ``interview``
primitive consumes ``.hpc/interview_spec.json`` (or the same shape) to
materialize / validate ``.hpc/tasks.py`` — that seam is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.build_interview_spec import (
    BuildInterviewSpecInput,
    BuildInterviewSpecResult,
)
from hpc_agent._wire.actions.interview import InterviewSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.io import atomic_write_json

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["build_interview_spec"]


def _build_interview_spec_result_post(result: BuildInterviewSpecResult) -> dict[str, Any]:
    """Project the typed result into the envelope ``data`` dict."""
    return result.model_dump(mode="json")


@primitive(
    name="build-interview-spec",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/interview_spec.json"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Assemble an InterviewSpec from discrete args (goal, task_count, "
            "produced_by, an explicit tasks_py_mode, an optional typed "
            "task_generator + entry_point) and write it to "
            ".hpc/interview_spec.json. The LLM passes values; it never "
            "composes the spec JSON. goal + task_generator are required "
            "(task_generator by mode: generator needs it, validate forbids "
            "it) → spec_invalid otherwise."
        ),
        spec_arg=True,
        schema_ref=SchemaRef(input="build_interview_spec"),
        spec_model=BuildInterviewSpecInput,
        experiment_dir_arg=True,
        result_post=_build_interview_spec_result_post,
        requires_ssh=False,
    ),
    agent_facing=True,
)
def build_interview_spec(
    experiment_dir: Path,
    *,
    spec: BuildInterviewSpecInput,
) -> BuildInterviewSpecResult:
    """Assemble an :class:`InterviewSpec` from *spec*'s discrete fields and persist it.

    The wire-validated *spec* has already enforced the
    ``tasks_py_mode``↔``task_generator`` invariant (generator requires the
    recipe; validate forbids it) and the ``goal`` ``min_length=1`` floor.
    This body assembles the canonical :class:`InterviewSpec` — re-validating
    through the *same* model the ``interview`` primitive consumes, so a
    structurally-impossible combination surfaces as ``spec_invalid`` here,
    not downstream — and writes ``.hpc/interview_spec.json`` atomically.

    Returns a :class:`BuildInterviewSpecResult` with the written path and
    an echo of the load-bearing fields.

    Raises :class:`errors.SpecInvalid` when the assembled fields do not
    form a valid ``InterviewSpec`` (e.g. an entry-point constraint the
    discriminated union rejects).
    """
    # Assemble the InterviewSpec field-for-field. task_generator / entry_point
    # are already typed discriminated-union nodes on the input model, so they
    # carry straight across; exclude_none drops the optionals InterviewSpec
    # also defaults to None, keeping the persisted JSON minimal and matching
    # what the SKILL hand-wrote.
    assembled: dict[str, Any] = {
        "goal": spec.goal,
        "task_count": spec.task_count,
        "produced_by": spec.produced_by.model_dump(exclude_none=True, mode="json"),
    }
    if spec.task_generator is not None:
        assembled["task_generator"] = spec.task_generator.model_dump(exclude_none=True, mode="json")
    if spec.entry_point is not None:
        assembled["entry_point"] = spec.entry_point.model_dump(exclude_none=True, mode="json")
    for opt in ("task_kind", "budget", "abort_if", "cluster_target", "transcript", "notes"):
        value = getattr(spec, opt)
        if value is None:
            continue
        if hasattr(value, "model_dump"):
            assembled[opt] = value.model_dump(exclude_none=True, mode="json")
        elif isinstance(value, list):
            assembled[opt] = [
                v.model_dump(exclude_none=True, mode="json") if hasattr(v, "model_dump") else v
                for v in value
            ]
        else:
            assembled[opt] = value

    # Re-validate through the InterviewSpec model the `interview` primitive
    # consumes. This is the single-author guarantee: build-interview-spec
    # emits exactly what interview accepts, so a mismatch is a spec_invalid
    # here (at assembly) rather than a confusing failure two verbs later.
    try:
        interview_spec = InterviewSpec.model_validate(assembled)
    except (ValueError, TypeError) as exc:
        raise errors.SpecInvalid(
            f"assembled fields do not form a valid InterviewSpec: {exc}"
        ) from exc

    spec_path = experiment_dir / ".hpc" / "interview_spec.json"
    atomic_write_json(spec_path, interview_spec.model_dump(exclude_none=True, mode="json"))

    return BuildInterviewSpecResult(
        spec_path=str(spec_path),
        tasks_py_mode=spec.tasks_py_mode,
        goal=spec.goal,
        task_count=spec.task_count,
        has_task_generator=spec.task_generator is not None,
        has_entry_point=spec.entry_point is not None,
        wrote=True,
    )
