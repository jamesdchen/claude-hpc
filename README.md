# claude-hpc

Personal HPC orchestrator for Claude Code. Manages job submission, monitoring, and result aggregation across SGE and SLURM clusters.

## What it does

- **Submit** array jobs from a `project.yaml` sweep config
- **Monitor** job status across clusters (qstat/squeue), report pending/running/failed
- **Aggregate** results from finished jobs into a single output
- **Sync** project files to/from cluster scratch directories via rsync

Works on Hoffman2 (UCLA IDRE, SGE) and Discovery (USC, SLURM).

## Installation

```bash
pip install -e /path/to/claude-hpc        # editable install
pip install -e "/path/to/claude-hpc[dev]"  # with ruff, mypy, pytest
```

Then install the Claude Code commands:

```bash
bash setup.sh
```

## Usage

### As a Python package

```python
from hpc.backends import get_backend
from hpc.remote import ssh_run, rsync_push, rsync_pull
from hpc.lifecycle import log_event, report_status, detect_scheduler
from hpc.gpu import pick_gpu
from hpc._config import load_clusters_config, load_project_config

# Submit an array job
backend = get_backend("slurm", script="job.slurm")
backend.submit_array("my_job", total_chunks=100, tasks_per_array=100, job_env=env)

# Pick best GPU on Hoffman2
gpu = pick_gpu(["A100", "H200", "V100"], live=True, ssh_host="user@cluster")

# Check job status
report = report_status("results/", job_ids=["12345"], total_chunks=10, scheduler="slurm")
```

### As Claude Code commands

```
/submit     # Generate and submit job arrays from project.yaml
/monitor    # Check job status, resubmit failures
/aggregate  # Collect results from finished jobs
/sync       # rsync project files to/from cluster
```

## Project integration

Each project repo needs a `project.yaml`:

```yaml
project: my_project
cluster: hoffman2
remote_path: /u/home/user/my_project
conda_env: my_env
rsync_exclude: [.git/, results/, __pycache__]

stages:
  train:
    type: single
    executor: "python scripts/train.py"
    template: gpu_array
    resources: { cpus: 8, mem: "64G", walltime: "2:00:00", gpus: 1, gpu_type: a100 }
    result_pattern: "checkpoints/best.pt"

  sweep:
    type: array
    depends_on: train
    executor: "python scripts/sweep.py"
    template: gpu_array
    resources: { cpus: 4, mem: "16G", walltime: "1:00:00", gpus: 1, gpu_type: a100 }
    chunks: 10
    result_pattern: "results/chunk_*.csv"
    aggregate_cmd: "python scripts/aggregate.py"
```

Then add `claude-hpc` to your project's requirements and import from `hpc.*`.

## Package structure

| Module | Responsibility |
|--------|---------------|
| `hpc._config` | Load and validate `clusters.yaml` and `project.yaml` |
| `hpc.remote` | SSH/rsync wrappers (no hardcoded defaults) |
| `hpc.lifecycle` | Job event logging, status querying, result checking |
| `hpc.gpu` | GPU type selection (static fallback + live qstat scoring) |
| `hpc.backends/` | Scheduler adapters: SGE, SLURM, SGE-remote, dry-run |

## Cluster config

Defined in `config/clusters.yaml`:

```yaml
hoffman2:
  host: hoffman2.idre.ucla.edu
  user: jamesdc1
  scheduler: sge
  gpu_types: [a100, h200, a6000, h100, v100, rtx2080ti]

discovery:
  host: discovery2.usc.edu
  user: jc_905
  scheduler: slurm
  account: pollok_1603
  gpu_types: [a100, a40, v100, l40s]
```

## Development

```bash
ruff check hpc/
ruff format hpc/
mypy hpc/ --ignore-missing-imports
```
