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
| `experiment_paths` | list[str]  | no       | Glob patterns for experiment YAML configs (used by `hpc collect`). |
| `registries`       | map        | no       | Importable registries for model/feature/subgroup choices (used by `hpc collect`). |

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

## collect Fields

These optional fields configure `python -m hpc.collect`, which generates a `.hpc/` directory
with cached dependency graphs, CLI help, and experiment metadata.

### experiment_paths

List of glob patterns relative to the project root. Each matching YAML file is read and
summarized in `.hpc/experiments.yaml`.

```yaml
experiment_paths:
  - "projects/ml/experiments/*.yaml"
  - "projects/dl/experiments/*.yaml"
```

### registries

Map of registry names to importable Python references in `"module.path:ATTRIBUTE"` format.
Each attribute is imported and its value stored in `.hpc/experiments.yaml` under `registries`.

```yaml
registries:
  models: "projects.ml.models.registry:ALL_MODELS"
  features: "projects.ml.features.feature_groups:FEATURE_TYPES"
  subgroups: "projects.ml.features.feature_groups:SUBGROUPS"
```

If the import fails (e.g., missing dependencies on the local machine), the registry entry
is stored as `null`.

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

---
---

# hpc.yaml Specification (Experiment Manifest)

The experiment manifest is a simpler alternative to `project.yaml` for repos where
the author does not want to deal with HPC details. The author provides a run command
and a parameter grid; claude-hpc handles chunking, submission, monitoring, and
result collection automatically.

If both `hpc.yaml` and `project.yaml` exist, claude-hpc prefers `hpc.yaml` for
submission but `project.yaml` remains usable via explicit stage selection.

---

## Top-Level Fields

| Field           | Type      | Required | Description                                                      |
|-----------------|-----------|----------|------------------------------------------------------------------|
| `project`       | string    | yes      | Short project name (used in job names, paths, logs).             |
| `cluster`       | string    | yes      | Cluster key matching an entry in `clusters.yaml`.                |
| `remote_path`   | string    | yes      | Absolute path on the remote cluster for this project.            |
| `run`           | string    | yes      | Shell command for a single experiment run. Grid params are appended as `--key value` CLI args. |
| `grid`          | map       | yes      | Parameter grid (see below).                                      |
| `resources`     | map       | yes      | Resource request per task (see below).                           |
| `env`           | map       | no       | Environment setup (see below).                                   |
| `results`       | map       | no       | Result collection config (see below).                            |
| `chunking`      | map       | no       | Data chunking within each grid point (see below).                |
| `rsync_exclude` | list[str] | no       | Patterns passed to `rsync --exclude` during sync.                |

---

## grid

A map of **parameter_name -> list of values**. claude-hpc computes the Cartesian
product to generate one HPC task per combination (or N tasks per combination if
`chunking` is set).

```yaml
grid:
  model: [ridge, xgboost, lightgbm]
  features: [har, pca]
  seed: [1, 2, 3]
```

This produces 3 × 2 × 3 = 18 grid points. Each grid point becomes a task that runs:

```
<run> --model ridge --features har --seed 1
```

---

## env

| Field       | Type   | Required | Description                                                |
|-------------|--------|----------|------------------------------------------------------------|
| `modules`   | string | no       | Space-separated modules to load (e.g., `"python gcc"`).   |
| `conda_env` | string | no       | Conda environment to activate before running.              |

---

## resources

Same schema as `project.yaml` stage resources:

| Field      | Type   | Required | Description                                   |
|------------|--------|----------|-----------------------------------------------|
| `cpus`     | int    | no       | Number of CPU cores per task.                 |
| `mem`      | string | yes      | Memory per task (e.g., `"16G"`).              |
| `walltime` | string | yes      | Maximum wall-clock time (`HH:MM:SS`).         |
| `gpus`     | int    | no       | Number of GPUs per task.                      |
| `gpu_type` | string | no       | Preferred GPU type (e.g., `a100`, `v100`).    |

If `gpus` is present, the `gpu_array` template is used; otherwise `cpu_array`.

---

## results

| Field            | Type   | Required | Description                                                      |
|------------------|--------|----------|------------------------------------------------------------------|
| `dir`            | string | no       | Result directory template. Supports `{run_id}` placeholder.     |
| `pattern`        | string | no       | Glob pattern for result files within the result dir.             |
| `aggregate_cmd`  | string | no       | Command to run after all tasks complete.                         |
| `summary_pattern`| string | no       | Glob pattern for summary files to download after aggregation.    |

`{run_id}` is a deterministic identifier derived from the grid point's parameter
values (e.g., `ridge_har_1`).

---

## chunking

Optional. Splits each grid point into N data chunks for additional parallelism.
Without this, each grid point is a single HPC task.

| Field       | Type   | Required | Description                                              |
|-------------|--------|----------|----------------------------------------------------------|
| `total`     | int    | yes      | Number of chunks per grid point.                         |
| `chunk_arg` | string | no       | CLI flag for chunk index (default: `"--chunk-id"`).      |
| `total_arg` | string | no       | CLI flag for total chunks (default: `"--total-chunks"`). |

With chunking, total HPC tasks = grid_points × `total`. Task *i* maps to
grid point `i // total` and chunk `i % total`.

---

## How It Works

1. claude-hpc reads `hpc.yaml` and expands the grid into individual tasks.
2. A `_hpc_dispatch.json` manifest is generated mapping each task ID to its
   full command string and result directory.
3. A standalone `_hpc_dispatch.py` script is deployed alongside the manifest.
4. The job template runs `python3 _hpc_dispatch.py` as its executor.
5. The dispatch script reads the manifest, finds the command for its task ID,
   and executes it.

The experiment author's code receives grid params as normal CLI args — no
awareness of HPC, chunking, or task IDs required (unless using `chunking`).

---

## Examples

### Simple Grid Search (CPU)

```yaml
project: my_experiment
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/my_experiment

run: "python3 -m my_experiment.train"

grid:
  model: [ridge, xgboost, lightgbm]
  lr: [0.01, 0.001]
  seed: [1, 2, 3]

env:
  modules: "python gcc"

resources:
  cpus: 1
  mem: "16G"
  walltime: "4:00:00"

results:
  dir: "results/{run_id}"
  pattern: "*.csv"

rsync_exclude: [.git/, results/, __pycache__]
```

### Grid + Data Chunking (CPU)

```yaml
project: harxhar
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/project-cucuringu/harxhar

run: "python3 -m projects.ml.cli.executor"

grid:
  model: [ridge, xgboost, lightgbm, random_forest]
  features: [har, pca, ae]

chunking:
  total: 100
  chunk_arg: "--chunk-id"
  total_arg: "--total-chunks"

env:
  modules: "python gcc"

resources:
  cpus: 1
  mem: "16G"
  walltime: "4:00:00"

results:
  dir: "results/{run_id}"
  pattern: "results_chunk_*.csv"
  aggregate_cmd: "python projects/ml/scripts/aggregate.py"
  summary_pattern: "*_summary*.csv"

rsync_exclude: [.git/, results/, __pycache__, "*.pyc", .mypy_cache/, all30min/, .claude/]
```

### GPU Training Grid

```yaml
project: dl_sweep
cluster: hoffman2
remote_path: /u/home/j/jamesdc1/dl_sweep

run: "python3 train.py"

grid:
  architecture: [resnet18, resnet50]
  lr: [0.001, 0.0001]
  batch_size: [32, 64]

env:
  modules: "conda cuda/12.3"
  conda_env: dl-env

resources:
  cpus: 4
  mem: "32G"
  walltime: "6:00:00"
  gpus: 2
  gpu_type: a100

results:
  dir: "results/{run_id}"
  pattern: "*.pt"

rsync_exclude: [.git/, results/, __pycache__, data/]
```
