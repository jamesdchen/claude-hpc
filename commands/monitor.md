Monitor running HPC jobs via SSH and take corrective action.

## Setup

Read the claude-hpc agent guide for configuration schema, SSH conventions, and Python APIs:

```bash
python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "CLAUDE.md")'
```

Read that file, then read both config files (`project.yaml` in cwd, `clusters.yaml` at the path shown in the guide). Construct `SSH_TARGET` and `REMOTE_PATH` from the configs. If `$ARGUMENTS` contains `--cluster <name>`, use that cluster instead of `project.cluster`.

## Step 0: Load Module Graph

If `.hpc/module_graph.yaml` exists, read it. It maps each stage to its dependency tree — every project file the executor imports, nested by call chain. When diagnosing failures:

1. Find the failing file from the traceback in the module graph
2. Read only that file
3. If you need upstream context, follow the tree to the caller
4. Never read files outside the stage's dependency tree

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
   Check for active jobs belonging to the current project via queue status commands. Cross-reference with stage names from project.yaml to identify which stages are running.

## Step 1: Check Status

Run the appropriate scheduler query (qstat for SGE, sacct for SLURM) and count completed results:

```bash
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

**Stall heuristic**: If ALL tasks have been pending for >15 minutes with zero running, or if the state is unchanged across 2 consecutive checks, treat as a stall. Go to Step 2 with category `queue_stall`.

## Step 2: Diagnose Failures

Read error logs for failed chunks (tail -50 from the appropriate log path).

Check job accounting (qacct for SGE, sacct for SLURM).

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

Build the resubmission command using the same template and env vars as the original, with resource overrides applied. Use single-quote SSH convention from the agent guide.

**Update your job-ids list** for subsequent status checks.

## Step 4: Aggregate (if configured)

When all chunks complete and the stage has `aggregate_cmd`:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && <stage.aggregate_cmd>'
```

After aggregation:
1. Verify output files exist using `stage.summary_pattern`.
2. Download summaries locally via rsync (include only summary patterns, exclude everything else).
3. Read and report key findings from the local summary files.

### Multi-Stage Progression

If the current stage completes and another stage has `depends_on` pointing to this stage, prompt: "Stage `<next_stage>` depends on `<this_stage>` which is now complete. Submit it? (`/submit <next_stage>`)"

## Step 5: Schedule Next Check

**Skip if `all_complete` or fully abandoned.** Report done and stop.

### Adaptive wait interval

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
2. **Cron handoff**: Each CronCreate starts fresh. The prompt must carry all state.
3. **Minimize tool output**: Use `tail -20` for logs. Prefer compact status commands over verbose output.
