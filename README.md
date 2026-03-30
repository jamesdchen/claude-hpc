# claude-hpc

A Claude Code plugin that gives Claude slash commands for managing HPC jobs across SGE and SLURM clusters. Claude handles the full lifecycle — sync code, submit jobs, monitor progress, resubmit failures, aggregate results — all via SSH from your local machine.

## Setup

```bash
bash setup.sh
```

This script:
1. **Checks prerequisites** — python3, pip, ssh, rsync, ruff, mypy, jq
2. **Installs slash commands** — copies `commands/*.md` into `~/.claude/commands/` so they're available from any project
3. **Installs the `hpc` package** — editable pip install for the Python utilities the commands depend on

After setup, the commands are available globally in Claude Code.

## Collecting project context

Before submitting or monitoring jobs, run:

```bash
python -m hpc.collect
```

This generates a `.hpc/` directory in your project root containing cached artefacts that help the agent debug failures and construct submissions without exploring your source code:

| Artefact | What it caches |
|----------|---------------|
| `module_graph.yaml` | AST-traced import tree for each stage's executor (depth 2) — scopes which files are relevant to a failure |
| `cli_help.yaml` | Parsed `--help` output for each executor and aggregate command — arg names, types, defaults, choices |
| `experiments.yaml` | Experiment YAML configs and Python registry values (models, features, subgroups) |
| `_meta.yaml` | Timestamps and SHA256 hashes of source files for staleness detection |

The slash commands check `.hpc/` automatically. If it's missing or stale, they'll suggest regenerating it.

## Commands

| Command | What it does |
|---------|-------------|
| `/submit` | Sync code to the cluster and submit job arrays from `project.yaml` |
| `/monitor` | Poll job status, diagnose failures, resubmit with resource overrides, schedule next check |
| `/aggregate` | Validate chunk completeness, run aggregation on cluster, download summaries |
| `/sync` | Git fetch, pull with rebase, push — with conflict resolution and selective commits |

## Job templates

Four templates ship with claude-hpc, covering the CPU/GPU and SGE/SLURM combinations:

| Template | SGE | SLURM |
|----------|-----|-------|
| CPU array | `templates/sge/cpu_array.sh` | `templates/slurm/cpu_array.slurm` |
| GPU array | `templates/sge/gpu_array.sh` | `templates/slurm/gpu_array.slurm` |

Templates are parameterized via environment variables (`CONDA_SOURCE`, `CONDA_ENV`, `MODULES`, `EXECUTOR`, `RESULT_DIR`, `TOTAL_CHUNKS`, etc.) injected at submission time from `project.yaml` and `clusters.yaml`. The stage's `template` field selects which template to use, and the cluster's `scheduler` field determines the directory.

## Project integration

Each project needs a `project.yaml` at its root. Minimal example:

```yaml
project: my_backtest
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/my_backtest
conda_env: backtest-env
rsync_exclude: [.git/, results/, __pycache__, "*.pyc"]

stages:
  backtest:
    type: array
    executor: "python -m backtest.run"
    template: cpu_array
    resources: { cpus: 1, mem: "16G", walltime: "4:00:00" }
    chunks: 100
    result_pattern: "results/chunk_*.csv"
    aggregate_cmd: "python scripts/aggregate.py"
    summary_pattern: "*_summary*.csv"
```

See [`config/schema.md`](config/schema.md) for the full specification of `project.yaml` fields.

## Supported clusters

| Cluster | Institution | Scheduler |
|---------|------------|-----------|
| Hoffman2 | UCLA IDRE | SGE |
| Discovery | USC | SLURM |

Cluster connection details are defined in `config/clusters.yaml`.
