"""PETSc solver adapter — checkpoint injection via monitors + the options database.

PETSc owns its solve loop (``TSSolve`` / ``SNESSolve`` are C code), so the
framework cannot place ``should_checkpoint()`` calls inside it. PETSc instead
exposes two hooks this adapter targets:

* **Monitor callbacks** (petsc4py): PETSc invokes a registered monitor once
  per step. :func:`make_checkpoint_monitor` builds a monitor whose body is the
  existing :func:`~hpc_agent.experiment_kit.checkpoint.should_checkpoint`
  cadence plus a PETSc-binary solution dump — the two-line instrumentation for
  a petsc4py script::

      ts.setMonitor(make_checkpoint_monitor())  # before ts.solve()

  Checkpoints land as ``checkpoint-<step>.petscbin`` under the same stable
  ``_checkpoints/`` dir the pickle helpers use (``HPC_CHECKPOINT_DIR``), so
  they survive a retry to a ``resubmit --from-checkpoint`` the same way.
  They are PETSc binary Vec dumps, NOT pickles — the ``.petscbin`` suffix
  keeps them invisible to ``read_latest_checkpoint`` (which would otherwise
  try to unpickle them); :func:`latest_petsc_checkpoint` is their
  discovery counterpart.

* **The options database** (opaque binaries): any PETSc app that calls
  ``setFromOptions()`` honors ``PETSC_OPTIONS`` from the environment.
  :func:`checkpoint_options` renders the fragment that turns on per-step
  binary solution dumping (``-ts_monitor_solution binary:<path>``), and
  :func:`canary_options` the fragment that caps the solve at two steps for
  the checkpoint-canary probe — so a compiled solver checkpoint-instruments
  with zero source changes. The materialized-wrapper integration lives in
  :mod:`hpc_agent.incorporation.wrap_entry_point`.

Cadence honesty: the options-database path can only dump per step — PETSc has
no walltime awareness — so an opaque binary degrades to step-cadence
checkpointing. Only the petsc4py monitor path gets true
``walltime_margin`` / ``interval`` semantics.

Resume honesty: *writing* checkpoints is generic; *loading* one is
app-specific (there is no universal PETSc restart option). The wrapper path
therefore requires the app to declare its restart flag (``resume_flag``) and
rotates the previous attempt's solution dump to ``petsc-restart.bin`` via
:func:`promote_restart` before the monitor truncates a fresh one.

Stdlib-only at import time; ``petsc4py`` is imported lazily inside the
monitor's viewer factory, so this module is safe to import at dispatch time
on a cluster runtime without PETSc.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hpc_agent.experiment_kit.checkpoint import checkpoint_dir, should_checkpoint

__all__ = [
    "PetscDetection",
    "detect_petsc_solver",
    "checkpoint_options",
    "canary_options",
    "resume_args",
    "wrapper_solution_path",
    "promote_restart",
    "petsc_checkpoint_path",
    "latest_petsc_checkpoint",
    "make_checkpoint_monitor",
]

# petsc4py class name → adapter solver kind. TS (time stepper) and SNES
# (nonlinear solver) are the two loop-owning objects a PDE script solves
# through; KSP solves are usually nested inside one of these.
_PETSC_SOLVER_CLASSES: dict[str, str] = {"TS": "ts", "SNES": "snes"}

# Per-kind options-database fragments. Only options known to exist are
# rendered here — ``-ts_monitor_solution`` / ``-snes_monitor_solution`` take a
# viewer spec (``binary:<path>``) and dump the solution each step;
# ``-ts_max_steps`` / ``-snes_max_it`` cap the solve for the canary probe.
_MONITOR_OPTION: dict[str, str] = {
    "ts": "-ts_monitor_solution",
    "snes": "-snes_monitor_solution",
}
_CANARY_OPTION: dict[str, str] = {
    "ts": "-ts_max_steps 2",
    "snes": "-snes_max_it 2",
}

# Wrapper-path file names under the stable ``_checkpoints/`` dir. The monitor
# appends every step into ONE file (PETSc binary viewers append); the restart
# rotation gives the resumed attempt a file the fresh monitor won't truncate.
_WRAPPER_SOLUTION = "petsc-solution.bin"
_WRAPPER_RESTART = "petsc-restart.bin"

# Monitor-path (petsc4py) per-step checkpoint naming. Deliberately parallel to
# ``checkpoint-<n>.pkl`` but with a distinct suffix: these are PETSc binary Vec
# dumps, and the pickle helpers must never try to load them.
_PETSC_CHECKPOINT_RE = re.compile(r"^checkpoint-(\d+)\.petscbin$")

# An app restart flag must look like a CLI flag — one or two leading dashes
# then an identifier-ish token. Rejects argv injection through the wire field.
_RESUME_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9_][A-Za-z0-9_\-]*$")


class PetscDetection:
    """Outcome of :func:`detect_petsc_solver` — what was found and where.

    Light value type (mirrors ``WrapperResult``), not a wire surface.
    ``sets_from_options`` is the capability gate for options-database
    injection: a script that never calls ``setFromOptions()`` ignores
    ``PETSC_OPTIONS`` and can only be instrumented via a monitor.
    """

    __slots__ = ("solver_var", "solver_kind", "sets_from_options", "evidence")

    def __init__(
        self, *, solver_var: str, solver_kind: str, sets_from_options: bool, evidence: str
    ) -> None:
        self.solver_var = solver_var
        self.solver_kind = solver_kind
        self.sets_from_options = sets_from_options
        self.evidence = evidence


def _imports_petsc4py(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "petsc4py" or a.name.startswith("petsc4py.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "petsc4py" or mod.startswith("petsc4py."):
                return True
    return False


def _ctor_kind(expr: ast.AST) -> str | None:
    """The solver kind constructed by *expr*, or None.

    Descends call/attribute chains so the common petsc4py idioms all match:
    ``PETSc.TS()``, ``PETSc.TS().create(comm)``, ``petsc4py.PETSc.SNES()``.
    """
    node: ast.AST = expr
    while True:
        if isinstance(node, ast.Call):
            node = node.func
            continue
        if isinstance(node, ast.Attribute):
            if node.attr in _PETSC_SOLVER_CLASSES:
                base = node.value
                if isinstance(base, ast.Name) and base.id == "PETSc":
                    return _PETSC_SOLVER_CLASSES[node.attr]
                if isinstance(base, ast.Attribute) and base.attr == "PETSc":
                    return _PETSC_SOLVER_CLASSES[node.attr]
            node = node.value
            continue
        return None


def detect_petsc_solver(source: str) -> PetscDetection | None:
    """Detect a petsc4py TS/SNES solve in *source*; None when absent.

    Same contract as the axis matchers (e.g. ``_match_stencil``): a hit
    requires positive evidence — a petsc4py import, a ``PETSc.TS()`` /
    ``PETSc.SNES()`` construction bound to a name, and a ``.solve()`` call on
    that name. Unparseable source is a miss, not an error (the entry-point
    scan feeds arbitrary candidate files through here).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not _imports_petsc4py(tree):
        return None

    solver_vars: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            kind = _ctor_kind(node.value)
            if kind is not None:
                solver_vars[node.targets[0].id] = kind
    if not solver_vars:
        return None

    solved: str | None = None
    sets_opts: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in solver_vars
        ):
            if node.func.attr == "solve" and solved is None:
                solved = node.func.value.id
            elif node.func.attr == "setFromOptions":
                sets_opts.add(node.func.value.id)
    if solved is None:
        return None

    kind = solver_vars[solved]
    return PetscDetection(
        solver_var=solved,
        solver_kind=kind,
        sets_from_options=solved in sets_opts,
        evidence=(
            f"petsc4py import + {solved} = PETSc.{kind.upper()}(...) + {solved}.solve()"
            + (f"; {solved}.setFromOptions() honors PETSC_OPTIONS" if solved in sets_opts else "")
        ),
    )


def _require_known_kind(solver_kind: str) -> None:
    if solver_kind not in _MONITOR_OPTION:
        raise ValueError(
            f"unknown PETSc solver kind: {solver_kind!r} "
            f"(expected one of {sorted(_MONITOR_OPTION)})"
        )


def checkpoint_options(*, solver_kind: str = "ts", solution_path: str | os.PathLike[str]) -> str:
    """The ``PETSC_OPTIONS`` fragment that dumps the solution each step.

    ``PETSC_OPTIONS`` tokenizes on whitespace with no quoting, so a path
    containing whitespace cannot be expressed — rejected loudly rather than
    silently mis-split. Framework result dirs never contain whitespace.
    """
    _require_known_kind(solver_kind)
    path = str(solution_path)
    if re.search(r"\s", path):
        raise ValueError(f"PETSC_OPTIONS cannot carry a whitespace path: {path!r}")
    return f"{_MONITOR_OPTION[solver_kind]} binary:{path}"


def canary_options(solver_kind: str = "ts") -> str:
    """The ``PETSC_OPTIONS`` fragment capping the solve at 2 steps (canary)."""
    _require_known_kind(solver_kind)
    return _CANARY_OPTION[solver_kind]


def resume_args(resume_flag: str, checkpoint_path: str | os.PathLike[str]) -> list[str]:
    """The argv tail handing the app its declared restart flag + checkpoint."""
    if not _RESUME_FLAG_RE.match(resume_flag):
        raise ValueError(
            f"resume_flag {resume_flag!r} does not look like a CLI flag (expected -name or --name)"
        )
    return [resume_flag, str(checkpoint_path)]


def wrapper_solution_path(result_dir: str | os.PathLike[str] | None = None) -> Path:
    """Where the wrapper-path monitor dumps the solution (single appended file)."""
    return checkpoint_dir(result_dir) / _WRAPPER_SOLUTION


def promote_restart(result_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """Rotate the previous attempt's solution dump into the restart slot.

    Called by the materialized wrapper BEFORE launching the app. The monitor
    option truncates ``petsc-solution.bin`` when the solver starts, so a
    resumed attempt must not read its restart state from the same path the
    new attempt writes to. Rotation:

    * a non-empty ``petsc-solution.bin`` exists → rename it to
      ``petsc-restart.bin`` (clobbering an older restart) and return that;
    * otherwise a non-empty ``petsc-restart.bin`` from an earlier rotation
      (the previous attempt died before its first dump) → return it as-is;
    * otherwise → None (fresh run, nothing to resume from).
    """
    d = checkpoint_dir(result_dir)
    solution = d / _WRAPPER_SOLUTION
    restart = d / _WRAPPER_RESTART
    try:
        if solution.is_file() and solution.stat().st_size > 0:
            os.replace(solution, restart)
            return restart
        if restart.is_file() and restart.stat().st_size > 0:
            return restart
    except OSError:
        return None
    return None


def petsc_checkpoint_path(step: int, result_dir: str | os.PathLike[str] | None = None) -> Path:
    """The monitor-path checkpoint file for *step* (not created)."""
    return checkpoint_dir(result_dir) / f"checkpoint-{int(step)}.petscbin"


def latest_petsc_checkpoint(result_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """The highest-step non-empty ``checkpoint-<n>.petscbin``, or None.

    Discovery counterpart of the monitor's writes — the analog of
    :func:`~hpc_agent.experiment_kit.checkpoint.latest_checkpoint` for PETSc
    binary checkpoints (which the pickle helper deliberately does not see).
    """
    d = checkpoint_dir(result_dir)
    if not d.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for p in d.iterdir():
        m = _PETSC_CHECKPOINT_RE.match(p.name)
        if not m:
            continue
        try:
            if not (p.is_file() and p.stat().st_size > 0):
                continue
        except OSError:
            continue
        step = int(m.group(1))
        if best is None or step > best[0]:
            best = (step, p)
    return best[1] if best else None


def _binary_viewer(path: Path) -> Any:
    """A petsc4py binary viewer writing to *path* (lazy petsc4py import)."""
    from petsc4py import PETSc  # noqa: PLC0415 — only on solver envs

    return PETSc.Viewer().createBinary(str(path), mode="w")


def make_checkpoint_monitor(
    *,
    strategy: str = "walltime_margin",
    margin_min: float = 10.0,
    interval_min: float = 30.0,
    result_dir: str | os.PathLike[str] | None = None,
    _viewer_factory: Callable[[Path], Any] | None = None,
) -> Callable[..., None]:
    """A petsc4py monitor callback that checkpoints on the framework cadence.

    The direct-instrumentation half of the adapter — the analog of handing a
    plain loop to :func:`~hpc_agent.experiment_kit.checkpoint.run_iterations`,
    for a loop PETSc owns. Attach it before solving::

        ts.setMonitor(make_checkpoint_monitor())          # TS
        snes.setMonitor(make_checkpoint_monitor())        # SNES

    Each invocation asks :func:`should_checkpoint` (same *strategy* /
    *margin_min* / *interval_min* semantics as every other executor) and, when
    due, dumps the current solution Vec to ``checkpoint-<step>.petscbin``
    with the write-to-tempfile + ``os.replace`` promote discipline
    ``write_checkpoint`` uses — a reader never sees a half-written dump.

    Works with both monitor signatures PETSc calls: ``(ts, step, time, u)``
    (TS — the Vec is passed) and ``(snes, its, fnorm)`` (SNES — the Vec is
    fetched via ``solver.getSolution()``).

    ``_viewer_factory`` is a test seam; the default lazily imports petsc4py.
    """
    factory = _viewer_factory if _viewer_factory is not None else _binary_viewer

    def monitor(solver: Any, step: int, *args: Any) -> None:
        if not should_checkpoint(
            strategy=strategy, margin_min=margin_min, interval_min=interval_min
        ):
            return
        # TS monitors receive (ts, step, time, u); SNES monitors (snes, its,
        # fnorm) — no Vec argument, so fall back to the solver's solution.
        u = args[1] if len(args) >= 2 else solver.getSolution()
        target = petsc_checkpoint_path(step, result_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            viewer = factory(tmp)
            try:
                u.view(viewer)
            finally:
                viewer.destroy()
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    return monitor
