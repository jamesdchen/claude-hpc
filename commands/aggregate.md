Help me aggregate, validate, and analyze experiment results using the project configuration.

Aggregation runs on the cluster to avoid transferring many chunk files. Only summary files are downloaded locally.

## Setup

Read both config files:
- `project.yaml` in the current working directory
- `clusters.yaml`: resolve path via `python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from the configs. If `$ARGUMENTS` contains `--cluster <name>`, use that cluster instead of `project.cluster`.

## Project Type Detection

Check for `hpc.yaml` in the current working directory:
- If `hpc.yaml` exists → this is a **manifest project**. Read `_hpc_dispatch.json` to understand the grid structure.
- If only `project.yaml` exists → proceed with the existing stage-based aggregation below.

### Manifest Aggregation Differences

For manifest projects, the aggregation flow is the same as below (Steps 0-5) with these modifications:

1. **Chunk completeness**: Check each grid point's result directory separately. A grid point is complete when all its chunks (or its single result, if no chunking) are present.

2. **Per-grid-point aggregation**: If `results.aggregate_cmd` is defined in `hpc.yaml`, run it once per completed grid point (with `RESULT_DIR` set to that grid point's result directory), OR once globally if the aggregate command handles discovery itself.

3. **Summary download**: Download summaries from all grid point result directories, not just a single stage result dir.

4. **Reporting**: Show per-grid-point status and results:
   ```
   Aggregation results:
     ridge_har:      complete — QLIKE: 0.342, MSE: 0.0012
     ridge_pca:      complete — QLIKE: 0.298, MSE: 0.0010
     xgboost_har:    incomplete (95/100 chunks)
     xgboost_pca:    complete — QLIKE: 0.310, MSE: 0.0011
   ```

After this section, the existing Steps 0-5 continue unchanged for stage-based projects.

---

## Step 0: Load Manifest

If `.hpc/cli_help.yaml` exists, read the aggregate CLI args for the target stage. Use these to understand available aggregation options without reading the aggregation script source.

## Arguments

$ARGUMENTS formats:

1. **Specific stage**: `<stage_name>` — aggregate results for that stage
2. **Empty**: auto-discover which stages have completed results ready for aggregation

For auto-discover, check each stage's result_dir for completed chunks and report which stages are ready (all chunks present) vs incomplete.

## Step 1: Check Job Status

Before aggregating, confirm all jobs have finished by checking the queue (qstat for SGE, squeue for SLURM).

If jobs are still running for the target stage, report which ones and wait. Do NOT aggregate partial results unless explicitly asked.

## Step 2: Validate Chunk Completeness

Count completed results vs expected `total_chunks`:

```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<stage.result_dir>/<stage.result_pattern> 2>/dev/null | wc -l'
```

**If chunks are missing:**

1. Identify which chunk IDs are missing by listing what exists and computing the gaps.
2. Check job accounting for failure reasons.
3. Check error logs (tail -50).
4. Report findings and suggest resubmitting via `/submit <stage_name>` or `/monitor <stage_name>`.
5. Wait for resubmitted jobs, then re-validate before aggregating.

**Partial aggregation:** Only proceed when all expected chunks are present, unless the user explicitly asks to aggregate partial results. If partial, note the missing count and percentage.

## Step 3: Aggregate (on cluster)

Run the stage's `aggregate_cmd` on the cluster:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && <stage.aggregate_cmd>'
```

If the stage has no `aggregate_cmd` defined, report that and ask the user what to do.

Verify the command succeeds (exit code 0). If it fails, read stderr and report to user.

## Step 4: Download Summaries

After aggregation completes, pull summary files locally using the stage's `summary_pattern`:

```bash
rsync -az \
    --include='*/' \
    --include='<stage.summary_pattern>' \
    --exclude='*' \
    $SSH_TARGET:$REMOTE_PATH/<stage.result_dir>/ ./<stage.result_dir>/
```

If the stage defines multiple summary patterns (as a list), include each one. Verify downloaded files exist locally.

## Step 5: Interpret Results

After downloading, read the local summary files and report key findings.

When interpreting:
- Lead with the most important metric or finding
- Flag anomalies (empty results, unexpected values, low sample counts)
- If the project.yaml defines a `metrics` section for the stage, use those metric names and their sort order
- Compare against any baseline results if available

## Multi-Stage Aggregation

If the project has multiple stages and `$ARGUMENTS` is empty:
1. Check all stages for completeness
2. Aggregate stages in dependency order (stages with `depends_on` after their dependencies)
3. Report results for each stage separately
