"""``classify-campaign-path`` primitive — AST pattern-match for the campaign path.

The campaign ``path`` decision — *manual fixed grid* (Path A) vs.
*strategy-driven adaptive sampling* (Path B) — was prose the worker
inferred by reading ``tasks.py``. But the discriminator is a **structural
fact about the code on disk**: does ``tasks.py`` import an optimizer
(Optuna / scikit-optimize / Hyperopt / …) and drive it with the
ask/tell/``prior`` loop, or does it enumerate a fixed grid? That is
exactly the kind of signal a stdlib AST scan resolves deterministically
— the same migration the ``classify-axis-easy`` matcher already makes for
the ``axis_class`` point.

So this primitive moves the common cases out of the LLM: a confident
hit (optimizer imports + ask/tell/``prior`` calls → ``strategy``; a
clean parse with none of them → ``manual``) is ``decided_by="code"``;
only the genuinely-unclassifiable tail (the source did not parse, or a
custom optimizer the matcher does not recognize) escalates to judgement
with both candidates. It also reports ``supports_async_concurrency`` —
the code signal the ``concurrency`` point consults before deciding how
many iterations to run in flight (the *aggressiveness* of that choice
stays judgement; whether the strategy *can* run async is a code fact).

Stdlib-only and total — it never raises (a parse error surfaces as the
``unclassifiable`` verdict in envelope ``data``), so the primitive
declares ``error_codes=[]``: uncertainty rides in ``data``, never on an
error channel. The classification routes through the shared
:func:`hpc_agent._kernel.decision.decide` kernel like every other
decision point.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = ["classify_campaign_path", "scan_campaign_path"]

# Top-level modules whose presence signals strategy-driven (Path B) sampling.
_OPTIMIZER_MODULES: frozenset[str] = frozenset(
    {"optuna", "skopt", "hyperopt", "ax", "nevergrad", "smac", "bayes_opt"}
)
# Imported names that signal an optimizer even from a broad library (sklearn).
_OPTIMIZER_NAMES: frozenset[str] = frozenset(
    {"RandomizedSearchCV", "BayesSearchCV", "HalvingRandomSearchCV", "Study", "Trial"}
)
# Call attribute/function names that signal an ask/tell optimization loop.
# ``prior`` is the framework's own history reader — a strong Path-B tell.
_STRATEGY_CALLS: frozenset[str] = frozenset(
    {"ask", "tell", "create_study", "prior", "fmin", "minimize", "RandomizedSearchCV"}
)


def scan_campaign_path(source: str) -> tuple[set[str], bool]:
    """Return ``(signals, parsed)`` for *source* — the AST evidence vector.

    *signals* is the set of optimizer imports / ask-tell calls found;
    *parsed* is False when the source is not valid Python (→ unclassifiable).
    Total: never raises.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set(), False

    signals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _OPTIMIZER_MODULES:
                    signals.add(f"import:{top}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in _OPTIMIZER_MODULES:
                signals.add(f"from:{top}")
            for alias in node.names:
                if alias.name in _OPTIMIZER_NAMES:
                    signals.add(f"name:{alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            attr = ""
            if isinstance(func, ast.Attribute):
                attr = func.attr
            elif isinstance(func, ast.Name):
                attr = func.id
            if attr in _STRATEGY_CALLS or attr.startswith("suggest_"):
                signals.add(f"call:{attr}")
    return signals, True


@primitive(
    name="classify-campaign-path",
    verb="query",
    side_effects=[],
    error_codes=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Stdlib-only AST pattern-match for a campaign's tasks.py: is it a "
            "manual fixed grid (Path A) or strategy-driven adaptive sampling "
            "(Path B)? Returns {path, decided_by, signals, "
            "supports_async_concurrency, candidates}. `path` is manual / "
            "strategy / unclassifiable. A confident hit is decided_by=code; "
            "an unparseable / unrecognized tasks.py escalates "
            "(decided_by=judgement) with both candidates for the LLM."
        ),
        args=(
            CliArg(
                "--source-path",
                type=str,
                required=True,
                help="Path to the campaign's tasks.py (or any .py defining total()/resolve()).",
            ),
        ),
    ),
    agent_facing=True,
)
def classify_campaign_path(*, source_path: str) -> dict[str, Any]:
    """Classify a campaign's ``tasks.py`` as manual vs. strategy-driven.

    Routes the ``path`` decision point through the shared decision kernel:
    the strategy rule fires on any optimizer signal (``decided_by="code"``,
    ``path="strategy"``); else a clean parse resolves to ``path="manual"``;
    an unparseable source abstains and escalates (``decided_by="judgement"``,
    ``path="unclassifiable"``) with both candidates.
    """
    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation

    try:
        source = Path(source_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        source = ""

    signals, parsed = scan_campaign_path(source) if source else (set(), False)
    # The strategy can run async when it's an ask/tell optimizer built for it —
    # Optuna's `constant_liar=True` is the canonical signal. Conservative: only
    # claim async support when the evidence is explicit.
    supports_async = bool(signals) and (
        "constant_liar" in source or any(s.startswith(("import:optuna", "from:optuna")) for s in signals)
    )

    def _strategy_rule(_: Any) -> CandidateAction | None:
        if signals:
            return CandidateAction(
                action="strategy",
                source="catalog",
                rationale=f"optimizer signals present: {sorted(signals)}",
            )
        return None

    def _manual_rule(_: Any) -> CandidateAction | None:
        if parsed:
            return CandidateAction(
                action="manual",
                source="catalog",
                rationale="tasks.py parsed with no optimizer imports / ask-tell calls — fixed grid",
            )
        return None

    def _unclassifiable(_: Any) -> Escalation:
        return Escalation(
            decided_by="judgement",
            reason="tasks.py did not parse — manual vs strategy is ambiguous; the LLM decides",
            candidate_actions=[
                CandidateAction(action="manual", source="catalog"),
                CandidateAction(action="strategy", source="catalog"),
            ],
        )

    decision = decide(
        "path",
        None,
        rules=[_strategy_rule, _manual_rule],
        on_abstain=_unclassifiable,
    )
    return {
        "path": decision.chosen.action if decision.chosen is not None else "unclassifiable",
        "decided_by": decision.decided_by,
        "signals": sorted(signals),
        "supports_async_concurrency": supports_async,
        "reason": decision.reason,
        "candidates": (
            [c.action for c in decision.escalation.candidate_actions]
            if decision.escalation is not None
            else []
        ),
    }
