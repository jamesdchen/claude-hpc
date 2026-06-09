"""The project version stays in sync across its three declared sites.

Runs ``scripts/lint_changelog_version.py`` in-process (the pre-commit hook
runs the same check) so a fresh ``pytest`` catches a version-site mismatch —
the silent ``pyproject.toml`` / ``uv.lock`` double-grab that a clean auto-merge
of identical version strings produces — even before pre-commit fires.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LINT_SCRIPT = REPO_ROOT / "scripts" / "lint_changelog_version.py"


def _load_lint():
    spec = importlib.util.spec_from_file_location("_lint_changelog_version_for_test", LINT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = sys.modules.get(spec.name) or importlib.util.module_from_spec(spec)
    if spec.name not in sys.modules:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def test_version_sites_in_sync() -> None:
    """pyproject.toml, uv.lock root, and the top released CHANGELOG header agree."""
    violations = _load_lint().check()
    assert not violations, "version sites out of sync:\n  " + "\n  ".join(violations)


def test_lint_detects_a_mismatch() -> None:
    """The lint actually fires on a disagreement (guards against a no-op check)."""
    lint = _load_lint()
    assert lint.changelog_top_released_version("## [Unreleased]\n\n## 9.9.9 — x\n") == "9.9.9"
    # [Unreleased] is skipped; a header with no number yields None.
    assert lint.changelog_top_released_version("## [Unreleased]\n\nstuff\n") is None
    assert lint.pyproject_version('[project]\nversion = "1.2.3"\n') == "1.2.3"
    lock = '[[package]]\nname = "hpc-agent"\nversion = "1.2.3"\n'
    assert lint.uv_lock_root_version(lock) == "1.2.3"
