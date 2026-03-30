# project.yaml Specification

Each project managed by claude-hpc has a `project.yaml` at its root defining
cluster targets, sync rules, and computational stages.

---

## Top-Level Fields

| Field            | Type       | Required | Description                                                  |
|------------------|------------|----------|--------------------------------------------------------------|
| `project`        | string     | yes      | Short project name (used in job names, paths, logs).         |
| `cluster`        | string     | yes      | Cluster key matching an entry in `clusters.yaml`.            |
| `remote_path`    | string     | yes      | Absolute path on the remote cluster for this project.        |
| `conda_env`      | string     | yes      | Conda environment to activate before running any stage.      |
| `rsync_exclude`  | list[str]  | no       | Patterns passed to `rsync --exclude` during sync.            |

---

## stages

A map of **stage_name -> stage_config**. Stages execute in dependency order
(topological sort of `depends_on` edges). Independent stages may run in
parallel.

### Stage Fields

| Field             | Type                 | Required | Description                                                                                       |
|-------------------|----------------------|----------|---------------------------------------------------------------------------------------------------|
| `type`            | `single` \| `array`  | yes      | `single` = one job. `array` = SLURM/SGE array job.                                               |
| `executor`        | string               | yes      | Shell command to run (receives chunk index via env var for array jobs).                            |
| `template`        | string               | yes      | SLURM/SGE template name from `templates/`.                                                        |
| `resources`       | map                  | yes      | Resource request (see below).                                                                     |
| `chunks`          | int                  | no       | Number of array tasks (required when `type: array`).                                              |
| `depends_on`      | string \| list[str]  | no       | Stage name(s) that must complete before this stage starts.                                        |
| `result_pattern`  | string               | no       | Glob pattern for expected output files. `{exp_id}` is interpolated at runtime.                    |
| `aggregate_cmd`   | string               | no       | Command to run after all array tasks complete to merge results.                                   |
| `summary_pattern` | string               | no       | Glob pattern for summary/aggregate output files.                                                  |
| `gpu_fallback`    | list[str]            | no       | Ordered list of GPU types to try if the preferred type is unavailable.                            |
| `max_retries`     | int                  | no       | Maximum number of automatic resubmissions on failure.                                             |
| `seed_env`        | string               | no       | Environment variable that receives the array task ID (default: `SLURM_ARRAY_TASK_ID`).           |

### Resource Fields

| Field      | Type   | Required | Description                                   |
|------------|--------|----------|-----------------------------------------------|
| `cpus`     | int    | yes      | Number of CPU cores per task.                 |
| `mem`      | string | yes      | Memory per task (e.g., `"16G"`, `"4G"`).      |
| `walltime` | string | yes      | Maximum wall-clock time (`HH:MM:SS`).         |
| `gpus`     | int    | no       | Number of GPUs per task.                      |
| `gpu_type` | string | no       | Preferred GPU type (e.g., `a100`, `v100`).    |

---

## Examples

### Array of Identical Tasks (single stage)

A classic embarrassingly-parallel backtest split into 100 chunks on CPU:

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
    result_pattern: "results/{exp_id}/chunk_*.csv"
    aggregate_cmd: "python scripts/aggregate.py"
    summary_pattern: "*_summary*.csv"
```

### Multi-Stage Pipeline with Dependencies

A train -> generate -> evaluate pipeline where each stage depends on the
previous one:

```yaml
project: generative_model
cluster: discovery
remote_path: /home1/user/generative_model
conda_env: gen-env
rsync_exclude: [.git/, samples/, __pycache__, "*.pyc", data/]

stages:
  train:
    type: single
    executor: "python scripts/train.py"
    template: gpu_array
    resources: { cpus: 8, mem: "64G", walltime: "2:00:00", gpus: 1, gpu_type: a100 }
    result_pattern: "checkpoints/best.pt"

  generate:
    type: array
    depends_on: train
    executor: "python scripts/generate.py --checkpoint checkpoints/best.pt"
    template: gpu_array
    resources: { cpus: 8, mem: "64G", walltime: "0:30:00", gpus: 1, gpu_type: a100 }
    chunks: 10
    seed_env: SLURM_ARRAY_TASK_ID
    result_pattern: "samples/seed_*/chunk_*.pt"

  evaluate:
    type: single
    depends_on: generate
    executor: "python scripts/evaluate.py --checkpoint checkpoints/best.pt"
    template: gpu_array
    resources: { cpus: 8, mem: "64G", walltime: "0:30:00", gpus: 1, gpu_type: a100 }
    result_pattern: "eval_results/metrics.json"
    summary_pattern: "eval_results/*"
```
