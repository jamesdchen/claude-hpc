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

## Running out of CI compute: account rotation

Set `HPC_GHA_POOL` instead of `HPC_GHA_REPO`/`GITHUB_TOKEN` to spread a campaign
across several accounts. When one returns a quota/billing `403`, the backend
advances to the next entry and re-dispatches — the campaign keeps going on your
other account at the next iteration boundary.

```bash
export HPC_GHA_WORKFLOW=fan-out.yml
export GH_TOKEN_A=ghp_aaa            # tokens stay in their own vars …
export GH_TOKEN_B=ghp_bbb
export HPC_GHA_POOL="me/exp=GH_TOKEN_A,other/exp=GH_TOKEN_B"   # … referenced by name
```

This works because the durable state is **local** (the Optuna study + the
completed-iteration sidecars), so switching accounts loses nothing — the next
batch just lands on the next account. Two things the backend handles for you:

- **Run ids are account-scoped.** `alive_job_ids` / `fetch_results` / `fetch_logs`
  **probe the pool**, so a batch that ran on account B is still polled and pulled
  from B even after rotation.
- **Only a quota/billing `403` rotates** (matched on `minutes` / `spending limit`
  / `billing` / …). A permissions `403` surfaces as a real error instead of
  silently burning through your accounts. Each rotation leaves an stderr
  breadcrumb.

Caveats: an **in-flight** run can't migrate — rotation takes effect on the *next*
dispatch, so switch at an iteration boundary (pull a running batch's results
before it rotates away). And Actions minutes bill to the **repo owner**, so each
pool entry must be a repo that account owns (a fork / separate push), not just a
collaborator on one shared repo.

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

## Where the input data lives

The runners have no shared filesystem. `fetch_results` solves *data-out* (per-task
metrics → artifacts); this is the *data-in* half — the training set every trial
reads. **Only the compute needs it**: the orchestrator just proposes
hyperparameters and reduces metrics, so the dataset never touches the laptop /
cloud container. It's purely a runner-side staging concern, content-addressed by
the `data_sha` hpc-agent already computes.

### The end-to-end flow

```
orchestrator                         backend                       each runner
────────────                         ───────                       ───────────
spec.input_datasets=["data/train.   reads data_sha off the         actions/cache key=data-<sha>
  parquet"]                          run sidecar, sends it as       → miss: dvc pull (fetch+md5
  → submit-flow writes               a workflow_dispatch input        verify) → hit: bytes restored
  data_sha to the sidecar            (cache key)                    LOCAL_DATA_DIR=$WORKSPACE/data
                                                                    executor reads it
```

1. **Declare it** — put `input_datasets: ["data/train.parquet"]` on the submit
   spec. submit-flow runs `compute_data_sha` and stamps `data_sha` on the run
   sidecar (provenance leg #222).
2. **The backend threads it** — `_execute_command` reads `data_sha` off
   `.hpc/runs/<run_id>.json` and sends it as a `data_sha` dispatch input. No user
   wiring; absent declaration → omitted (un-keyed cache).
3. **Each runner stages by content** — the workflow caches `data/` keyed by
   `data-<data_sha>`, so the bytes are pulled once per content version and any
   data change misses the cache and re-pulls.

### Recommended: DVC (the framework was built for it)

`compute_data_sha` **special-cases DVC**: when a `data/train.parquet.dvc` pointer
sits beside the path, it uses the pointer's md5 instead of re-hashing the working
tree. So with DVC the whole chain is one identity:

```bash
# once, on the orchestrator:
dvc add data/train.parquet      # writes data/train.parquet.dvc (commit it); bytes → DVC remote
dvc push
```

Then `data_sha == the DVC md5`, and the runner's `dvc pull` (in `fan-out.yml`)
fetches **and md5-verifies** the exact bytes — the cache key and the verified
content are the same hash end to end. The bytes live in any DVC remote (S3 / GCS /
Azure), private, never in the repo. Small tabular data is the trivial fallback:
commit it and `checkout` brings it (no `.dvc`, no pull).

### Runner-side: the executor reads `LOCAL_DATA_DIR`

`LOCAL_DATA_DIR` is the dispatcher-contract data root; your executor already keys
off it (no GitHub-specific code):

```python
# .hpc/executor.py
import os, xgboost as xgb, pandas as pd
from hpc_agent.execution.mapreduce.metrics_io import read_kw_env, write_metrics

kw = read_kw_env()                                          # {"max_depth": 6, "eta": 0.3, ...}
df = pd.read_parquet(os.path.join(os.environ["LOCAL_DATA_DIR"], "train.parquet"))
booster = xgb.train({"max_depth": int(kw["max_depth"]), "eta": float(kw["eta"])},
                    xgb.DMatrix(df.drop(columns="y"), label=df["y"]))
rmse = ...                                                  # eval on a holdout
write_metrics(os.environ["RESULT_DIR"], {"objective": rmse})  # → task-<i> artifact → reduce
```

Pin the version: every campaign iteration must train on the *same* data for
metrics to compare. `data_sha` riding in the run's `env_hash` makes accidental
drift change run identity, so it surfaces instead of silently skewing the sweep.

## Limits worth knowing

- A matrix is capped at **256 cells per run** and ~20 concurrent runners by
  default; for larger sweeps chunk into multiple dispatches.
- Standard runners are CPU-only, 6 h/job; results come back only as artifacts
  (default 90-day retention).

## Future: orchestrating from an ephemeral cloud container

A natural extension (recorded here, not yet implemented): run the orchestrator
itself in a Claude Code web container instead of a laptop. It inherits the same
two constraints, both already solved here:

- **Reachability** — the pure-API backend reaches GitHub over HTTPS, so a
  locked-down container that can't SSH a campus cluster can still drive a
  campaign (the network policy must allow `api.github.com`).
- **Ephemeral state** — the container is reclaimed after inactivity and re-cloned
  fresh, so the campaign state (`.hpc/runs/*.json`, `.hpc/campaigns/<id>/`, with
  `HPC_JOURNAL_DIR` pointed into the repo) must be committed back each iteration;
  `prior()` replays it on the next session. The same checkpoint-to-git discipline
  the data-staging and account-rotation sections rely on.

Pieces to add when this is taken on: a SessionStart hook / `setup.sh` that
installs hpc-agent + this plugin + strategy deps, the config as environment
variables, and the network policy. Then the whole pipeline is HTTPS + git — no
laptop, no SSH, no shared filesystem.
See https://code.claude.com/docs/en/claude-code-on-the-web.

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
