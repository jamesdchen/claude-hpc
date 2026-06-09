"""CI lint: library-knowledge packages may be imported only at declared assembly points.

Enforces question 2 of CLAUDE.md's four-question boundary test ("core
dispatches, never branches"): modules that encode knowledge of a specific
third-party library — solver adapters, axis-matcher pattern modules — are
*implementations behind a core-owned seam*. Core code reaches them only
through the seam's dispatcher/registry; the seam's wiring lives at a small
number of **declared assembly points**, enumerated below.

Without this lint the boundary erodes silently: each new feature that
imports ``solver_adapters.petsc`` directly adds another core location that
must change when adapter #2 arrives (and another place experiment-blind
library knowledge leaks into general control flow). With it, adding an
assembly point is a *reviewed edit to this file* — a conscious boundary
decision with a diff — rather than an incidental import.

Mechanics: AST-scan every ``.py`` under ``src/hpc_agent``; any absolute
import of a knowledge package (the package root or any submodule) from a
file that is neither inside that package nor in its assembly-point list is
a violation. The package ROOT counts too — its re-exports are library-named
(``detect_petsc_solver``), so importing the root is the same boundary
crossing. Tests are exempt (they may exercise anything directly).

List hygiene is enforced: a declared assembly point that no longer exists,
or that no longer imports its package, fails the lint — stale entries get
cleaned, so the list stays an accurate map of where the boundary is wired.

Same scan/report shape as ``lint_subject_imports.py``: every violation
surfaces a ``path:lineno: <message>`` line and the script exits 1.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src" / "hpc_agent"

# Each knowledge package: where its implementation lives (dir relative to the
# scan root) and the declared assembly points (files relative to the scan
# root) allowed to import it. Everything else goes through the seam named in
# ``seam`` — the library-agnostic surface core code should call instead.
KNOWLEDGE_PACKAGES: dict[str, dict[str, object]] = {
    "hpc_agent.experiment_kit.solver_adapters": {
        "package_dir": "experiment_kit/solver_adapters",
        "seam": "experiment_kit.checkpoint_formats (formats) / the adapter registry-to-be",
        "assembly_points": (
            # The checkpoint-format registry — names each format's adapter.
            "experiment_kit/checkpoint_formats.py",
            # Materializes the solver-instrumented wrapper (entry_point.solver).
            "incorporation/wrap_entry_point.py",
            # Surfaces per-candidate solver detection on the scan output.
            "ops/detect_entry_point.py",
        ),
    },
    "hpc_agent.experiment_kit.axis_matcher.matchers": {
        "package_dir": "experiment_kit/axis_matcher/matchers",
        "seam": "experiment_kit.axis_matcher (the classifier dispatcher)",
        "assembly_points": (
            # The pattern-priority dispatcher — the one importer of matchers.
            "experiment_kit/axis_matcher/_classifier.py",
        ),
    },
}


def _iter_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, module)`` for every absolute import, including ones inside
    functions (lazy imports cross the boundary just the same)."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — stays inside its own package; a file
                # already inside a knowledge package is allowed anyway.
                continue
            if node.module:
                out.append((node.lineno, node.module))
    return out


def _imports_package(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def lint_file(path: Path, scan_root: Path) -> list[tuple[int, str]]:
    """``(lineno, message)`` per knowledge-package import violation in *path*."""
    rel = path.resolve().relative_to(scan_root.resolve()).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    findings: list[tuple[int, str]] = []
    for lineno, module in _iter_imports(tree):
        for package, spec in KNOWLEDGE_PACKAGES.items():
            if not _imports_package(module, package):
                continue
            package_dir = str(spec["package_dir"])
            assembly = spec["assembly_points"]
            if rel.startswith(package_dir + "/") or rel in assembly:
                continue
            findings.append(
                (
                    lineno,
                    f"library-knowledge import: {rel} imports {module}, but is not a "
                    f"declared assembly point for {package}. Route through the seam "
                    f"({spec['seam']}) — or, if this file IS a new assembly point, add "
                    f"it to KNOWLEDGE_PACKAGES in scripts/lint_library_knowledge.py "
                    f"(a reviewed boundary decision).",
                )
            )
    findings.sort(key=lambda f: f[0])
    return findings


def lint_assembly_point_hygiene(scan_root: Path) -> list[str]:
    """Stale-entry guard: every declared assembly point must exist AND still
    import its package — otherwise the list drifts from reality and stops
    being a map of where the boundary is wired."""
    problems: list[str] = []
    for package, spec in KNOWLEDGE_PACKAGES.items():
        for rel in spec["assembly_points"]:  # type: ignore[union-attr]
            path = scan_root / str(rel)
            if not path.is_file():
                problems.append(
                    f"{rel}: declared as an assembly point for {package} but does not "
                    "exist — remove the stale entry"
                )
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                problems.append(f"{rel}: declared assembly point is unparseable")
                continue
            if not any(_imports_package(module, package) for _, module in _iter_imports(tree)):
                problems.append(
                    f"{rel}: declared as an assembly point for {package} but no longer "
                    "imports it — remove the stale entry"
                )
    return problems


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for problem in lint_assembly_point_hygiene(root):
        print(f"{root}: {problem}", file=sys.stderr)
        failures += 1
    for py in sorted(root.rglob("*.py")):
        if not py.is_file():
            continue
        for lineno, message in lint_file(py, root):
            print(f"{py}:{lineno}: {message}", file=sys.stderr)
            failures += 1
    if failures:
        print(f"lint_library_knowledge: {failures} violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
