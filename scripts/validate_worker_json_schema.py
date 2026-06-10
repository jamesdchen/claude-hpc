#!/usr/bin/env python3
"""Live validation for issue #269: `claude -p --json-schema` × the agent loop.

The decode-time worker output constraint (`HPC_AGENT_WORKER_JSON_SCHEMA` →
`--json-schema` with the lenient ``worker.output.json``) ships off by default
because two questions were unanswerable offline (no worker credentials in the
build sandbox):

1. **Composition** — does `--json-schema` constrain only the worker's FINAL
   message, leaving the multi-step `--bare` tool loop (rsync / qsub / canary
   in production) intact?
2. **Schema acceptance** — does the CLI accept the *lenient* WorkerReport
   schema (`additionalProperties: true`, no `required`), or does its
   structured-output mode demand the API-strict variant?

This harness answers both against a real `claude -p` run, exercising the
production spawn path (`_run_claude_worker` — argv assembly, temp-file +
stdin prompt transport, JSON result-envelope unwrap) and the production
schema loader (`_worker_output_schema` with the gate forced on). The worker
is given a deterministic multi-step task with observable side effects:

  * Write a token to ``<workdir>/step1_token.txt``        (tool turn 1)
  * Read it back                                          (tool turn 2)
  * Write the uppercased token to ``<workdir>/step2_echo.txt`` (tool turn 3)
  * Emit a WorkerReport carrying the token as its final message

PASS requires: exit 0, both side-effect files present with the right bytes
(the loop ran — question 1), the final output a schema-valid WorkerReport
(question 2), and the report's token matching the loop's (the constrained
decode reflects work actually done in the loop, not a hallucinated report).
If the CLI rejects the lenient schema the harness retries with the strict
``worker.strict.output.json`` and reports which shape was accepted.

Auth modes (``--mode``, default auto-detect):

  * ``bare``    — production `ClaudeCliInvoker` argv (`--bare`, API key auth).
  * ``ambient`` — same argv minus `--bare`, relying on the calling
    environment's own `claude` login. This is what the production
    `ClaudeCliOAuthInvoker` mode reduces to (it too drops `--bare`); use it
    where credentials are host-managed (e.g. Claude Code remote containers).

Run:  python scripts/validate_worker_json_schema.py [--mode bare|ambient]
Exit: 0 all checks pass, 1 otherwise. Evidence JSON on stdout either way.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from hpc_agent._kernel.lifecycle.invoke import (  # noqa: E402
    _WORKER_ALLOWED_TOOLS,
    _WORKER_DISALLOWED_TOOLS,
    _WORKER_JSON_SCHEMA_ENV,
    _WORKER_MODEL,
    RenderedPrompt,
    _load_schema_resource,
    _run_claude_worker,
    _worker_output_schema,
)

_PROCEDURE = """\
You are a delegated hpc-agent validation worker. Execute the numbered steps
exactly, using your tools; do not ask questions, do not skip steps.

After the steps, your FINAL message must be ONLY a JSON object of this shape
(no prose, no code fences):

{"result": {"validated": true, "token": "<the token you wrote in step 1>",
 "steps_completed": 3}, "decisions": [], "anomalies": ""}

Set "anomalies" to a short description if any step failed; otherwise "".
"""

_TASK_TEMPLATE = """\
Invocation context:
- workdir: {workdir}
- token: {token}

Steps:
1. Write a file {workdir}/step1_token.txt whose entire content is exactly the
   token above (no trailing newline).
2. Read {workdir}/step1_token.txt back to confirm the content.
3. Write a file {workdir}/step2_echo.txt whose entire content is exactly the
   token uppercased (no trailing newline).

Then emit the final JSON report described in your procedure.
"""


def _mode_args(mode: str) -> list[str]:
    args = [] if mode == "ambient" else ["--bare"]
    return [
        *args,
        "--model",
        _WORKER_MODEL,
        "--settings",
        '{"sandbox": {"enabled": false}}',
        "--allowedTools",
        _WORKER_ALLOWED_TOOLS,
        "--disallowedTools",
        _WORKER_DISALLOWED_TOOLS,
    ]


def _detect_mode() -> str:
    return "bare" if os.environ.get("ANTHROPIC_API_KEY") else "ambient"


def _run_once(mode: str, schema: str, schema_name: str) -> dict:
    token = secrets.token_hex(8)
    workdir = Path(tempfile.mkdtemp(prefix="hpc-agent-269-"))
    prompt = RenderedPrompt(
        cacheable_prefix=_PROCEDURE,
        variable_suffix=_TASK_TEMPLATE.format(workdir=workdir, token=token),
    )
    with tempfile.TemporaryDirectory(prefix="hpc-agent-269-cwd-") as cwd:
        result = _run_claude_worker(
            executable="claude",
            mode_args=_mode_args(mode),
            prompt=prompt,
            cwd=cwd,
            output_schema=schema,
        )

    checks: dict[str, bool] = {}
    evidence: dict = {
        "schema": schema_name,
        "mode": mode,
        "exit_code": result.exit_code,
        "workdir": str(workdir),
    }

    checks["exit_zero"] = result.exit_code == 0

    step1 = workdir / "step1_token.txt"
    step2 = workdir / "step2_echo.txt"
    step1_text = step1.read_text(encoding="utf-8").strip() if step1.is_file() else None
    step2_text = step2.read_text(encoding="utf-8").strip() if step2.is_file() else None
    checks["tool_loop_step1"] = step1_text == token
    checks["tool_loop_step2"] = step2_text == token.upper()

    report_obj = None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        report_obj = json.loads(result.output)
    checks["final_message_is_json_object"] = isinstance(report_obj, dict)

    if isinstance(report_obj, dict):
        from hpc_agent._wire.spawn_contract import WorkerReport

        try:
            report = WorkerReport.model_validate(report_obj)
            checks["workerreport_valid"] = True
            checks["token_round_trip"] = report.result.get("token") == token
        except Exception as exc:  # pydantic ValidationError
            checks["workerreport_valid"] = False
            checks["token_round_trip"] = False
            evidence["validation_error"] = str(exc)[:2000]
    else:
        checks["workerreport_valid"] = False
        checks["token_round_trip"] = False

    evidence["checks"] = checks
    evidence["passed"] = all(checks.values())
    evidence["report"] = report_obj if isinstance(report_obj, dict) else None
    if not evidence["passed"]:
        evidence["stdout_tail"] = (result.output or "")[-2000:]
        evidence["stderr_tail"] = (result.stderr or "")[-2000:]
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["bare", "ambient"], default=_detect_mode())
    args = parser.parse_args()

    # Force the gate on so the production loader (_worker_output_schema) is
    # what supplies the minified lenient schema — the exact bytes the flip
    # would put on every worker's argv.
    os.environ[_WORKER_JSON_SCHEMA_ENV] = "1"
    lenient = _worker_output_schema()
    if lenient is None:
        print("FATAL: _worker_output_schema() returned None with the gate on")
        return 1

    runs = [_run_once(args.mode, lenient, "worker.output.json (lenient)")]
    if not runs[0]["passed"] and not runs[0]["checks"]["exit_zero"]:
        # Question 2 contingency: lenient rejected → try the strict variant.
        strict = _load_schema_resource("worker.strict.output.json")
        if strict:
            runs.append(_run_once(args.mode, strict, "worker.strict.output.json (strict)"))

    print(json.dumps({"issue": 269, "runs": runs}, indent=2))
    final = runs[-1]
    print()
    for name, ok in final["checks"].items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(
        f"\n{'PASS' if final['passed'] else 'FAIL'}: schema={final['schema']} mode={final['mode']}"
    )
    return 0 if final["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
