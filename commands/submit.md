Help me submit HPC jobs via SSH using the project configuration.

All cluster commands run remotely via SSH. Code is synced from the local machine before submission.

## Setup

Read both config files:
- `project.yaml` in the current working directory
- `clusters.yaml`: resolve path via `python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from the configs. If `$ARGUMENTS` contains `--cluster <name>`, use that cluster instead of `project.cluster`.

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Step 0: Load Manifest

If `.hpc/` exists in the project directory, read `.hpc/cli_help.yaml` and `.hpc/experiments.yaml`. These contain cached executor CLI signatures, available experiment configs, and model/feature/subgroup registries. Use them to construct submissions without exploring source code.

If `.hpc/` is missing or stale (check `_meta.yaml` timestamp), suggest running `python -m hpc.collect` to regenerate.

## Step 1: Clarify What to Run

List the available stages from `project.stages` in a table:

| Stage | Description | Template | Executor |
|-------|-------------|----------|----------|

Ask which stage to run (if not already clear from `$ARGUMENTS`).

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

1. **Cluster job load** — run the appropriate queue status command (qstat for SGE, squeue for SLURM).

2. **No duplicate submission** — check for existing results using the stage's `result_pattern`:
   ```bash
   ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<stage.result_dir>/<stage.result_pattern> 2>/dev/null | wc -l'
   ```
   Report how many results already exist vs expected `total_chunks`.

3. **Dependency check** — if the stage has `depends_on`, verify the dependency stage completed. If incomplete, report and ask whether to proceed.

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
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && qsub \
    -t 1-<total_chunks> \
    -N <stage_name> \
    -o logs -j y \
    -l <resource_key>=<resource_val> \
    ... \
    -v CONDA_SOURCE=<cluster.conda_source>,CONDA_ENV=<project.conda_env>,MODULES='"'"'<...>'"'"',EXECUTOR='"'"'<stage.executor>'"'"',RESULT_DIR=<stage.result_dir>,TOTAL_CHUNKS=<total_chunks>,EXTRA_ARGS='"'"'<...>'"'"' \
    <template_path>'
```

Resource flags are built from `stage.resources`. Each key-value pair becomes a `-l key=value` flag. For GPU stages, the resource line includes the GPU type and cuda count (e.g., `-l gpu,A100,cuda=2`).

### SLURM Submission

Look up the template at `templates/slurm/<stage.template>.slurm` (relative to claude-hpc package root).

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && sbatch \
    --array=1-<total_chunks> \
    --job-name=<stage_name> \
    --output=logs/%x_%A_%a.out \
    --account=<cluster.account> \
    --mem=<stage.resources.mem> \
    --time=<stage.resources.time> \
    --cpus-per-task=<stage.resources.cpus> \
    ... \
    --export=CONDA_SOURCE=<cluster.conda_source>,CONDA_ENV=<project.conda_env>,MODULES='"'"'<...>'"'"',EXECUTOR='"'"'<stage.executor>'"'"',RESULT_DIR=<stage.result_dir>,TOTAL_CHUNKS=<total_chunks> \
    <template_path>'
```

For GPU stages on SLURM, add `--gres=gpu:<count>` and `--partition=gpu` (or the appropriate partition from stage.resources).

### After Submission

1. Parse the job ID from the submission output.
2. Report: job ID, stage name, total chunks, cluster, expected result location.
3. Suggest running `/monitor` to track progress.

### Multi-Stage Pipelines

If the project has multiple stages with `depends_on` chains, after successful submission of one stage, note which stages are now unblocked and can be submitted next.

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory in resources |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| ModuleNotFoundError | Env not set up | Check modules and conda_env in config |
| rsync failure | SSH key issue | Check `ssh $SSH_TARGET hostname` first |
