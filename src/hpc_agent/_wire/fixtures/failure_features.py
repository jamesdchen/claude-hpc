"""Pydantic model for the structured ``failure_features`` evidence block.

Authoring SoT for ``hpc_agent/schemas/failure_features.json`` (a
cross-cutting shape, registered verbatim in
``scripts/build_schemas.py:_NON_SUFFIX_MAPPING`` like ``envelope.json``).

Captures the diagnostic evidence a diagnosis would need to classify
*why* an operation failed, independent of *what* failed — the feature
set, not an enumeration of failure modes (the failure space is open).
Every field is optional and populated only where the operation can
supply it, so a bare ``{}`` is valid. This layer produces evidence; it
decides no recovery. See issue #230.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import FailureClass


class _TemporalContext(BaseModel):
    """When in the operation's life it failed — the single most
    discriminating feature (structural vs. runtime)."""

    model_config = ConfigDict(extra="forbid")

    phase: Literal["first_attempt", "after_progress", "unknown"] = Field(
        description=(
            "'first_attempt' = failed before any unit of work succeeded "
            "(points at a structural/config problem). 'after_progress' = "
            "failed after N units succeeded (points at a runtime/data-dependent "
            "problem). 'unknown' when the operation cannot tell."
        ),
    )
    successful_units: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Count of units (tasks, requests, rounds) that succeeded before "
            "the failure, when known."
        ),
    )


class _AttemptsThisEpisode(BaseModel):
    """What recovery has already been tried this failure episode — lets a
    decision-maker avoid re-trying an exhausted strategy."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(
        ge=0,
        description="Number of recovery attempts already made this episode.",
    )
    strategies: list[str] | None = Field(
        default=None,
        description=(
            "Ordered list of recovery strategies already applied (e.g. 'retry', "
            "'resubmit', 'restart_service', 'increase_walltime')."
        ),
    )


class _LivenessVsCorrectness(BaseModel):
    """Distinguishes 'down' from 'up-but-broken' by separating a liveness
    signal (does it respond?) from a correctness signal (does a real request
    return a valid result?). The classic silent-rot case is liveness=pass,
    correctness=fail."""

    model_config = ConfigDict(extra="forbid")

    liveness: Literal["pass", "fail", "unknown"] = Field(
        description="Result of a liveness check (port open / process alive / health endpoint).",
    )
    correctness: Literal["pass", "fail", "unknown"] = Field(
        description=(
            "Result of a real-path request (an actual request that exercises the "
            "backing work, not a port ping)."
        ),
    )
    detail: str | None = None


class _LogTail(BaseModel):
    """Bounded, normalized tail of the relevant log. Callers MUST bound this
    (the schema does not transport unbounded logs) and SHOULD normalize
    volatile tokens (timestamps, paths, addresses) so signatures cluster."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The normalized log tail.")
    truncated: bool = Field(
        default=False,
        description="True when content was dropped to fit the bound.",
    )
    source: str | None = Field(
        default=None,
        description="Where the tail came from, e.g. 'stderr', 'task.log', 'service.stdout'.",
    )


class _Probe(BaseModel):
    """One targeted probe result from a caller-supplied probe hook."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Probe identifier, e.g. 'gpu_visible', 'service_real_request', 'scratch_writable'.",
    )
    ok: bool = Field(description="Whether the probe passed.")
    detail: str | None = None


class FailureFeatures(BaseModel):
    """Structured diagnostic evidence attached to an ``ok=false`` envelope on
    operation failure. Evidence only; no recovery logic. See issue #230."""

    model_config = ConfigDict(extra="forbid")

    error_class_raw: str | None = Field(
        default=None,
        description=(
            "The failure signature exactly as the producer (a failing op or a "
            "caller-supplied probe hook) wrote it. Open, ungoverned, preserved "
            "verbatim — the framework cannot enforce a vocabulary across producers "
            "it does not own, and never rejects or rewrites this value. Used for "
            "audit and as the input to normalization."
        ),
    )
    error_class: FailureClass | None = Field(
        default=None,
        description=(
            "The canonical failure class, assigned by the framework's normalizer "
            "at ingest (NOT written by the producer) by mapping 'error_class_raw' "
            "onto a controlled, framework-owned vocabulary. This is the field "
            "deterministic recovery policy and the recall/memory subsystem index "
            "on, so it MUST come from a value space the framework governs. "
            "'unknown' means the normalizer could not canonicalize the raw string "
            "— the well-defined trigger to escalate to a decision-maker (e.g. the "
            "agentic layer), whose resolution should feed a new raw->canonical "
            "mapping back into the normalizer rather than minting a new free-text "
            "class. Distinct from the envelope's coarser 'error_code' — several "
            "error_classes can map to one error_code. MUST stay aligned with the "
            "'error_class' field in failures.output.json (same vocabulary, same "
            "normalizer)."
        ),
    )
    resource_spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The resource/sweep spec in effect at the moment of failure (e.g. GPU "
            "type/count, scheduler-level params, sampling width). The discriminator "
            "that turns a shared signature into opposite fixes — e.g. OOM at "
            "tp_size=2 vs OOM at n=128."
        ),
    )
    temporal_context: _TemporalContext | None = None
    attempts_this_episode: _AttemptsThisEpisode | None = None
    liveness_vs_correctness: _LivenessVsCorrectness | None = None
    log_tail: _LogTail | None = None
    probes: list[_Probe] | None = Field(
        default=None,
        description="Optional targeted probe outputs from caller-supplied probe hooks.",
    )
