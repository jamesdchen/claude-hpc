Help me aggregate, validate, and analyze experiment results using the project configuration.

Aggregation runs on the cluster to avoid transferring many chunk files. Only summary files are downloaded locally.

## Configuration

Read the two config files at the start:

- **$PROJECT_YAML** — `project.yaml` in the current working directory.
- **$CLUSTERS_YAML** — `config/clusters.yaml` in the claude-hpc package directory.

```bash
PROJECT_YAML="$(pwd)/project.yaml"
CLUSTERS_YAML="$(python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")')"
```

Extract from project config:
- Target cluster (or `--cluster` override from `$ARGUMENTS`)
- Stage definition: `result_dir`, `result_pattern`, `total_chunks`, `aggregate_cmd`, `summary_pattern`

Extract from cluster config:
- `host`, `user` → `SSH_TARGET="$USER@$HOST"`

Construct `REMOTE_PATH` from `project.remote_path`.

### SSH Quoting Rule
**Always use single quotes** for remote commands containing shell variables that should expand on the remote host:
```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && for f in results/*.csv; do echo "$f"; done'
```

## Arguments

$ARGUMENTS formats:

1. **Specific stage**: `<stage_name>` — aggregate results for that stage
2. **Empty**: auto-discover which stages have completed results ready for aggregation

For auto-discover, check each stage's result_dir for completed chunks:
```bash
for each stage in project.stages:
    ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<stage.result_dir>/<stage.result_pattern> 2>/dev/null | wc -l'
```

Report which stages are ready (all chunks present) vs incomplete.

## Step 1: Check Job Status

Before aggregating, confirm all jobs have finished:

For SGE:
```bash
ssh $SSH_TARGET 'qstat -u '"$USER"''
```

For SLURM:
```bash
ssh $SSH_TARGET 'squeue -u '"$USER"''
```

If jobs are still running for the target stage, report which ones and wait. Do NOT aggregate partial results unless explicitly asked.

## Step 2: Validate Chunk Completeness

Count completed results vs expected `total_chunks`:

```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/<stage.result_dir>/<stage.result_pattern> 2>/dev/null | wc -l'
```

**If chunks are missing:**

1. Identify which chunk IDs are missing by listing what exists and computing the gaps.
2. Check job accounting for failure reasons:

   For SGE:
   ```bash
   ssh $SSH_TARGET 'qacct -j <JOBID>'
   ```
   For SLURM:
   ```bash
   ssh $SSH_TARGET 'sacct -j <JOBID> --format=JobID,State,ExitCode,Elapsed,MaxRSS --noheader'
   ```

3. Check error logs:
   ```bash
   ssh $SSH_TARGET 'tail -50 '"$REMOTE_PATH"'/logs/<stage_name>.*'
   ```

4. Report findings and suggest resubmitting via `/submit <stage_name>` or `/monitor <stage_name>`.

5. Wait for resubmitted jobs, then re-validate before aggregating.

**Partial aggregation:** Only proceed when all expected chunks are present, unless the user explicitly asks to aggregate partial results. If partial, note the missing count and percentage.

## Step 3: Aggregate (on cluster)

Run the stage's `aggregate_cmd` on the cluster:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && <stage.aggregate_cmd>'
```

If the stage has no `aggregate_cmd` defined, report that no aggregation command is configured and ask the user what to do.

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

If the stage defines multiple summary patterns (as a list), include each one:
```bash
rsync -az \
    --include='*/' \
    --include='<pattern_1>' --include='<pattern_2>' ... \
    --exclude='*' \
    $SSH_TARGET:$REMOTE_PATH/<stage.result_dir>/ ./<stage.result_dir>/
```

Verify downloaded files exist locally.

## Step 5: Interpret Results

After downloading, read the local summary files and report key findings.

When interpreting:
- Lead with the most important metric or finding
- Flag anomalies (empty results, unexpected values, low sample counts)
- If the project.yaml defines a `metrics` section for the stage, use those metric names and their sort order (lower-is-better vs higher-is-better)
- Compare against any baseline results if available

## Multi-Stage Aggregation

If the project has multiple stages and `$ARGUMENTS` is empty:
1. Check all stages for completeness
2. Aggregate stages in dependency order (stages with `depends_on` after their dependencies)
3. Report results for each stage separately

## Key Paths

| What | Path |
|------|------|
| Project config | `project.yaml` (current directory) |
| Cluster config | `config/clusters.yaml` (claude-hpc package) |
| Job templates | `templates/{scheduler}/` (claude-hpc package) |
| Remote results | `$REMOTE_PATH/<stage.result_dir>/` |
| Local results | `./<stage.result_dir>/` |
| Remote logs | `$REMOTE_PATH/logs/` |
