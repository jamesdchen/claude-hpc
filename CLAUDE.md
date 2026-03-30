# Behavioral Guide

## Parallelization Strategy

### When to parallelize (DO)
- **Exploration**: When a task touches 2+ modules or the scope is unclear, launch 2-3 Explore agents in parallel (one per area) before planning.
- **File writes**: When implementing changes across multiple independent files, use parallel Agent calls (one per file or module).
- **Verification**: Run tests, lint (ruff), and type-check (mypy) in parallel after implementation. Use separate Bash calls for each.
- **Large tasks**: Use /swarm whenever the task decomposes into 3+ independent subtasks that touch different files.

### When NOT to parallelize (DON'T)
- **Same-file edits**: Never dispatch two agents to edit the same file. Sequential edits to one file must be sequential.
- **Ordered operations**: Operations with causal dependencies must be sequential (e.g., stage -> commit -> push; create branch -> switch -> edit).
- **Shared state**: If two tasks read/write the same global state, config, or database, run them sequentially.
- **Clarification needed**: Don't parallelize when the task is ambiguous -- clarify first, then parallelize the execution.

## Task Approach

### Read before asking
When uncertain about code, architecture, or intent:
1. Read the relevant files, git log, and docs FIRST.
2. Only ask the user when the answer genuinely isn't in the codebase.
3. Prefer `git log --oneline -20`, `git blame`, and reading source over asking "what does this do?"

### Minimize friction
- Batch tool calls to reduce permission prompts -- if 4 independent reads are needed, do them in one message.
- Don't ask for permission to proceed; ask only for clarification.
- Execute the obvious path; flag alternatives only when the tradeoff is real.

## Pre-commit Discipline

Before any `git commit`:
1. Run `ruff check --fix` on all staged .py files.
2. Run `ruff format` on all staged .py files.
3. Run `mypy --ignore-missing-imports` on all staged .py files.
4. Run these checks in parallel, fix any issues, then commit.

## Communication Style
- Terse. Lead with the action or answer, not the reasoning.
- No trailing summaries of what you just did -- the diff speaks.
- No filler ("Let me", "I'll go ahead and", "Great question").
- Use tables and bullet points over prose.

## HPC Orchestration

This project provides Claude Code slash commands and a Python package for managing jobs across HPC clusters.

### Configuration files
- **`config/clusters.yaml`** — Cluster definitions (host, user, scheduler, scratch path, modules, GPU types). Each cluster entry maps a short name to its SSH/scheduler details.
- **`project.yaml`** (per-project, user-created) — Defines the experiment: conda env, script path, sweep parameters, resource requests, and result aggregation rules. Place it in the project root.

### Available commands
| Command | Purpose |
|---------|---------|
| `/submit` | Generate and submit job arrays from project.yaml sweep config |
| `/monitor` | Check job status across clusters (qstat/squeue), report pending/running/failed |
| `/aggregate` | Collect results from finished jobs, merge into a single output |
| `/sync` | rsync project files to/from cluster scratch directories |

### Workflow
1. Define clusters in `config/clusters.yaml` (done once).
2. Create `project.yaml` in your experiment repo.
3. `/sync` to push code to the cluster.
4. `/submit` to launch the sweep.
5. `/monitor` to track progress.
6. `/aggregate` to collect results locally.

## The `hpc` Python Package

The `hpc` package (`hpc/`) provides the programmatic layer beneath the slash commands.

### Key modules
| Module | Responsibility |
|--------|---------------|
| `hpc._config` | Load and validate `clusters.yaml` and `project.yaml` |
| `hpc.remote` | SSH/rsync wrappers for running commands and syncing files on clusters |
| `hpc.lifecycle` | Job submission, monitoring, and cancellation logic |
| `hpc.gpu` | GPU type resolution and resource-request formatting |
| `hpc.backends/` | Scheduler-specific adapters (SGE, SLURM) for script generation and status parsing |

### Templates
Job script templates live in `templates/sge/` and `templates/slurm/`. They are Jinja-style templates rendered by the backends at submission time.

### Installation
```bash
pip install -e .          # editable install
pip install -e ".[dev]"   # with ruff, mypy, pytest
```
