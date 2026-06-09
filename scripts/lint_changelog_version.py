"""CI lint: the project version stays in sync across the three places it lives.

The version churn problem: every change used to bump ``version`` in
``pyproject.toml`` AND in ``uv.lock`` AND add a dated ``## X.Y.Z`` CHANGELOG
header. Parallel branches then collide on the next number — and the nastiest
case is silent: when two branches pick the *same* number, ``pyproject.toml`` /
``uv.lock`` auto-merge cleanly to a wrong result (two releases sharing a
version) while only CHANGELOG conflicts.

The fix is the ``[Unreleased]`` convention: entries accumulate under
``## [Unreleased]`` and the version is bumped only at release time. This lint
enforces the invariant that makes that safe — the three version sites agree:

* ``pyproject.toml`` ``[project].version``
* ``uv.lock`` root package (``name = "hpc-agent"``) ``version``
* the top *released* CHANGELOG header (the first ``## X.Y.Z`` line; a leading
  ``## [Unreleased]`` section is skipped)

A mismatch is exactly the silent double-grab this is meant to catch. Parsed
with ``re`` (not ``tomllib``) so it runs on the project's 3.10 floor.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
UV_LOCK = REPO / "uv.lock"
CHANGELOG = REPO / "CHANGELOG.md"

_ROOT_PKG_NAME = "hpc-agent"
# A semver-ish ``X.Y.Z`` (optionally with a pre-release/build suffix).
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+[\w.+-]*")


def pyproject_version(text: str) -> str | None:
    """The ``[project].version`` string, or ``None`` if absent.

    Anchored to ``version = "..."`` at line start, which uniquely matches the
    project version — sibling keys are ``target-version`` / ``python_version``
    / ``requires-python`` (different prefixes), never a bare ``version =``.
    """
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def uv_lock_root_version(text: str, pkg: str = _ROOT_PKG_NAME) -> str | None:
    """The version of the editable root package in ``uv.lock``.

    Finds the ``[[package]]`` block whose ``name`` is *pkg* and returns the
    ``version`` declared in that same block.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == f'name = "{pkg}"':
            for follow in lines[i + 1 : i + 6]:
                m = re.match(r'\s*version\s*=\s*"([^"]+)"', follow)
                if m:
                    return m.group(1)
            return None
    return None


def changelog_top_released_version(text: str) -> str | None:
    """The first ``## X.Y.Z`` header, skipping a leading ``## [Unreleased]``."""
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        m = _VERSION_RE.search(line)
        if m:  # a numbered release header (## [Unreleased] has no digits)
            return m.group(0)
    return None


def check() -> list[str]:
    """Return a list of violation strings (empty when in sync)."""
    violations: list[str] = []
    py = pyproject_version(PYPROJECT.read_text(encoding="utf-8"))
    lock = uv_lock_root_version(UV_LOCK.read_text(encoding="utf-8"))
    changelog = changelog_top_released_version(CHANGELOG.read_text(encoding="utf-8"))

    if py is None:
        violations.append("pyproject.toml: could not find [project].version")
    if lock is None:
        violations.append(f"uv.lock: could not find version for root package {_ROOT_PKG_NAME!r}")
    if changelog is None:
        violations.append("CHANGELOG.md: no released '## X.Y.Z' header found")

    found = {"pyproject.toml": py, "uv.lock": lock, "CHANGELOG.md (top release)": changelog}
    distinct = {v for v in found.values() if v is not None}
    if len(distinct) > 1:
        detail = ", ".join(f"{k}={v!r}" for k, v in found.items())
        violations.append(f"version sites disagree: {detail}")
    return violations


def main() -> int:
    violations = check()
    if not violations:
        return 0
    print("ERROR: project version is out of sync across its declared sites:")
    for v in violations:
        print(f"  {v}")
    print(
        "\nFix: pyproject.toml [project].version, uv.lock root package version, and the top "
        "released '## X.Y.Z' CHANGELOG header must all match. New work goes under "
        "'## [Unreleased]'; bump the version (and promote [Unreleased]) only at release."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
