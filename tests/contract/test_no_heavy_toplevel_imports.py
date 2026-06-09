"""Guard: no heavy data/ML dependency is imported at MODULE LEVEL (#288).

Every `hpc-agent <verb>` invocation builds the CLI parser, which triggers
`register_primitives()` — a `pkgutil.walk_packages` import of *every* module
under `ops/`, `meta/`, `incorporation/`, `state/`, `cli/`, `recovery/`, and
`_kernel/extension/`. If any of those modules (or anything they import at
module load) does a top-level `import pandas` / `numpy` / `pyarrow` / …,
EVERY verb — including submit-side ones that never touch a dataframe — pays
that dependency's import cost on startup.

The #288 audit found the hot path already clean: the heavy data deps are
either absent or imported function-locally (the lone `pyarrow.parquet` in
`ops/validate/input_dataset.py` is inside the function that uses it). This
test locks that in: a future top-level `import pandas` in a CLI-reachable
module re-grows the per-verb startup tax and trips here, pointing the author
at the function-local fix.

Scope note: `execution/mapreduce/templates/` is excluded — those are scaffold
files emitted into *user* projects (where importing numpy/torch at the top
is correct), not modules the framework imports.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import hpc_agent

# Root module names that must never be imported at module load by a
# CLI-reachable hpc_agent module. Each is a multi-hundred-millisecond
# import (native extensions, large package init) that submit/monitor-side
# verbs do not need — they belong inside the function that uses them.
# The solver/MPI roots are additionally the libraries whose *knowledge*
# lives in core (solver adapters, axis matchers): core may know their
# idioms, but importing them eagerly would also break the rule that core
# CI runs without them (docs/internals/engineering-principles.md).
_BANNED_ROOTS = frozenset(
    {
        "pandas",
        "numpy",
        "scipy",
        "sklearn",
        "pyarrow",
        "torch",
        "matplotlib",
        "tensorflow",
        "jax",
        "polars",
        "petsc4py",
        "mpi4py",
    }
)

_PKG_ROOT = Path(hpc_agent.__file__).resolve().parent
# Scaffold templates are emitted into user projects, not imported by the
# framework — a top-level `import numpy` there is correct, not a tax.
_EXCLUDED_DIRS = ("execution/mapreduce/templates/",)


def _module_level_import_roots(tree: ast.Module) -> set[str]:
    """Root names of imports that execute at MODULE LOAD (not inside a def).

    Imports nested in a `def`/`async def` are lazy (paid only when that
    function runs) and are exempt. Imports at module scope — including
    inside a module-level `try:` / `if TYPE_CHECKING:` — execute on import
    and are checked. (A `TYPE_CHECKING` guard would still be flagged; none
    of the banned roots are used as type-only imports today, and if one
    were, the fix is the standard string-annotation + lazy-import pattern.)
    """
    roots: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._fn_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._fn_depth += 1
            self.generic_visit(node)
            self._fn_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Import(self, node: ast.Import) -> None:
            if self._fn_depth == 0:
                roots.update(alias.name.split(".")[0] for alias in node.names)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            # `node.level == 0` skips relative imports (`from . import x`),
            # which can never name a third-party root.
            if self._fn_depth == 0 and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
            self.generic_visit(node)

    _Visitor().visit(tree)
    return roots


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(_PKG_ROOT).as_posix()
    return any(rel.startswith(d) for d in _EXCLUDED_DIRS)


def test_no_heavy_dependency_imported_at_module_level() -> None:
    offenders: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        if _is_excluded(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        banned = _module_level_import_roots(tree) & _BANNED_ROOTS
        if banned:
            rel = path.relative_to(_PKG_ROOT).as_posix()
            offenders.append(f"{rel}: {sorted(banned)}")

    assert not offenders, (
        "Heavy dependency imported at module level (paid on every `hpc-agent` "
        "verb's startup — #288). Move the import inside the function that uses "
        "it (the `pyarrow.parquet` pattern in ops/validate/input_dataset.py):\n  "
        + "\n  ".join(offenders)
    )


def test_core_dependencies_exclude_heavy_libraries() -> None:
    """Core's dependency set must never grow a banned root — knowledge of a
    library may live in core (AST patterns, golden bytes), the library itself
    may not (docs/internals/engineering-principles.md: core CI verifies
    library knowledge WITHOUT the library installed; if correctness needs the
    real library, the knowledge belongs in a plugin whose CI carries it).

    Checks ``[project.dependencies]`` and every ``[project.optional-dependencies]``
    extra: the dev/test extras are what core CI installs, and a hypothetical
    ``solver`` extra here would still put the dependency in core's pyproject —
    a plugin ships its own.
    """
    import tomllib

    from tests._paths import REPO_ROOT

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]

    declared = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)

    def dist_name(requirement: str) -> str:
        # PEP 503 normalization of the name part of a PEP 508 requirement.
        name = re.match(r"[A-Za-z0-9._-]+", requirement.strip())
        assert name is not None, f"unparseable requirement: {requirement!r}"
        return re.sub(r"[-_.]+", "-", name.group()).lower()

    # Import roots and dist names coincide for every banned root except
    # sklearn, whose distribution is scikit-learn.
    banned_dists = (_BANNED_ROOTS - {"sklearn"}) | {"scikit-learn"}
    offenders = sorted(d for d in map(dist_name, declared) if d in banned_dists)
    assert not offenders, (
        "Banned library in core pyproject dependencies — core encodes library "
        "*knowledge* via crafted fixtures and must verify it without the library "
        f"installed; ship a plugin instead: {offenders}"
    )
