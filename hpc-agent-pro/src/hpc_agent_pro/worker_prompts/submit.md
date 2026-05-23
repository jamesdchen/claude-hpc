Agent-facing composition over the **[submit-flow](../../docs/primitives/submit-flow.md) workflow atom** (full pre-flight + rsync + deploy + qsub + record pipeline in one CLI call). For just the journal-write half (when the agent has already qsubbed), use the [submit-spec](../../docs/primitives/submit-spec.md) primitive directly. Both are idempotent on `run_id`: a replay returns `data.deduped: true` and emits no cluster-side side effects.

Throughout this procedure, "invoke <primitive>" means call the primitive's `backed_by.cli` or `backed_by.python` entry point; see `docs/primitives/<name>.md` for the full contract. For envelope/exit-code shapes see `docs/reference/cli-spec.md`.

## Setup

**Load context first.** Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for run / campaign / cluster state. Never rely on conversational memory or shell variables — a context compaction or a session restart erases them; the on-disk state does not.

- `data.latest_run` — cluster, profile, resources, env, remote_path, campaign_id, run_id, cmd_sha, job_ids. On a `reuse`/`interview` action, read these instead of re-interviewing the user.
- `data.in_flight` — active runs (run_id, stage, ssh_target, job_ids).
- `data.campaigns` — campaign ids + cursor iteration.
- `data.next_step_hint` — `submit` / `monitor` / `aggregate`.

If a value you need later is absent here, derive it from the run sidecar on disk — never from memory.

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from hpc_agent import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Call [suggest-setup-action](../../docs/primitives/suggest-setup-action.md) to figure out where in the priority ladder the experiment sits — it returns `{priority, action, run_id, candidates, reason}`:

```bash
hpc-agent suggest-setup-action --experiment-dir .
```

Branch on `action`:

| `action` | Priority | Meaning | Procedure behavior |
|---|---|---|---|
| `monitor` | 0 | At least one in-flight run on the journal | Stop and report; the caller switches to the status workflow. |
| `reuse` | 1 | Per-experiment sidecars exist | Each sidecar carries the full v2 config snapshot — resources/env/constraints/runtime. Reuse keeps `tasks.py` byte-identical so `cmd_sha` matches. |
| `interview` | 2 | `.hpc/tasks.py` exists, no run history | Skip executor-discovery + axes interview (tasks.py already encodes the axis); jump to Step 4b (planner). |
| `fresh` | 3 | Nothing exists | Full interview from Step 1. |

## Step 1: Discover Executors

Invoke [discover-executors](../../docs/primitives/discover-executors.md). The primitive scans `executors/`, `scripts/`, `src/` (in order, falling back to repo root), filters utilities, and classifies each executor by contract.

Map flag set per contract:
- **New-contract** (`info.has_compute_function == true`): if `.hpc/tasks.py` exists, read `FLAGS[<module>]` for the per-executor flag list. If first submit, capture intended flags during Step 6b interview.
- **Old-contract** (`info.has_main_guard` only): run `python3 <info.path> --help` to map the CLI interface.

If `discover_executors` returns empty, scaffolding requires an interactive sub-interview which a headless worker cannot run — record the boundary in `decisions` and stop for the caller to handle.

## Step 2: Parse user intent

The caller has already parsed the user's natural-language request into a list of `(executor_id, axis_shape)` tuples; the result arrives via the invocation `fields`. Flags `--no-canary` and `campaign_id=<slug>` thread through verbatim.

For multi-executor submissions sharing `(ssh_target, remote_path)`, build a **batch spec** — `{"specs": [<per-spec>...], "rsync_excludes": [...], "skip_preflight": ...}`; `submit-flow` auto-routes it to the batched path (one rsync + one deploy + N qsubs). Heterogeneous batches raise `spec_invalid`. Why batch rather than N parallel submits: see [submit-flow.md](../../docs/primitives/submit-flow.md).

## Step 3: Plan the parallelization axis

The task list lives in user-written `.hpc/tasks.py` (`total()` + `resolve(task_id)`). Step 6 scaffolds it once per experiment; from then on it is committed and reused on every submit. There are two shapes, and Step 3 decides which:

- **Cartesian grid** — each task is one independent cell of a parameter grid. `tasks_example.py` Pattern 1; scaffolded deterministically by [build-tasks-py](../../docs/primitives/build-tasks-py.md) at Step 6b. The 80% case.
- **Planner-driven** — the executor iterates a *totally-ordered series* (a walk-forward backtest, an online-learning scan) and you want to fan that series out. Splitting a *stateful* series computation is only correct if each chunk replays the right warm-up; hpc-agent owns that via `hpc_agent.template.plan_tasks`. Emitted by [build-tasks-py](../../docs/primitives/build-tasks-py.md) when the spec carries a `data_axis` (Step 3b's classification).

### 3a: Detect a series axis

Read `compute()` / the `@register_run` function and its call graph — the same code-analysis pass that classifies hardware from `info.imports` at Step 4. A series axis is present when the executor loops over an ordered series (a time index, a date range, rows of a sorted frame) and you intend to parallelize *that loop*. If there is no series loop, it is a cartesian grid — skip to Step 4.

### 3b: Classify the `DataAxis`

The experiment declares nothing about parallelism — you classify it. The one question: **does the loop carry state, and is the state transition associative?** (Full model: `hpc_agent/template/axis.py`. The same classification reference lives in [build-tasks-py.md](../../docs/primitives/build-tasks-py.md) so an integrator driving `build-tasks-py` directly — without this procedure — gets the identical guidance.)

| Observation in `run()` | `DataAxis` | Halo |
|---|---|---|
| Loop body is a pure function of its row (no accumulator) | `Independent()` | none |
| Accumulates an *associative* summary (sum, count, min/max, sufficient statistics) | `Associative(monoid)` | none |
| Refits / re-reads a *trailing window* of bounded length (rolling stat, `train_window` lookback) | `BoundedHalo(halo_fn)` | ≈ the window length |
| Unbounded or order-dependent dependency (running state with no fixed horizon; trial *n* depends on `0..n-1`) | `Sequential()` | — |

Inference is **never trusted unverified** — classifying data dependencies is real program analysis and you will sometimes get it wrong. Two rules:

- **Default to `Sequential()` on any uncertainty.** A serial run is slow, not wrong. Narrow to a splittable axis only when the code makes the dependency structure unambiguous.
- **Bias halos large.** An over-wide halo wastes compute; a too-small halo is silent corruption. For a `train_window`-based refit, set `halo_fn` to the full window (e.g. `train_window` days × the intraday bar count), never a guess below it.

### 3c: Serial-elision gate (mandatory for a non-`Sequential` axis)

Before scaffolding a planner-driven `tasks.py`, prove the classification on a fixture: `hpc_agent.template.check_elision` (or `assert_elision_equivalent`) runs the experiment once whole and once split N ways and asserts the results agree. If it fails, the axis is misclassified — widen the halo or fall back to `Sequential()`. This gate is what makes the inference safe: a misclassified axis produces a job that runs fine and returns plausible-but-wrong numbers, and nothing else catches it. Do not skip it, and recommend the experiment repo wire `assert_elision_equivalent` into its CI as a required check.

If the projected task count exceeds `constraints.max_tasks` or ~1000, record a `magnitude_warning` in `decisions` / `anomalies` so the caller can confirm with the user before proceeding.

## Step 4: Auto-Configure Environment

Resolve in order: cluster (from `fields` or `data.latest_run`); `SSH_TARGET` + `REMOTE_PATH` from cluster config; environment classification from `info.imports`:

| Imports detected | Classification | Environment |
|---|---|---|
| `torch`/`tensorflow`/`cuda` | GPU/DL | Load CUDA modules + activate conda env |
| `sklearn`/`xgboost`/`lightgbm` | CPU/ML | Load python modules |
| `numpy`/`pandas` only | CPU/lightweight | Load python modules |

For DL executors with `conda_envs` listed in `clusters.yaml` → record the candidates as a `decisions` entry for the caller to confirm with the user; the caller re-invokes with the picked env in `fields`. Resource defaults: CPU/ML 1×16G×4h; GPU/DL 4×16G×6h×2gpu (gpu_type=first in cluster's `gpu_types`).

Build rsync excludes from `.gitignore` patterns + the standard set (`__pycache__/`, `*.pyc`, `.git/`, `.claude/`, `.mypy_cache/`) + result directories. `.hpc/` rides rsync — the cluster needs `tasks.py` and the in-flight `runs/<run_id>.json`; `submit-flow` protects the framework-deployed `.hpc/` files from `--delete` itself (see [submit-flow.md](../../docs/primitives/submit-flow.md)).

## Step 4b: Compute Throughput Plan

After grid expansion produces `total_tasks`, invoke [plan-throughput](../../docs/primitives/plan-throughput.md):

```bash
hpc-agent plan-throughput --cluster <name> --total-tasks <n> [--est-task-duration-s <s>]
```

It reads the cluster's scheduler constraints from `clusters.yaml`, packs the grid into concurrency-bounded waves, and returns `{strategy, total_batches, n_waves, est_total_wall_s, wave_map, ...}`. Thread the returned `wave_map` into `write_run_sidecar(..., wave_map=wave_map)` at Step 6d — the cluster-side combiner reads it from the sidecar. A cluster with no `constraints:` block falls back to scheduler defaults (a single array for a grid under the default `max_array_size`).

## Step 4c: Smart constraint planner (resource-quality aware)

For GPU profiles, invoke [score-submit-plan](../../docs/primitives/score-submit-plan.md). For CPU-only, skip.

Optional pre-check: [best-submit-window](../../docs/primitives/best-submit-window.md) (`hpc-agent best-submit-window --profile <p> --cluster <c> --within-hours 24 --top-k 5`) surfaces low-traffic windows. Advisory; record the "submit now vs wait" choice in `decisions` if non-trivial so the caller surfaces it to the user.

Three branches on `score-submit-plan`'s envelope:

### 4c-A: `needs_canary: true` (cold start)

No runtime priors exist. Don't try to score — submit a 1-task canary first using `data.canary_plan.constraint`. Run through Steps 5–10 with `--no-canary` (we **are** the canary). Wait for terminal; capture `gpu_type`, `node`, `elapsed_sec`, `exit_code` from sacct/qacct. On success, append a sample via `hpc_agent.state.runtime_prior.append_sample`. On SEGV: STOP and record `canary_segv` in `decisions` for the caller to surface to the user (do NOT auto-retry on a different node — the failure is informative; re-running blindly may mask whether the workload itself is buggy). On timeout: bump walltime 2× and retry the canary ONCE. After two timeouts record `canary_timeout` in `decisions` and stop.

After a *successful* canary, re-invoke score-submit-plan and proceed to 4c-B.

### 4c-B: `needs_canary: false` (priors exist)

`score-submit-plan` scores the candidates and runs its adversarial-backfill mode — walltime / footprint shrink recommendations, a probed `(walltime × mem × constraint)` lattice, and the closed-loop `walltime_drift` calibration. The full rubric and what each output field means are in [score-submit-plan.md](../../docs/primitives/score-submit-plan.md). The procedure's job is to act on its envelope:

**Auto-pick rule** (per-candidate): when `recommended_tuple.predicted_eta_sec is not None`, use the tuple's walltime/mem/cpus/constraint automatically — SLURM has confirmed a fitting backfill window. Record `rationale` in the audit file so the choice is replayable.

**Auto-apply rule** (cluster-wide): apply `array_reshape.recommended_max_array_size` automatically when present. Do NOT auto-apply `walltime_split` — record `walltime_split_pending` in `decisions` and stop; the caller must confirm the executor checkpoints before chaining (`requires_checkpointing: true` would otherwise kill work at every segment boundary).

After submission, write a prediction sidecar via `hpc_agent.forecast.calibration.record_prediction_sidecar` so post-completion ingestion can validate the calibration.

For each chosen candidate's `stressed_nodes`, record the `co_tenants` context in `decisions` per node so the caller can decide whether to soft-exclude. The caller re-invokes with the resulting `--exclude=<node1>,...` flag in `fields` and the procedure adds it to the sbatch invocation.

### 4c-C: planner errors

If `plan-submit` envelope is `ok: false`, fall back to static-constraint flow: take `gpu_constraint` and `constraints.max_walltime` from `clusters.yaml`, proceed without exclude list. Record the planner error verbatim in `anomalies` — quality awareness is degraded.

### Audit file

After Step 8 returns job IDs, write the decision to `.hpc/runs/<run_id>.decision.json`:

```python
import json
from pathlib import Path
from datetime import datetime, timezone
decision = {
    "schema_version": 1,
    "run_id": run_id,
    "profile": profile,
    "cluster": cluster,
    "submitted_at": datetime.now(timezone.utc).isoformat(),
    "candidates_considered": [...],
    "chosen": {"constraint": ..., "walltime_sec": ..., "exclude_nodes": [...], "rationale": ...},
    "job_ids": job_ids,
}
Path(f".hpc/runs/{run_id}.decision.json").write_text(json.dumps(decision, indent=2, sort_keys=True))
```

## Step 5: Confirm Run Plan (via summarize-submit-plan)

Don't hand-author the summary. Once Step 6c emits the resolved spec via [build-submit-spec](../../docs/primitives/build-submit-spec.md), render the canonical confirmation via [summarize-submit-plan](../../docs/primitives/summarize-submit-plan.md):

```bash
hpc-agent summarize-submit-plan --spec /tmp/submit_spec.json
```

The envelope's `data` carries `{headline, body, confirm_prompt}`. Surface `headline`, `body`, and `confirm_prompt` in the worker `result` so the caller can show them to the user. For multi-job submissions, call once per spec and concatenate bodies under one combined header. The primitive flips to a magnitude-warning prompt automatically when `total_tasks > 1000`.

## Step 6: Scaffold (or reuse) `.hpc/tasks.py` and write the per-run sidecar

### 6a: Reuse if `.hpc/tasks.py` exists

```python
from pathlib import Path
from hpc_agent import (
    framework_subdir, tasks_path, load_tasks_module, compute_cmd_sha,
)
experiment_dir = Path.cwd()
framework_subdir(experiment_dir)
tp = tasks_path(experiment_dir)
```

If `tp.exists()`, read it as-is — never regenerate. To change the axis, the user edits `.hpc/tasks.py` directly and re-runs. Skip to 6c.

### 6b: Scaffold from canonical example (first submit only)

If `tp.exists()` is False, walk through `hpc_agent/mapreduce/templates/scaffolds/tasks_example.py` (top-level `FLAGS: dict[str, list[Flag]]`, eager-materialized `_TASKS = [...]`, three commented-out usage patterns inline). Generate via [build-tasks-py](../../docs/primitives/build-tasks-py.md) — don't hand-author it. Refuses to overwrite without `--force`.

**Planner-driven axis (Step 3b).** When Step 3 classified a non-trivial `DataAxis`, pass it to [build-tasks-py](../../docs/primitives/build-tasks-py.md) in the spec's `data_axis` field: `{kind, chunks, series_length, halo_expr?, monoid?}`. The primitive then emits a `plan_tasks`-driven `tasks.py` deterministically — the `axes` become the sweep, the series axis is partitioned per the classification. The agent classifies; it never hand-writes `tasks.py`. `series_length` is the integer you probed at Step 3a; `halo_expr` (for `bounded_halo`) is a plain arithmetic expression over `params`, e.g. `params['train_window'] * 48`. The serial-elision gate (Step 3c) must have passed before the file is committed.

**Axis naming**: prefer experiment-prefixed axis names (`exp_horizon`, `ridge_alpha`) over bare ones (`horizon`, `alpha`) — a bare name whose uppercase form is a real env var (an axis `home` → `$HOME`) corrupts the executor's environment. `build-tasks-py` rejects names that collide with a reserved set at scaffold time; the mechanism and the recommended `HPC_KW_NAMESPACE_ONLY=1` default are in [build-tasks-py.md](../../docs/primitives/build-tasks-py.md).

Copy the dispatcher:
```python
import shutil
from hpc_agent import _PACKAGE_ROOT
shutil.copy(_PACKAGE_ROOT / "mapreduce" / "templates" / "scaffolds" / "cli_dispatcher.py", experiment_dir / ".hpc" / "cli.py")
```

Commit `.hpc/tasks.py` + `.hpc/cli.py`. No push — user controls upstream.

### 6c: Compute `cmd_sha`, check for resume

```python
from hpc_agent import compute_cmd_sha, compute_tasks_py_sha
tasks = load_tasks_module(tp)
cmd_sha = compute_cmd_sha(tasks)
tasks_py_sha = compute_tasks_py_sha(tp)
```

```bash
hpc-agent find-prior-run --experiment-dir . --cmd-sha "$CMD_SHA"
```

Branch on envelope's `{found, is_orphan}`:
- `found=False` → fresh; continue to 6d.
- `found=True, is_orphan=False` → real prior. Record in `decisions` and surface to the caller — only the user can choose resume-vs-fresh.
- `found=True, is_orphan=True` → half-baked sidecar. Suggest `prune-orphan-sidecars` or proceed and let `submit_flow_batch`'s auto-prune handle it.

### 6d: Write sidecar + build submit-flow spec

Use [build-submit-spec](../../docs/primitives/build-submit-spec.md) to assemble the spec — synthesizes `EXECUTOR`/`HPC_RUN_ID`/`HPC_CMD_SHA`/`HPC_TASK_COUNT`/`REPO_DIR`/`MODULES`/`CONDA_SOURCE`/`CONDA_ENV`/`HPC_RUNTIME`/`HPC_CAMPAIGN_ID`, picks the canonical script path from `(backend, is_gpu)`, validates against `schemas/submit_flow.input.json`.

Write the per-run sidecar via `write_run_sidecar(..., wave_map=wave_map)`. Pass `None` for any v2 field that doesn't apply. **Don't pass `job_ids` here** — the sidecar is *pending* until `submit-flow` runs `update_run_sidecar_job_ids` after qsub returns.

## Step 6b: Pre-flight Gate (cached per cluster)

Cache marker: `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json` (TTL 24h). If marker exists, `all_ok=true`, < 24h old → log `preflight: cached <N>m ago — OK` and skip to Step 7.

Otherwise invoke [check-preflight](../../docs/primitives/check-preflight.md) with `--cluster <name>`. On `data.all_ok == true`: write/update marker, continue. On any check failure: do NOT write marker, record `setup_required` in `decisions` with the failing checks verbatim and stop — the user fixes their environment with `hpc-agent setup --cluster <name>` and the caller re-invokes.

## Step 6c: Pre-submit campaign validation

Invoke `validate-campaign`:

```bash
hpc-agent validate-campaign --spec validate_campaign.input.json --experiment-dir .
```

Branch on `data.overall`:
- `pass` → proceed.
- `warn` → record warnings in `anomalies`; proceed.
- `fail` → do NOT proceed. Record the `error`-severity findings with `code`/`message`/`suggested_fix` in `decisions` and stop. **No `--force` flag by design** — the caller edits `.hpc/playbook.yaml` if a rule is wrong, then re-invokes.

## Step 6d: Predict start time

Invoke [predict-start-time](../../docs/primitives/predict-start-time.md). Inputs: squeue + sshare snapshots (gather via SSH first), partition info, your priority/walltime/constraint, candidate offsets `[0,1,3,6,12,24]`. Surface result:
- `best_submit_offset_hours == 0` → submit now is optimal.
- `> 0` → record "wait N hours, predicted total time M minutes vs submit-now's M' minutes" in `decisions` so the caller can ask the user.

Advisory, NOT a gate. The procedure always proceeds; the predictor is decision support.

## Step 7-8: Invoke `submit-flow`

Steps 7 (rsync), 7b (canary), 8 (qsub), 10 (record) are ONE CLI call. Spec shape (matches `schemas/submit_flow.input.json`):

```json
{
  "profile": "<job_name>", "cluster": "<cluster>", "ssh_target": "user@host",
  "remote_path": "<remote_path>", "job_name": "<job_name>",
  "run_id": "<run_id from 6d>", "total_tasks": <tasks.total()>,
  "backend": "sge", "script": ".hpc/templates/cpu_array.sh",
  "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_RUN_ID": "...", ...},
  "pass_env_keys": [...],
  "canary": true, "campaign_id": "<slug>", "runtime": "uv",
  "skip_preflight": true
}
```

`skip_preflight: true` is correct — Step 6b just ran. For GPU jobs: `script: ".hpc/templates/gpu_array.sh"` (SGE) or `gpu_array.slurm` (SLURM).

```bash
hpc-agent submit-flow --spec spec.json --experiment-dir .
```

- `data.deduped: true` → original cluster jobs running. Record `deduped` in `decisions`; the caller switches to the status workflow.
- `data.deduped: false` → fresh. Capture `data.run_id`/`job_ids`/`canary_job_ids`.
- Error envelopes: branch by `error_code` per submit-flow's contract.

### Canary verification (route through `verify-canary`)

When `data.canary_done: true`:

```bash
hpc-agent verify-canary --experiment-dir . --canary-run-id "$CANARY_RUN_ID" --expect-output "results/seed_42/metrics.json"
```

Branch:
- `ok=True` → continue to main array submit.
- `ok=False` → record `stderr_tail` verbatim and the `failure_kind` (`dispatcher_failed`/`import_error`/`oom_killed`/`missing_output`/`timeout`) in `decisions`, stop.

## Step 8b: Verify the array is queued/running

`qsub`/`sbatch` returning a job ID is necessary but not sufficient. Confirm each returned job ID is alive on the cluster BEFORE reporting success:

```bash
# SLURM
ssh $SSH_TARGET 'squeue -j '"$JOB_IDS"' -h -o "%i %T %r"; sacct -j '"$JOB_IDS"' -n -P -o JobID,State,Reason 2>&1 | head'
# SGE
ssh $SSH_TARGET 'qstat -j '"$JOB_IDS"' 2>&1 | head -40; qstat -u '"$USER"' | awk "NR>2"'
```

Classify each job ID as **healthy** (proceed) or **failed** (abort) per the state taxonomy in [scheduler-states.md](../../docs/reference/scheduler-states.md). A wave-2+ job pending on a dependency is healthy.

On a failed state: record the scheduler reason verbatim and the bad job ID in `decisions`, stop. Do not run Step 9 or Step 10.

## Step 9-10: Cache + report

Do not cache run config in conversational memory. `submit-flow` persists the full v2 config snapshot (executor, cluster, remote_path, env, resources) to the run sidecar; any later step recovers it with `hpc-agent load-context`. Conversational memory is lost on context compaction or a session restart — the sidecar is not.

Report after submission and Step 8b verification: job ID, executor(s), grid dimensions, total tasks, cluster, verified scheduler state. The caller suggests `/monitor-hpc` to track progress.

The journal write happens inside `submit-flow` via `runner.submit_and_record`. For multi-executor submissions (one sidecar per executor), invoke `submit-flow` once per submitted job — each call writes its own sidecar.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` or every cluster call hangs on auth. The user runs `hpc-agent setup --cluster <name>` once per machine to probe the environment and populate the 24h cache marker Step 6b reads.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. Sleep 1s between back-to-back calls or expect `scheduler_throttled`.
- **Idempotency**: `submit-flow` is replay-safe on `run_id`. If `data.deduped: true`, original cluster jobs are running — do NOT re-invoke.
- **No cancel/abort**: hpc-agent has no kill primitive. If the user decides an experiment is bad, the caller stops monitoring; cluster jobs run to walltime.
- `--dry-run` never touches the cluster and never writes to the journal — safe to run repeatedly.
- The cluster-side template translates the scheduler's per-task index (`SGE_TASK_ID` / `SLURM_ARRAY_TASK_ID`) into `HPC_TASK_ID` (0-based) before exec'ing `$EXECUTOR`, which then imports `.hpc/tasks.py`, calls `tasks.resolve(HPC_TASK_ID)`, and runs the executor command from the sidecar with kwargs merged into the env.
