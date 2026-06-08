"""Headless campaign driver — advance one workflow step per invocation.

This is deliberately **not** a ``@primitive``. Primitives are pure
JSON-in / JSON-out tools that an agent invokes; this script does the
opposite — it *drives*, and for judgement steps it may spawn an LLM
(``claude -p``). Keeping that out of the primitive layer preserves the
primitives' side-effect contract, testability, and cost transparency.

It reads the ``delegate`` block emitted by ``hpc-agent load-context``
and executes the next step:

- ``kind == "cli"`` — a deterministic step (``monitor`` / ``aggregate``).
  The driver runs the matching ``hpc-agent`` verb directly; no LLM, no
  cost.
- ``kind == "agent"`` — a judgement step (a fresh submission, a campaign
  ``decide``). The driver shells ``claude -p``, but **only** when
  ``--allow-agent-steps`` is passed — spawning an LLM is an explicit,
  opt-in, billable side effect.

One step per invocation: idempotent and cron-friendly. Wrap it in cron
or ``/loop`` to walk a campaign — each tick advances exactly one step
and the on-disk state (run sidecars, journal, cursors) is the only
thing carried between ticks.

Usage::

    python -m hpc_agent.meta.campaign.driver --experiment-dir .
    python -m hpc_agent.meta.campaign.driver --experiment-dir . --dry-run
    python -m hpc_agent.meta.campaign.driver --experiment-dir . --allow-agent-steps
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent._kernel.extension.spawn_prompt import WorkerReport

__all__ = [
    "StepTable",
    "JudgementResolver",
    "CampaignLoopConfig",
    "load_context",
    "plan_action",
    "default_judgement_resolver",
    "main",
]

# The mechanism below is neutral — it does not know about campaigns. The
# domain knowledge stays with the caller, injected as a ``StepTable`` (which
# deterministic verb each ``delegate.step`` maps to) and a
# ``JudgementResolver`` (how an ``agent`` step is actually executed). This is
# the same "mechanism is neutral; the caller owns policy" seam that
# ``_kernel/decision/kernel.py`` establishes one level down.

# A delegate-step name -> the hpc-agent verb that performs that deterministic
# (``kind == "cli"``) step. Injected by the caller; ``CampaignLoopConfig``
# supplies the campaign map.
StepTable = Mapping[str, str]

# How an ``agent`` (judgement) step is executed. Takes the delegate block's
# ``spawn_request`` and the experiment dir; returns the parsed worker report
# (to print as the per-tick record) and the worker's exit code. Injected so
# the loop never hardcodes ``claude -p``; the default wraps ``run_workflow``.
JudgementResolver = Callable[[dict[str, Any], Path], "tuple[WorkerReport, int]"]

# Campaign's deterministic steps. The only campaign-flavored content the loop
# needs — kept here, in the campaign module, and handed to the neutral
# mechanism via ``CampaignLoopConfig``.
_CAMPAIGN_STEP_VERB: dict[str, str] = {
    "monitor": "monitor-flow",
    "aggregate": "aggregate-flow",
}


def load_context(experiment_dir: Path) -> dict[str, Any]:
    """Run ``hpc-agent load-context`` and return the envelope's ``data``.

    Raises :class:`RuntimeError` when the CLI fails or the envelope is
    not ``ok`` — the driver cannot plan a step without context.
    """
    proc = subprocess.run(
        ["hpc-agent", "load-context", "--experiment-dir", str(experiment_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"load-context failed (exit {proc.returncode}): {proc.stderr.strip()}")
    envelope = json.loads(proc.stdout)
    if not envelope.get("ok"):
        raise RuntimeError(f"load-context returned a non-ok envelope: {envelope}")
    data: dict[str, Any] = envelope["data"]
    return data


def plan_action(
    delegate: dict[str, Any] | None,
    *,
    step_table: StepTable,
    allow_agent_steps: bool,
) -> dict[str, Any]:
    """Map a ``delegate`` block to a concrete action intent.

    Pure function (no I/O) so the routing logic is unit-testable. The
    *step_table* (delegate-step -> hpc-agent verb) is injected by the caller —
    the mechanism stays neutral; the campaign map lives in
    :data:`CampaignLoopConfig`. Returns one of:

    - ``{"action": "cli", "verb": ..., "run_id": ..., "step": ...}``
    - ``{"action": "agent", "spawn_request": ..., "step": ...}``
    - ``{"action": "skip", "reason": ...}``
    """
    if not delegate:
        return {"action": "skip", "reason": "load-context returned no delegate block"}

    kind = delegate.get("kind")
    step = delegate.get("step")

    if kind == "cli":
        verb = step_table.get(step) if isinstance(step, str) else None
        if verb is None:
            return {"action": "skip", "reason": f"no cli verb mapped for step {step!r}"}
        run_id = delegate.get("run_id")
        if not run_id:
            return {"action": "skip", "reason": f"cli step {step!r} has no run_id"}
        return {"action": "cli", "verb": verb, "run_id": run_id, "step": step}

    if kind == "agent":
        if not allow_agent_steps:
            return {
                "action": "skip",
                "reason": (
                    f"step {step!r} needs an agent; pass --allow-agent-steps to "
                    "permit the driver to spawn `claude -p` (a billable side effect)"
                ),
            }
        spawn_request = delegate.get("spawn_request")
        if not spawn_request:
            return {
                "action": "skip",
                "reason": f"agent step {step!r} has no spawn_request",
            }
        return {"action": "agent", "spawn_request": spawn_request, "step": step}

    return {"action": "skip", "reason": f"unknown delegate kind {kind!r}"}


def _run_cli_step(verb: str, run_id: str, experiment_dir: Path) -> int:
    """Run a deterministic ``hpc-agent`` workflow verb for *run_id*.

    Both ``monitor-flow`` and ``aggregate-flow`` only *require* ``run_id``
    in their input spec, so a minimal ``{"run_id": ...}`` spec is valid.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=f"{verb}-spec-", delete=False
    ) as handle:
        json.dump({"run_id": run_id}, handle)
        spec_path = handle.name
    try:
        proc = subprocess.run(
            ["hpc-agent", verb, "--spec", spec_path, "--experiment-dir", str(experiment_dir)],
            check=False,
        )
        return proc.returncode
    finally:
        with contextlib.suppress(OSError):
            os.unlink(spec_path)


def default_judgement_resolver(
    spawn_request: dict[str, Any], experiment_dir: Path
) -> tuple[WorkerReport, int]:
    """Resolve a judgement step via ``claude -p`` — the default resolver.

    *spawn_request* is the delegate block's ``spawn_request`` — a
    ``{workflow, experiment_dir, fields}`` dict. It is handed to
    :func:`hpc_agent._kernel.lifecycle.run.run_workflow`, the same
    code-orchestrated entrypoint ``hpc-agent run`` uses: it validates
    and renders the request into the canonical worker prompt, invokes a
    fresh-context worker, and parses the worker's report.

    Returns the parsed report (which the loop prints as the per-tick record)
    and the worker's exit code. This is the exact current Claude path; the
    seam exists so an alternate transport can be injected (#305) without the
    loop knowing.
    """
    from hpc_agent._kernel.lifecycle.run import run_workflow

    report, exit_code, _cache_stats = run_workflow(
        workflow=spawn_request["workflow"],
        experiment_dir=str(experiment_dir),
        fields=spawn_request.get("fields", {}),
    )
    return report, exit_code


def _run_agent_step(
    spawn_request: dict[str, Any],
    experiment_dir: Path,
    resolver: JudgementResolver,
) -> int:
    """Run a judgement step via the injected *resolver* and record it.

    The resolver returns the parsed worker report and the exit code; the
    report is printed so a cron/`/loop` tick leaves a record of the step.
    """
    report, exit_code = resolver(spawn_request, experiment_dir)
    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    return exit_code


@dataclass(frozen=True)
class CampaignLoopConfig:
    """The campaign-flavored configuration the neutral loop runs under.

    Bundles the two seams the loop injects — *step_table* (which deterministic
    verb each ``delegate.step`` maps to) and *resolver* (how a judgement step
    is executed). The defaults reproduce today's ``hpc-campaign-driver``
    behavior exactly: the monitor/aggregate map and the ``run_workflow``-backed
    ``claude -p`` resolver.
    """

    step_table: StepTable = field(default_factory=lambda: _CAMPAIGN_STEP_VERB)
    resolver: JudgementResolver = default_judgement_resolver


def main(argv: list[str] | None = None, *, config: CampaignLoopConfig | None = None) -> int:
    """Advance one campaign workflow step. Returns a process exit code."""
    if config is None:
        config = CampaignLoopConfig()
    parser = argparse.ArgumentParser(
        prog="hpc-campaign-driver",
        description="Advance one campaign workflow step from load-context's delegate block.",
    )
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Experiment repo root (default: cwd).",
    )
    parser.add_argument(
        "--allow-agent-steps",
        action="store_true",
        help="Permit the driver to spawn `claude -p` for judgement steps (billable).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned action and exit without executing it.",
    )
    args = parser.parse_args(argv)

    data = load_context(args.experiment_dir)
    delegate = data.get("delegate")
    plan = plan_action(
        delegate,
        step_table=config.step_table,
        allow_agent_steps=args.allow_agent_steps,
    )

    print(json.dumps({"delegate": delegate, "plan": plan}, indent=2, sort_keys=True))

    if args.dry_run or plan["action"] == "skip":
        return 0
    if plan["action"] == "cli":
        return _run_cli_step(plan["verb"], plan["run_id"], args.experiment_dir)
    if plan["action"] == "agent":
        return _run_agent_step(plan["spawn_request"], args.experiment_dir, config.resolver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
