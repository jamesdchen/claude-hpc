"""CLI dry-run contract for ``submit-flow`` / ``submit-flow-batch`` (#212).

``--dry-run`` must write the resolved spec(s) to the designated verification
folder and report the path(s) on the envelope, all without touching the
cluster (the journal/dump folder are local-only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ._helpers import parse_envelope as _parse_envelope
from ._helpers import run_cli as _run_cli


def _spec(run_id: str = "ml-20260605-153012-abcd1234", **overrides: object) -> dict:
    base: dict = {
        "profile": "ml",
        "cluster": "hoffman2",
        "ssh_target": "user@hoffman2.idre.ucla.edu",
        "remote_path": "/u/scratch/exp",
        "job_name": "ml",
        "run_id": run_id,
        "total_tasks": 6,
        "backend": "sge",
        "script": "run.sh",
        "job_env": {"EXECUTOR": "python run.py"},
    }
    base.update(overrides)
    return base


def _env(journal: Path, dump: Path | None = None) -> dict[str, str]:
    env = {"HPC_JOURNAL_DIR": str(journal), "PATH": os.environ.get("PATH", "")}
    if dump is not None:
        env["HPC_SPEC_DUMP_DIR"] = str(dump)
    # Windows needs these to spawn a Python subprocess; no-op on POSIX.
    for _var in ("SystemRoot", "COMSPEC", "USERPROFILE"):
        _val = os.environ.get(_var)
        if _val is not None:
            env[_var] = _val
    return env


def test_submit_flow_dry_run_writes_spec_dump(tmp_path: Path) -> None:
    spec_file = tmp_path / "spec.json"
    spec_payload = _spec()
    spec_file.write_text(json.dumps(spec_payload))
    dump = tmp_path / "dump"

    rc, out, _ = _run_cli(
        "submit-flow",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec_file),
        "--dry-run",
        env=_env(tmp_path / "journal", dump),
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    assert env_resp["ok"] is True
    data = env_resp["data"]
    assert data["dry_run"] is True
    assert data["would_launch"] == 6

    dump_path = Path(data["spec_dump_path"])
    assert dump_path.parent.resolve() == dump.resolve()
    assert dump_path.name == f"{spec_payload['run_id']}.json"
    # The dumped file is the spec verbatim — re-submittable as-is.
    assert json.loads(dump_path.read_text(encoding="utf-8")) == spec_payload


def test_submit_flow_batch_dry_run_writes_one_dump_per_spec(tmp_path: Path) -> None:
    batch = {
        "specs": [
            _spec(run_id="run-aaaa-1111"),
            _spec(run_id="run-bbbb-2222", job_name="ml2"),
        ]
    }
    spec_file = tmp_path / "batch.json"
    spec_file.write_text(json.dumps(batch))
    dump = tmp_path / "dump"

    rc, out, _ = _run_cli(
        "submit-flow",  # auto-dispatches to batch on a {specs: [...]} shape
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec_file),
        "--dry-run",
        env=_env(tmp_path / "journal", dump),
    )
    assert rc == 0
    data = _parse_envelope(out)["data"]
    assert data["dry_run"] is True
    assert data["n_specs"] == 2

    paths = [Path(p) for p in data["spec_dump_paths"]]
    assert [p.name for p in paths] == ["run-aaaa-1111.json", "run-bbbb-2222.json"]
    for p, entry in zip(paths, batch["specs"], strict=True):
        assert json.loads(p.read_text(encoding="utf-8")) == entry


def test_submit_flow_dry_run_defaults_dump_under_journal(tmp_path: Path) -> None:
    """With no HPC_SPEC_DUMP_DIR, the dump lands under the journal home's
    submit_specs/ — never inside the experiment dir (the rsync source)."""
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(_spec()))
    journal = tmp_path / "journal"

    rc, out, _ = _run_cli(
        "submit-flow",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec_file),
        "--dry-run",
        env=_env(journal),
    )
    assert rc == 0
    dump_path = Path(_parse_envelope(out)["data"]["spec_dump_path"])
    assert dump_path.parent.name == "submit_specs"
    assert journal.resolve() in dump_path.resolve().parents
    # Not under the experiment's deploy tree (.hpc) — it must not ship via rsync.
    assert (tmp_path / ".hpc").resolve() not in dump_path.resolve().parents
