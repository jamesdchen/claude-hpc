Monitor running HPC jobs via SSH and take corrective action.

## Configuration

Read the two config files at the start:

- **$PROJECT_YAML** — `project.yaml` in the current working directory.
- **$CLUSTERS_YAML** — `config/clusters.yaml` in the claude-hpc package directory.

```bash
PROJECT_YAML="$(pwd)/project.yaml"
CLUSTERS_YAML="$(python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")')"
```

Extract from project config:
- Target cluster name (or `--cluster` override from `$ARGUMENTS`)
- Stage definition: `result_pattern`, `result_dir`, `total_chunks`, `gpu_fallback`, `max_retries`, `aggregate_cmd`, `depends_on`

Extract from cluster config:
- `host`, `user` → `SSH_TARGET="$USER@$HOST"`
- `scheduler` — `sge` or `slurm`
- `gpu_types` — fallback order for GPU queue stalls

Construct `REMOTE_PATH` from `project.remote_path`.

### SSH Quoting Rule
**Always use single quotes** for remote commands containing shell variables that should expand on the remote host:
```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && for f in results/*.csv; do echo "$f"; done'
```

## Operating Principles

1. **Act autonomously on known failures.** For OOM, walltime, and node failures, immediately resubmit with appropriate resource overrides. Do NOT ask for permission. Only pause for code bugs or unrecognized errors.
2. **Compact context each iteration.** Summarize all prior monitoring output into a single state block before scheduling the next check.
3. **Self-loop.** After each monitoring cycle, schedule the next check using `CronCreate` with an adaptive interval.

## Arguments

$ARGUMENTS formats (pick one):

1. **Stage + monitor** (no job-ids — checks active jobs for the stage):
   `<stage_name>` or `<stage_name> --cluster <name>`

2. **Monitor existing** (job-ids provided):
   `<stage_name> <job_ids> [total_chunks]`
   Example: `train 12345678,12345679 100`

3. **Auto-discover** (empty):
   Check for active jobs belonging to the current project.

   For SGE:
   ```bash
   ssh $SSH_TARGET 'qstat -u '"$USER"''
   ```
   For SLURM:
   ```bash
   ssh $SSH_TARGET 'squeue -u '"$USER"' --format="%.18i %.9P %.30j %.8T %.10M %.6D %R"'
   ```
   Cross-reference with stage names from project.yaml to identify which stages are running.

## Step 1: Check Status

### SGE
```bash
# Job queue status
ssh $SSH_TARGET 'qstat -u '"$USER"''

# Count completed results for the stage
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<stage.result_dir>/<stage.result_pattern> 2>/dev/null | wc -l'
```

### SLURM
```bash
# Job queue status
ssh $SSH_TARGET 'sacct -j <JOB_ID> --format=JobID,State,ExitCode,Elapsed,MaxRSS --noheader'

# Count completed results
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<stage.result_dir>/<stage.result_pattern> 2>/dev/null | wc -l'
```

Parse results to determine state:

| Condition | State | Action |
|-----------|-------|--------|
| completed == total_chunks | `all_complete` | Go to Step 4 |
| running > 0 or pending > 0 | `still_running` | Check for stalls (Step 1b), then Step 5 |
| failed > 0 and running == 0 | `has_failures` | Go to Step 2 |
| completed == 0 and running == 0 | `all_failed` | Go to Step 2 (triage carefully) |

### Step 1b: Detect Queue Stalls

For SGE — check how long jobs have been queued:
```bash
ssh $SSH_TARGET 'qstat -u '"$USER"' -j <JOBID>' | grep submission_time
```

For SLURM — check pending duration:
```bash
ssh $SSH_TARGET 'squeue -j <JOBID> --format="%.18i %.8T %.20S %.20V" --noheader'
```

**Stall heuristic**: If ALL tasks have been pending for >15 minutes with zero running, or if the state is unchanged across 2 consecutive checks, treat as a stall. Go to Step 2 with category `queue_stall`.

## Step 2: Diagnose Failures

Read error logs for failed chunks.

For SGE:
```bash
ssh $SSH_TARGET 'tail -50 '"$REMOTE_PATH"'/logs/<stage_name>.o<JOBID>.<TASKID>'
```
Or check scratch for GPU jobs:
```bash
ssh $SSH_TARGET 'tail -50 <cluster.scratch>/<stage_name>.o<JOBID>.<TASKID>'
```

For SLURM:
```bash
ssh $SSH_TARGET 'tail -50 '"$REMOTE_PATH"'/logs/<stage_name>_<JOBID>_<TASKID>.out'
```

Check job accounting:

For SGE:
```bash
ssh $SSH_TARGET 'qacct -j <JOBID> -t <TASKID>'
```

For SLURM:
```bash
ssh $SSH_TARGET 'sacct -j <JOBID>_<TASKID> --format=JobID,State,ExitCode,Elapsed,MaxRSS,MaxVMSize --noheader'
```

Classify the failure:

| Pattern | Category | Action |
|---------|----------|--------|
| `CUDA out of memory` / `OutOfMemoryError` | GPU OOM | Resubmit with more memory + smaller batch |
| High memory usage + exit !=0 | System OOM | Resubmit with higher memory limit |
| Time limit exceeded | Walltime | Resubmit with longer walltime |
| Node failure / `Eqw` / `NODE_FAIL` | Infra issue | Resubmit as-is |
| All tasks pending >15min / unchanged across 2 checks | Queue stall | Delete stalled job, resubmit with GPU fallback |
| Python traceback with clear bug | Code bug | **STOP. Report to user. Do NOT resubmit.** |
| Unrecognized error | Unknown | **STOP. Read full log, report to user.** |

**AUTONOMY RULE**: For OOM, walltime, node failures, and queue stalls — act immediately. Only STOP for code bugs and unrecognized errors.

## Step 3: Resubmit Failed Chunks

Check retry count. The stage's `max_retries` (default 3) is the limit. If exceeded, report to user and skip.

### Resource overrides by failure type

| Failure | Retry 1 | Retry 2+ |
|---------|---------|----------|
| GPU OOM | 2x memory, batch_size/2 | 4x memory, batch_size/4 |
| System OOM | 2x memory | 4x memory |
| Timeout | 2.5x walltime | 3.5x walltime |
| Node fail | no overrides | no overrides |
| Queue stall | switch GPU type (use `gpu_fallback` from stage, or `gpu_types` from cluster) | next GPU in fallback |

### GPU Fallback Order

Use the stage's `gpu_fallback` list if defined, otherwise fall back to `cluster.gpu_types`. Skip the GPU type that stalled. For single-GPU types, also adjust cuda count and batch size as needed.

### SGE Resubmission
```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && qsub -t <failed_task_ids> \
    -N <stage_name> -o logs -j y \
    -l <resource_overrides> \
    -v CONDA_SOURCE=<...>,CONDA_ENV=<...>,MODULES='"'"'<...>'"'"',EXECUTOR='"'"'<...>'"'"',RESULT_DIR=<...>,TOTAL_CHUNKS=<...>,EXTRA_ARGS='"'"'<...>'"'"' \
    <template_path>'
```

### SLURM Resubmission
```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && sbatch \
    --array=<failed_task_ids> \
    --job-name=<stage_name> \
    --output=logs/%x_%A_%a.out \
    --account=<cluster.account> \
    <resource_overrides> \
    --export=CONDA_SOURCE=<...>,CONDA_ENV=<...>,MODULES='"'"'<...>'"'"',EXECUTOR='"'"'<...>'"'"',RESULT_DIR=<...>,TOTAL_CHUNKS=<...> \
    <template_path>'
```

**Update your job-ids list** for subsequent status checks.

## Step 4: Aggregate (if configured)

When all chunks complete and the stage has `aggregate_cmd`:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && <stage.aggregate_cmd>'
```

After aggregation:
1. Verify output files exist using `stage.summary_pattern`.
2. Download summaries locally:
   ```bash
   rsync -az \
       --include='*/' --include='<stage.summary_pattern>' --exclude='*' \
       $SSH_TARGET:$REMOTE_PATH/<stage.result_dir>/ ./<stage.result_dir>/
   ```
3. Read and report key findings from the local summary files.

### Multi-Stage Progression

If the current stage completes and another stage has `depends_on` pointing to this stage, prompt: "Stage `<next_stage>` depends on `<this_stage>` which is now complete. Submit it? (`/submit <next_stage>`)"

## Step 5: Schedule Next Check

**Skip if `all_complete` or fully abandoned.** Report done and stop.

### Adaptive wait interval

If progress data is available (percentage complete, estimated time remaining):

| Condition | Interval | Reason |
|-----------|----------|--------|
| < 10% complete | 3 min | pace still settling |
| ETA < 10 min | 3 min | finishing soon |
| ETA 10-30 min, stable pace | 10 min | stable, moderate time |
| ETA 10-30 min, unstable pace | 5 min | fluctuating, moderate time |
| ETA > 30 min, stable pace | 15 min | stable, long run |
| ETA > 30 min, unstable pace | 10 min | fluctuating, long run |

Fallback (no progress data):

| State | Interval |
|-------|----------|
| All pending, none running | 5 min |
| Some running, no progress yet | 3 min |
| Just resubmitted failed chunks | 3 min |
| Unchanged from previous check | double previous interval (cap 15 min) |

### Schedule via CronCreate

1. Cancel any existing monitor cron job.
2. Create a one-shot cron at current time + interval.
3. The prompt must include full state for the next iteration:

```
/monitor <stage_name> <comma_separated_job_ids> <total_chunks>

[Monitor State] stage=<name> | cluster=<cluster> | chunks=X/Y done, Z running, W failed | retries: {chunk: count, ...} | jobs: <id_list> | gpu_type: <current_gpu> | last_check: <time> | prev_interval: <minutes> | consecutive_pending: <count>
```

4. Report: `Next check in X min (reason). Cron job: <id>`

## Step 6: Report

Always end with a concise summary:
- Chunks: X/Y complete, Z running, W failed
- Actions taken this iteration (if any)
- Next: waiting / needs attention / done

## Context Management

1. **Within a conversation**: Avoid re-reading data already in context. Summarize before scheduling.
2. **Cron handoff**: Each CronCreate starts fresh. The prompt must carry all state:
   ```
   [Monitor State] stage=<name> | cluster=<cluster> | chunks=X/Y done, Z running, W failed | retries: {...} | jobs: <ids> | last_check: <time> | prev_interval: <min>
   [Actions taken]: resubmitted chunk 3 (OOM, retry 1), ...
   ```
3. **Minimize tool output**: Use `tail -20` for logs. Prefer compact status commands over verbose output.
