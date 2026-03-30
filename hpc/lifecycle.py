"""Job lifecycle tracking: event logging, status querying, result checking.

Works with any project that has:
- A result directory with result files matching a configurable glob pattern
- Job IDs from a scheduler (SGE or SLURM)
- A lifecycle.jsonl event log

Event types: submit, resubmit, complete, fail (extensible via log_event).
"""

from __future__ import annotations

__all__ = [
    "log_event",
    "check_results",
    "query_sacct",
    "query_sge",
    "detect_scheduler",
    "report_status",
    "get_err_log_paths",
]

import glob
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event logging
# ---------------------------------------------------------------------------


def log_event(audit_path: str | Path, action: str, **details) -> None:
    """Append a JSON-lines event to the lifecycle audit trail.

    Parameters
    ----------
    audit_path : path to lifecycle.jsonl
    action : event name (e.g. "submit", "resubmit", "complete", "fail")
    **details : arbitrary key-value pairs stored alongside the event
    """
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        **details,
    }
    try:
        Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        logger.warning("Failed to write audit log: %s", exc)


def read_events(audit_path: str | Path) -> list[dict]:
    """Read all events from a lifecycle.jsonl file."""
    events: list[dict] = []
    path = Path(audit_path)
    if not path.exists():
        return events
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


# ---------------------------------------------------------------------------
# Result checking
# ---------------------------------------------------------------------------

_DEFAULT_RESULT_RE = re.compile(r"(?:results_)?chunk_(\d+)\.csv$", re.IGNORECASE)


def _extract_chunk_id(filename: str, pattern: re.Pattern | None = None) -> int | None:
    """Return the integer chunk id embedded in *filename*, or None."""
    pat = pattern or _DEFAULT_RESULT_RE
    m = pat.search(filename)
    return int(m.group(1)) if m else None


def check_results(
    result_dir: str | Path,
    total_chunks: int,
    file_glob: str = "*chunk_*.csv",
    chunk_pattern: re.Pattern | None = None,
    validate: bool = True,
) -> dict[int, dict]:
    """Scan *result_dir* for completed result files.

    Parameters
    ----------
    result_dir : directory to scan
    total_chunks : expected number of chunks (IDs 1..total_chunks)
    file_glob : glob pattern for result files
    chunk_pattern : regex with group(1) capturing the chunk ID integer.
        Defaults to matching ``chunk_<N>.csv`` or ``results_chunk_<N>.csv``.
    validate : if True and files are CSVs, check for header + >=1 data row
    """
    import csv

    results: dict[int, dict] = {}
    rdir = Path(result_dir).resolve()

    for path_str in glob.glob(str(rdir / file_glob)):
        chunk_id = _extract_chunk_id(os.path.basename(path_str), chunk_pattern)
        if chunk_id is None or chunk_id < 1 or chunk_id > total_chunks:
            continue

        if validate and path_str.endswith(".csv"):
            try:
                with open(path_str, newline="") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if header is None:
                        continue
                    row_count = sum(1 for _ in reader)
                    if row_count < 1:
                        continue
                results[chunk_id] = {"status": "complete", "csv_rows": row_count}
            except OSError:
                continue
        else:
            results[chunk_id] = {"status": "complete", "path": path_str}

    return results


# ---------------------------------------------------------------------------
# Scheduler queries
# ---------------------------------------------------------------------------


def query_sacct(job_ids: list[str], cluster: str | None = None) -> dict:
    """Query SLURM sacct for array task states.

    Returns {task_id: {state, exit_code, job_id}} or {"error": ...}.
    """
    task_info: dict[int, dict] = {}

    for job_id in job_ids:
        cmd = ["sacct", "-j", job_id, "--format=JobID,State,ExitCode", "--noheader", "--parsable2"]
        if cluster:
            cmd.insert(1, f"--clusters={cluster}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {"error": "sacct_unavailable"}

        if result.returncode != 0 or not result.stdout.strip():
            if not task_info:
                return {"error": "sacct_unavailable"}
            continue

        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            job_field, state, exit_code = parts[0], parts[1], parts[2]
            if "_" not in job_field:
                continue
            try:
                tid = int(job_field.split("_")[1])
            except (IndexError, ValueError):
                continue
            task_info[tid] = {"state": state, "exit_code": exit_code, "job_id": job_id}

    if not task_info:
        return {"error": "sacct_unavailable"}
    return task_info


# SGE state code -> normalized state
_SGE_STATE_MAP: dict[str, str] = {
    "r": "RUNNING",
    "t": "RUNNING",
    "Rr": "RUNNING",
    "Rt": "RUNNING",
    "qw": "PENDING",
    "hqw": "PENDING",
    "Eqw": "FAILED",
    "Ehqw": "FAILED",
    "dr": "CANCELLED",
    "dt": "CANCELLED",
    "dRr": "CANCELLED",
    "dRt": "CANCELLED",
    "ds": "CANCELLED",
    "dS": "CANCELLED",
    "dT": "CANCELLED",
}


def _expand_task_range(spec: str) -> list[int]:
    """Expand an SGE task range like '3-10:1' or '5' into a list of ints."""
    spec = spec.strip()
    if not spec or spec == "undefined":
        return []
    m = re.match(r"(\d+)(?:-(\d+)(?::(\d+))?)?", spec)
    if not m:
        return []
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    step = int(m.group(3)) if m.group(3) else 1
    return list(range(start, end + 1, step))


def _process_qacct_block(
    block: dict[str, str],
    job_id: str,
    task_info: dict[int, dict],
) -> None:
    """Extract task status from a single qacct block."""
    tid_str = block.get("taskid", "")
    if not tid_str or tid_str == "undefined":
        return
    try:
        tid = int(tid_str)
    except ValueError:
        return
    if tid in task_info:
        return  # qstat data takes precedence

    exit_status = block.get("exit_status", "0")
    failed = block.get("failed", "0")
    try:
        exit_int = int(exit_status)
        failed_int = int(failed.split()[0]) if failed else 0
    except ValueError:
        exit_int, failed_int = -1, -1

    if exit_int == 0 and failed_int == 0:
        state = "COMPLETED"
    elif failed_int == 100:
        state = "TIMEOUT"
    elif failed_int != 0:
        state = "NODE_FAIL"
    else:
        state = "FAILED"

    task_info[tid] = {"state": state, "exit_code": exit_status, "job_id": job_id}


def query_sge(job_ids: list[str], user: str | None = None) -> dict:
    """Query SGE via qstat + qacct for array task states.

    Returns {task_id: {state, exit_code, job_id}} or {"error": ...}.
    """
    task_info: dict[int, dict] = {}
    sge_user = user or os.environ.get("USER", os.environ.get("USERNAME", ""))

    # Phase 1: qstat for running/pending tasks
    try:
        result = subprocess.run(
            ["qstat", "-u", sge_user],
            capture_output=True,
            text=True,
            timeout=30,
        )
        qstat_out = result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        qstat_out = ""

    job_id_set = set(job_ids)
    for line in qstat_out.strip().splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        jid = cols[0].strip()
        if jid not in job_id_set:
            continue
        state_code = cols[4].strip()
        normalized = _SGE_STATE_MAP.get(state_code, "UNKNOWN")
        task_spec = cols[-1].strip() if len(cols) >= 9 else ""
        for tid in _expand_task_range(task_spec):
            task_info[tid] = {"state": normalized, "exit_code": None, "job_id": jid}

    # Phase 2: qacct for finished tasks
    for job_id in job_ids:
        try:
            result = subprocess.run(
                ["qacct", "-j", job_id],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if result.returncode != 0:
            continue

        current: dict[str, str] = {}
        for raw_line in result.stdout.splitlines():
            if raw_line.startswith("====="):
                if current:
                    _process_qacct_block(current, job_id, task_info)
                    current = {}
                continue
            parts = raw_line.split(None, 1)
            if len(parts) == 2:
                current[parts[0]] = parts[1].strip()
        if current:
            _process_qacct_block(current, job_id, task_info)

    if not task_info:
        return {"error": "sge_unavailable"}
    return task_info


# ---------------------------------------------------------------------------
# Scheduler detection
# ---------------------------------------------------------------------------


def detect_scheduler(result_dir: str | Path | None = None) -> str:
    """Auto-detect scheduler type.

    Checks (in order):
    1. experiment_meta.json in result_dir (if provided)
    2. Probe for sacct (SLURM)
    3. Fall back to "sge"
    """
    if result_dir is not None:
        meta_path = Path(result_dir) / "experiment_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                backend = meta.get("backend", "")
                if "sge" in backend:
                    return "sge"
                if "slurm" in backend:
                    return "slurm"
            except (json.JSONDecodeError, OSError):
                pass
    try:
        result = subprocess.run(["sacct", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return "slurm"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "sge"


# ---------------------------------------------------------------------------
# Error log paths
# ---------------------------------------------------------------------------


def get_err_log_paths(
    job_ids: list[str],
    total_chunks: int,
    scheduler: str = "slurm",
    log_dir: str = "",
    job_name: str = "",
    scratch_dir: str = "",
) -> dict[int, str]:
    """Find the most recent error log path on disk for each chunk.

    Parameters
    ----------
    log_dir : directory for SLURM logs (e.g. /path/to/logs)
    scratch_dir : directory for SGE logs (e.g. $SCRATCH)
    """
    paths: dict[int, str] = {}
    for tid in range(1, total_chunks + 1):
        for job_id in reversed(job_ids):
            if scheduler == "sge":
                p = os.path.join(scratch_dir, f"{job_name}.o{job_id}.{tid}")
            else:
                p = os.path.join(log_dir, f"slurm-{job_id}_{tid}.err")
            if os.path.isfile(p):
                paths[tid] = p
                break
    return paths


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

_ACTIVE_STATES = {"RUNNING", "REQUEUED", "CONFIGURING"}
_PENDING_STATES = {"PENDING"}
_FAILED_STATES = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"}


def report_status(
    result_dir: str | Path,
    job_ids: list[str],
    total_chunks: int,
    scheduler: str | None = None,
    *,
    file_glob: str = "*chunk_*.csv",
    chunk_pattern: re.Pattern | None = None,
    log_dir: str = "",
    scratch_dir: str = "",
    job_name: str = "",
    slurm_cluster: str | None = None,
    sge_user: str | None = None,
) -> dict:
    """Assemble a full JSON status report.

    Parameters
    ----------
    result_dir : directory containing result files
    job_ids : scheduler job IDs to query
    total_chunks : expected number of chunks
    scheduler : "slurm" or "sge" (auto-detected if None)
    file_glob, chunk_pattern : forwarded to check_results
    log_dir, scratch_dir, job_name : forwarded to get_err_log_paths
    slurm_cluster : --clusters flag for sacct
    sge_user : user for qstat -u
    """
    csv_results = check_results(
        result_dir, total_chunks, file_glob=file_glob, chunk_pattern=chunk_pattern
    )

    if scheduler is None:
        scheduler = detect_scheduler(result_dir)

    if job_ids:
        if scheduler == "sge":
            job_info = query_sge(job_ids, user=sge_user)
        else:
            job_info = query_sacct(job_ids, cluster=slurm_cluster)
    else:
        job_info = {}
    query_error = job_info.pop("error", None)

    complete_ids = set(csv_results)
    chunks: dict[str, dict] = {}
    summary = {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}

    for tid in range(1, total_chunks + 1):
        if tid in complete_ids:
            chunks[str(tid)] = csv_results[tid]
            summary["complete"] += 1
        elif tid in job_info:
            info = job_info[tid]
            state = info["state"]
            if state in _ACTIVE_STATES:
                cat = "running"
            elif state in _PENDING_STATES:
                cat = "pending"
            elif state in _FAILED_STATES or state.startswith("CANCELLED"):
                cat = "failed"
            else:
                cat = "unknown"
            chunks[str(tid)] = {"status": cat, **info}
            summary[cat] += 1
        else:
            chunks[str(tid)] = {"status": "unknown"}
            summary["unknown"] += 1

    # Error log paths for non-complete chunks
    failed_or_unknown = [tid for tid in range(1, total_chunks + 1) if tid not in complete_ids]
    all_err = (
        get_err_log_paths(
            job_ids,
            total_chunks,
            scheduler=scheduler,
            log_dir=log_dir,
            scratch_dir=scratch_dir,
            job_name=job_name,
        )
        if job_ids
        else {}
    )
    err_paths = {str(tid): all_err[tid] for tid in failed_or_unknown if tid in all_err}

    report: dict = {
        "result_dir": str(Path(result_dir).resolve()),
        "total_chunks": total_chunks,
        "scheduler": scheduler,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "chunks": chunks,
        "summary": summary,
    }
    if err_paths:
        report["err_log_paths"] = err_paths
    if query_error:
        report["query_error"] = query_error
    return report
