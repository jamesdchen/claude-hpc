# hpc-agent-github-actions

An hpc-agent backend plugin that runs task-array fan-outs on **GitHub Actions
runners** instead of an SSH cluster. You orchestrate locally (campaign loop,
`tasks.py`, Optuna ask/tell); each submit fans out as a workflow run whose
matrix has one cell per task; results come back as artifacts.

This is a **pure-API backend** in the sense of
[`docs/proposals/crowd-compute-backend.md`](../../../docs/proposals/crowd-compute-backend.md):
no SSH, no shared filesystem. It plugs into the same registry seam as the
built-in SGE/SLURM backends.

## Install + configure

```bash
pip install -e examples/plugins/hpc-agent-github-actions
```

Copy [`workflow-template/fan-out.yml`](workflow-template/fan-out.yml) into your
experiment repo's `.github/workflows/`, then point the backend at it:

```bash
export HPC_GHA_REPO=owner/your-repo       # where the workflow lives + runs
export HPC_GHA_WORKFLOW=fan-out.yml
export HPC_GHA_REF=main
export GITHUB_TOKEN=ghp_...               # actions:write (dispatch) + actions:read (poll/pull)
```

In `clusters.yaml`, name it like any scheduler (the host's config validator
accepts any plugin-registered backend name):

```yaml
clusters:
  github-actions:
    scheduler: github-actions
```

## How it maps onto hpc-agent

| Scheduler concept | This backend |
|---|---|
| construct backend | `from_build_context` — ignores the SSH fields, reads `$HPC_GHA_*` / `$GITHUB_TOKEN` |
| `qsub`/`sbatch` an array | `_execute_command` POSTs `workflow_dispatch`, resolves the run id (the "job id") |
| per-task kwargs | the workflow resolves `resolve(HPC_TASK_ID)` from your `.hpc/tasks.py` on the runner — same as the SLURM dispatcher node-side |
| `qstat` liveness | `alive_job_ids` → `GET /actions/runs/{id}` status |
| post-submit health | `classify_scheduler_state` (`queued`/`in_progress` → alive; `failure` → error; `cancelled` → held) |
| result pull (rsync) | `fetch_results` → download + unzip the run's artifacts |
| stderr logs | `fetch_logs` → download the run's job-logs zip |

The submit override lives in **`_execute_command`**, not `submit_array_tracked`:
submit-flow's single-array path (`_make_single_array_submission`) calls
`_build_command` + `_execute_command` and parses `JOB_ID_REGEX` from stdout, and
that is the path a real submit takes.

## What works end-to-end vs. what still needs bridging

**Covered by the backend seam** (dispatch on backend hooks, no host edit):

- construction via `from_build_context` (config seam)
- the submit itself (`workflow_dispatch` + run-id resolution)
- liveness polling (`alive_job_ids`) used by status / monitor / reconcile
- post-submit health (`classify_scheduler_state`)

**Not behind a backend hook — submit-flow / monitor / aggregate assume SSH +
a shared filesystem**, so these need wiring on the host side (the same two
assumptions the proposal flags as "do not survive contact"):

- **submit-flow's prelude** — `_validate_ssh_target` → ssh preflight probe →
  `rsync_push` → `deploy_runtime`. There is no login node and no shared mount;
  the runner gets your code via `actions/checkout`. Bypass the prelude with
  `HPC_AGENT_SKIP_PREFLIGHT=1` and `HPC_AGENT_SKIP_RSYNC_DEPLOY=1` (and pass
  placeholder `ssh_target` / `remote_path` / `script` in the spec, which a
  pure-API backend ignores).
- **per-task result reads** — monitor/aggregate read result dirs over the shared
  FS. `fetch_results` is the replacement (download + unzip artifacts); wire it
  where the SSH path rsync-pulls. The `reduce` job in the workflow already
  combines on Actions and emits one small `reduced` artifact, so you usually
  pull that, not the N per-task ones.
- the `build_*_cmd` / `parse_*` / `stderr_log_path` **staticmethods** can't be
  implemented (a `@staticmethod` can't hold the authenticated client); the
  instance methods above replace them.

For most tuning loops the clean path is to **not route through submit-flow** at
all — drive the backend's dispatch/poll/fetch from your own local loop. See the
`drive_gha.py` sketch in the chat history / the framework's
`code-driven-orchestration` doc.

## Limits worth knowing

- A matrix is capped at **256 cells per run** and ~20 concurrent runners by
  default; for larger sweeps chunk into multiple dispatches.
- Standard runners are CPU-only, 6 h/job; results come back only as artifacts
  (default 90-day retention).

## Live validation (the #269 discipline)

The build sandbox has no `GITHUB_TOKEN` and blocks outbound network, so the REST
calls ship **unvalidated**. Before relying on it, run one real dispatch:

```bash
export HPC_GHA_REPO=owner/your-repo HPC_GHA_WORKFLOW=fan-out.yml GITHUB_TOKEN=ghp_...
python -c "
from hpc_agent_github_actions.backend import GitHubActionsBackend
b = GitHubActionsBackend('$HPC_GHA_REPO', 'fan-out.yml')
cp = b._execute_command(b._build_command('1-4', 'smoke', {'HPC_RUN_ID':'smoke','EXECUTOR':'true'}), {}, None)
print('run id:', cp.stdout, 'exit:', cp.returncode)
print('alive:', b.alive_job_ids([cp.stdout]))
"
```

The pure logic (`classify_scheduler_state`, `_parse_total`) needs no network and
is the part to unit-test.
