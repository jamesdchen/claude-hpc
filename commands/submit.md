Help me submit HPC jobs via SSH using the project configuration.

All cluster commands run remotely via SSH. Code is synced from the local machine before submission.

## Configuration

Read the two config files at the start:

- **$PROJECT_YAML** — `project.yaml` in the current working directory. Contains stages, cluster target, remote path, conda env, rsync excludes.
- **$CLUSTERS_YAML** — `config/clusters.yaml` in the claude-hpc package directory. Contains host, user, scheduler type, modules, conda_source, gpu_types.

```bash
# Locate configs
PROJECT_YAML="$(pwd)/project.yaml"
CLUSTERS_YAML="$(python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")')"
```

Parse the project config to extract:
- `project.cluster` — default cluster name (key in clusters.yaml)
- `project.remote_path` — remote working directory
- `project.conda_env` — conda environment name
- `project.rsync_exclude` — list of rsync exclude patterns
- `project.stages` — dict of stage definitions

Parse the cluster config to extract (for the target cluster):
- `host`, `user` — SSH target (`user@host`)
- `scheduler` — `sge` or `slurm`
- `modules` — list of modules to load
- `conda_source` — path to conda.sh on cluster
- `gpu_types` — ordered list of available GPU types

If `$ARGUMENTS` contains `--cluster <name>`, use that cluster instead of `project.cluster`.

Construct: `SSH_TARGET="$USER@$HOST"` and `REMOTE_PATH` from the config.

## Step 1: Clarify What to Run

List the available stages from `project.stages` in a table:

| Stage | Description | Template | Executor |
|-------|-------------|----------|----------|

Ask which stage to run (if not already clear from `$ARGUMENTS`).

For each stage, the project.yaml defines:
- `template` — name of the job template (looked up in `templates/{scheduler}/`)
- `executor` — the command to run (e.g., `python -m myproject.run`)
- `resources` — dict of resource requests (h_data, h_rt, gpu, cuda, etc.)
- `total_chunks` — default number of array tasks
- `result_pattern` — glob pattern for completed result files (e.g., `results_chunk_*.csv`)
- `result_dir` — output directory on the cluster
- `depends_on` — (optional) name of a stage that must complete first
- `aggregate_cmd` — (optional) command to run after all chunks complete
- `summary_pattern` — (optional) glob for summary files to download
- `gpu_fallback` — (optional) ordered list of GPU types to try on queue stalls
- `max_retries` — (optional) max resubmission attempts per chunk

## Step 2: Sync Code to Cluster

Push local code to the cluster using the project's rsync_exclude list:

```bash
# Build exclude flags from project.yaml rsync_exclude list
rsync -az --delete \
    --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
    # ... add each entry from project.rsync_exclude as --exclude='<pattern>' ...
    . $SSH_TARGET:$REMOTE_PATH/
```

Verify the sync succeeded (exit code 0) before proceeding.

## Step 3: Pre-Flight Validation

Run these checks via SSH:

1. **Cluster job load:**

   For SGE:
   ```bash
   ssh $SSH_TARGET "qstat -u $USER"
   ```
   For SLURM:
   ```bash
   ssh $SSH_TARGET "squeue -u $USER"
   ```

2. **No duplicate submission** — check for existing results using the stage's `result_pattern`:
   ```bash
   ssh $SSH_TARGET "ls $REMOTE_PATH/<stage.result_dir>/<stage.result_pattern> 2>/dev/null | wc -l"
   ```
   Report how many results already exist vs expected `total_chunks`.

3. **Dependency check** — if the stage has `depends_on`, verify that the dependency stage completed:
   ```bash
   # Count completed results for the dependency stage
   ssh $SSH_TARGET "ls $REMOTE_PATH/<dep_stage.result_dir>/<dep_stage.result_pattern> 2>/dev/null | wc -l"
   ```
   If the dependency is incomplete, report it and ask whether to proceed anyway.

## Step 4: Dry Run (Recommended)

Preview the submission command without actually launching jobs. Print:
- The full qsub/sbatch command that would be executed
- Resource requests from `stage.resources`
- Environment variables that would be passed
- Array range (1 to total_chunks)

Ask whether to proceed with actual submission.

## Step 5: Submit

Build the appropriate submission command based on the scheduler type.

### SGE Submission

Look up the template at `templates/sge/<stage.template>.sh` (relative to claude-hpc package root).

```bash
ssh $SSH_TARGET "cd $REMOTE_PATH && qsub \
    -t 1-<total_chunks> \
    -N <stage_name> \
    -o logs -j y \
    -l <resource_key>=<resource_val> \
    ... \
    -v CONDA_SOURCE=<cluster.conda_source>,CONDA_ENV=<project.conda_env>,MODULES='<space-separated cluster.modules>',EXECUTOR='<stage.executor>',RESULT_DIR=<stage.result_dir>,TOTAL_CHUNKS=<total_chunks>,EXTRA_ARGS='<any passthrough args>' \
    <template_path>"
```

Resource flags are built from `stage.resources`. Each key-value pair becomes a `-l key=value` flag. For GPU stages, the resource line includes the GPU type and cuda count (e.g., `-l gpu,A100,cuda=2`).

### SLURM Submission

Look up the template at `templates/slurm/<stage.template>.slurm` (relative to claude-hpc package root).

```bash
ssh $SSH_TARGET "cd $REMOTE_PATH && sbatch \
    --array=1-<total_chunks> \
    --job-name=<stage_name> \
    --output=logs/%x_%A_%a.out \
    --account=<cluster.account> \
    --mem=<stage.resources.mem> \
    --time=<stage.resources.time> \
    --cpus-per-task=<stage.resources.cpus> \
    ... \
    --export=CONDA_SOURCE=<cluster.conda_source>,CONDA_ENV=<project.conda_env>,MODULES='<space-separated cluster.modules>',EXECUTOR='<stage.executor>',RESULT_DIR=<stage.result_dir>,TOTAL_CHUNKS=<total_chunks>,EXTRA_ARGS='<any passthrough args>' \
    <template_path>"
```

For GPU stages on SLURM, add `--gres=gpu:<count>` and `--partition=gpu` (or the appropriate partition from stage.resources).

### After Submission

1. Parse the job ID from the submission output.
2. Report: job ID, stage name, total chunks, cluster, expected result location.
3. Suggest running `/monitor` to track progress.

### Multi-Stage Pipelines

If the project has multiple stages with `depends_on` chains, after successful submission of one stage, note which stages are now unblocked and can be submitted next.

## SSH Quoting Rule

**Always use single quotes** for the remote command string when it contains shell variables (`$f`, `$?`, etc.) that should expand on the remote host:
```bash
# CORRECT — single outer quotes, variables expand on remote host
ssh $SSH_TARGET 'cd /path && echo "$HOME"'

# WRONG — double outer quotes, local shell eats the $
ssh $SSH_TARGET "cd /path && echo \"$HOME\""
```

When you need to interpolate a **local** variable into the command, use single quotes with concatenation:
```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && ls'
```

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory in resources |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| ModuleNotFoundError | Env not set up | Check modules and conda_env in config |
| rsync failure | SSH key issue | Check `ssh $SSH_TARGET hostname` first |
