`/submit-hpc` triggers the **submit** workflow — submit a parameter-grid experiment to an HPC cluster, with `hpc-agent-pro`'s planner-aware constraint scoring (Step 4c: `score-submit-plan`, walltime/footprint right-sizing, co-tenant-aware exclusion) layered on top of the host's submit pipeline.

This command is a thin trigger over `hpc-agent run`, the code-orchestrated entrypoint. Do not run the `hpc-submit` skill, and do not perform the workflow steps yourself in this conversation — the workflow runs in a fresh-context worker. The plugin's overriding `hpc-submit` skill (`skills/hpc-submit/SKILL.md`) is the canonical SoT and reaches the worker via the prompt-renderer's plugin-lookup path.

1. Structure the user's request into a JSON object `<fields>` — the run or notebook to submit, plus any explicit choices they stated (`cluster`, `--no-canary`, `campaign_id`). No up-front interview is needed; pass whatever the user gave.
2. Run, via the `Bash` tool: `hpc-agent run submit --fields-json '<fields>'`. It validates the fields, generates the canonical worker prompt by code, and spawns a fresh-context worker that executes the `hpc-submit` skill. It prints a JSON envelope.
3. Surface to the user: `data.report.result` (run id, job ids, grid dimensions, verified scheduler state, chosen constraint + rationale), `data.report.decisions` (each judgement point the worker reached and why — including the planner-aware ones: `co_tenant_exclusion`, `submit_now_vs_wait`, `walltime_split_confirm`), and `data.report.anomalies`.
4. If a decision is an **escalation** — a cluster pick, an axis classification, a scaffolding interview the worker can't complete headless, a co-tenant exclude/allow per stressed node, a submit-now-vs-wait choice when the predictor returns a non-zero offset, a `walltime_split` confirm because the executor must checkpoint — ask the user, add it to `<fields>`, and run `hpc-agent run submit` again. A fresh, unscaffolded experiment may take two round-trips.

## Common failure modes (orchestrator quick reference)

| Symptom | Cause | Fix |
|---|---|---|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) > 30 min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| `ModuleNotFoundError` | Env not set up | Check modules and `conda_env` |
| rsync/scp failure | SSH key issue | `ssh $SSH_TARGET hostname` first |
| `--features` not recognized | Executor doesn't support that arg | Check `--help`, update executor |

When the user mentions CLI arguments the executor doesn't accept (e.g. "sweep features=[har, pca]" but `--features` isn't in `--help`), surface it: "ml_ridge.py doesn't accept `--features`. Should I add it, or did you mean a different executor?"
