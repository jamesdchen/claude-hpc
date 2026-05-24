"""``apply-smart-submit-plan`` — code-ifies Step 4c-B of submit.md.

Given (a) the agent's base submit spec and (b) the verbatim
``score-submit-plan`` envelope from a prior planner call, apply the
auto-pick + auto-apply rules and emit a refined spec plus an audit
list of decisions taken.

The auto-pick rule is per-candidate: when a candidate's
``recommended_tuple.predicted_eta_sec`` is not None, SLURM has
confirmed a fitting backfill window for the right-sized resource
tuple — use it automatically. The auto-apply rule for
``array_reshape`` is similarly mechanical: when present, apply.
The third planner output, ``walltime_split``, is NOT auto-applied —
``walltime_split.requires_checkpointing=true`` means the executor
must checkpoint at segment boundaries; if it doesn't, chaining
kills work at every segment. So we surface the split as a
``walltime_split_confirm`` decision and let the caller decide.

The body never calls ``score-submit-plan``; the envelope arrives
pre-computed. ``composes=["score-submit-plan"]`` is workflow-graph
metadata, not a runtime invocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape, SchemaRef

from hpc_agent_pro._schema_models.workflows.apply_smart_submit_plan import (
    ApplySmartSubmitPlanResult,
    ApplySmartSubmitPlanSpec,
)

if TYPE_CHECKING:
    from pathlib import Path


# Keys the recommended_tuple writes into the refined spec. Anything not
# in this set passes through unchanged; anything in this set is
# overwritten when the auto-pick fires.
_TUPLE_KEYS = ("constraint", "walltime_sec", "mem_mb", "cpus")


def _find_auto_pick_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the first candidate whose ``recommended_tuple.predicted_eta_sec is not None``.

    Per submit.md Step 4c-B: when a candidate's predicted_eta_sec is
    present, SLURM has confirmed a fitting backfill window — that's
    the auto-pick signal. If multiple candidates qualify, take the
    first one (the agent's cost rubric handles tie-breaking; here we
    only need a deterministic auto-pick rule).
    """
    for cand in candidates or []:
        rec = (cand or {}).get("recommended_tuple") or {}
        if rec.get("predicted_eta_sec") is not None:
            return cand
    return None


@primitive(
    name="apply-smart-submit-plan",
    verb="workflow",
    composes=["score-submit-plan"],
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Apply Step 4c-B of submit.md: refine a base submit spec via "
            "a score-submit-plan envelope. Auto-picks the lattice-confirmed "
            "tuple, auto-applies array_reshape, and escalates walltime_split "
            "as a pending decision (requires checkpointing posture confirmation)."
        ),
        spec_arg=True,
        spec_model=ApplySmartSubmitPlanSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="apply_smart_submit_plan"),
    ),
    agent_facing=True,
)
def apply_smart_submit_plan(
    experiment_dir: Path,  # noqa: ARG001 — accepted for CliShape uniformity
    *,
    spec: ApplySmartSubmitPlanSpec,
) -> ApplySmartSubmitPlanResult:
    """Refine *spec.submit_spec* using *spec.score_submit_plan_envelope*.

    Returns a refined spec dict plus a decisions list:

    * Auto-applied: ``recommended_tuple`` (when any candidate has
      ``predicted_eta_sec is not None``).
    * Auto-applied: ``array_reshape.recommended_max_array_size``.
    * Escalated: ``walltime_split`` (surfaced as
      ``walltime_split_confirm`` pending decision; never auto-applied).
    """
    refined = dict(spec.submit_spec)
    envelope = spec.score_submit_plan_envelope or {}
    decisions: list[dict[str, Any]] = []

    # ── Auto-pick: recommended_tuple ──────────────────────────────────
    candidates = envelope.get("candidates") or []
    if not isinstance(candidates, list):
        raise errors.SpecInvalid(
            f"score_submit_plan_envelope.candidates must be a list; got {type(candidates).__name__}"
        )
    pick = _find_auto_pick_candidate(candidates)
    if pick is not None:
        rec = pick.get("recommended_tuple") or {}
        applied: dict[str, Any] = {}
        for key in _TUPLE_KEYS:
            if key in rec and rec[key] is not None:
                refined[key] = rec[key]
                applied[key] = rec[key]
        decisions.append(
            {
                "point": "recommended_tuple",
                "outcome": "auto_applied",
                "why": rec.get(
                    "rationale",
                    "predicted_eta_sec is not None — SLURM confirmed a fitting backfill window.",
                ),
                "applied": applied,
                "predicted_eta_sec": rec.get("predicted_eta_sec"),
            }
        )

    # ── Auto-apply: array_reshape ─────────────────────────────────────
    array_reshape = envelope.get("array_reshape")
    if isinstance(array_reshape, dict):
        new_size = array_reshape.get("recommended_max_array_size")
        if new_size is not None:
            refined["max_array_size"] = new_size
            decisions.append(
                {
                    "point": "array_reshape",
                    "outcome": "auto_applied",
                    "why": array_reshape.get(
                        "rationale",
                        "Cluster-wide array-size reshape to fit the backfill window.",
                    ),
                    "applied": {"max_array_size": new_size},
                    "from": array_reshape.get("current_max_array_size"),
                    "to": new_size,
                }
            )

    # ── Escalate: walltime_split (NOT auto-applied) ───────────────────
    walltime_split = envelope.get("walltime_split")
    if isinstance(walltime_split, dict):
        decisions.append(
            {
                "point": "walltime_split_confirm",
                "outcome": "pending",
                "why": (
                    "walltime_split surfaced. requires_checkpointing="
                    f"{walltime_split.get('requires_checkpointing')!r}; the caller "
                    "must confirm the executor checkpoints at segment boundaries "
                    "before chaining (otherwise work is killed at every segment)."
                ),
                "split_recommendation": dict(walltime_split),
            }
        )

    return ApplySmartSubmitPlanResult(refined_spec=refined, decisions=decisions)
