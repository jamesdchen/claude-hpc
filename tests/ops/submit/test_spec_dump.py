"""Unit tests for the pre-submit spec verification dump (#212).

Covers the two helpers in ``hpc_agent.ops.submit.spec_dump``: where the
designated folder resolves (default journal home vs ``HPC_SPEC_DUMP_DIR``
override) and that ``write_spec_dump`` lands the verbatim spec under a
filesystem-safe ``<run_id>.json`` name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.ops.submit.spec_dump import spec_dump_dir, write_spec_dump

_SPEC: dict = {
    "profile": "ml",
    "cluster": "hoffman2",
    "ssh_target": "user@host",
    "remote_path": "/u/scratch/exp",
    "job_name": "ml",
    "run_id": "ml-20260605-153012-abcd1234",
    "total_tasks": 6,
    "backend": "sge",
    "script": "run.sh",
    "job_env": {"EXECUTOR": "python run.py"},
}


@pytest.fixture
def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the journal home so the default dump dir lands in tmp."""
    monkeypatch.delenv("HPC_SPEC_DUMP_DIR", raising=False)
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def test_default_dir_is_under_journal_not_experiment_tree(
    _journal_home: Path, tmp_path: Path
) -> None:
    """Default dump dir is the per-repo journal home's submit_specs/, NOT
    inside experiment_dir (so it never ships via rsync)."""
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    d = spec_dump_dir(experiment_dir)
    assert d.name == "submit_specs"
    assert d.is_dir()
    # Must not be nested inside the experiment tree (the rsync source).
    assert experiment_dir.resolve() not in d.resolve().parents
    assert (tmp_path / "journal").resolve() in d.resolve().parents


def test_env_override_redirects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HPC_SPEC_DUMP_DIR wins and is created on demand."""
    override = tmp_path / "review_here"
    monkeypatch.setenv("HPC_SPEC_DUMP_DIR", str(override))
    d = spec_dump_dir(tmp_path / "exp")
    assert d.resolve() == override.resolve()
    assert d.is_dir()


def test_blank_env_override_falls_back_to_default(
    _journal_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank/whitespace override is treated as unset (mirrors the journal
    env precedence), not as a literal empty path."""
    monkeypatch.setenv("HPC_SPEC_DUMP_DIR", "   ")
    d = spec_dump_dir(tmp_path / "exp")
    assert d.name == "submit_specs"


def test_write_dumps_verbatim_spec_under_run_id(_journal_home: Path, tmp_path: Path) -> None:
    """The file is named after the run_id and contains the spec verbatim."""
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    path = write_spec_dump(experiment_dir, run_id=_SPEC["run_id"], spec=_SPEC)
    assert path.name == f"{_SPEC['run_id']}.json"
    assert json.loads(path.read_text(encoding="utf-8")) == _SPEC


def test_write_is_pretty_and_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Human reviewers get indented, sorted output (stable diffs)."""
    monkeypatch.setenv("HPC_SPEC_DUMP_DIR", str(tmp_path / "d"))
    path = write_spec_dump(tmp_path / "exp", run_id="r1", spec={"b": 1, "a": 2})
    text = path.read_text(encoding="utf-8")
    assert text == '{\n  "a": 2,\n  "b": 1\n}'


def test_write_rejects_path_traversal_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run_id with a path separator can't escape the dump folder."""
    monkeypatch.setenv("HPC_SPEC_DUMP_DIR", str(tmp_path / "d"))
    with pytest.raises(errors.SpecInvalid):
        write_spec_dump(tmp_path / "exp", run_id="../evil", spec=_SPEC)


def test_write_is_a_copy_not_a_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutating the caller's dict after the dump doesn't change the file."""
    monkeypatch.setenv("HPC_SPEC_DUMP_DIR", str(tmp_path / "d"))
    spec = dict(_SPEC)
    path = write_spec_dump(tmp_path / "exp", run_id="r2", spec=spec)
    spec["total_tasks"] = 999
    assert json.loads(path.read_text(encoding="utf-8"))["total_tasks"] == 6
