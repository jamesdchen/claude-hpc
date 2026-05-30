"""Byte-for-byte golden tests for ``render_script``.

Agent A (TEST safety net) — asserts that the new
``render_script(PROFILE, kind=...)`` reproduces the CURRENT runtime
array template files exactly.

The golden bytes are read from ``git show HEAD:<path>`` rather than the
working tree, because another agent is concurrently DELETING those
files as part of the profile migration. Pinning to HEAD keeps the
golden stable regardless of the working-tree state.

If/when the templates relocate or are deleted at HEAD, update
``_GOLDEN_PATHS`` accordingly — the bytes are the contract, not the
location.
"""

from __future__ import annotations

import subprocess

import pytest

_REPO_TEMPLATE_ROOT = "src/hpc_agent/models/mapreduce/templates/runtime"

# (profile_const_name, kind, git-tracked path at HEAD)
_GOLDEN_PATHS = [
    ("SLURM_PROFILE", "cpu", f"{_REPO_TEMPLATE_ROOT}/slurm/cpu_array.slurm"),
    ("SLURM_PROFILE", "gpu", f"{_REPO_TEMPLATE_ROOT}/slurm/gpu_array.slurm"),
    ("SGE_PROFILE", "cpu", f"{_REPO_TEMPLATE_ROOT}/sge/cpu_array.sh"),
    ("SGE_PROFILE", "gpu", f"{_REPO_TEMPLATE_ROOT}/sge/gpu_array.sh"),
]


def _golden_bytes(path: str) -> bytes:
    """Read the file content at HEAD (not the working tree)."""
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


def test_golden_paths_exist_at_head():
    """Sanity: every golden path must still be tracked at HEAD, else the
    byte-match cases below are silently vacuous."""
    for _name, _kind, path in _GOLDEN_PATHS:
        out = _golden_bytes(path)
        assert out, f"golden template empty/missing at HEAD: {path}"


@pytest.mark.parametrize(
    ("profile_name", "kind", "path"),
    _GOLDEN_PATHS,
    ids=[f"{n}-{k}" for n, k, _ in _GOLDEN_PATHS],
)
def test_render_script_matches_golden(profile_name, kind, path):
    """render_script(PROFILE, kind=...) must equal the golden file byte-for-byte.

    Imports the NEW profile module — will ERROR until the spine lands it.
    """
    from hpc_agent.infra.backends import profile as profile_mod

    prof = getattr(profile_mod, profile_name)
    rendered = profile_mod.render_script(prof, kind=kind)
    assert isinstance(rendered, str), "render_script must return a str"

    golden = _golden_bytes(path)
    # Compare as bytes via UTF-8 so any trailing-newline / encoding drift
    # is caught.
    assert rendered.encode("utf-8") == golden, (
        f"render_script({profile_name}, kind={kind!r}) diverged from {path}"
    )


def test_profile_backend_render_script_classmethod_matches_golden():
    """The engine also exposes render_script as a classmethod reading
    cls.profile. ERRORs until the spine lands ProfileBackend."""
    from hpc_agent.infra.backends import get_backend_class

    cls = get_backend_class("slurm")
    rendered = cls.render_script(kind="cpu")
    golden = _golden_bytes(f"{_REPO_TEMPLATE_ROOT}/slurm/cpu_array.slurm")
    assert rendered.encode("utf-8") == golden
