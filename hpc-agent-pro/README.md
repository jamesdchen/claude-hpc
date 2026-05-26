# hpc-agent-pro

Scheduling-strategy and queue-wait forecasting plugin for `hpc-agent`.

`hpc-agent` ships the job-execution surface — submit, monitor,
aggregate, campaign, resubmit. `hpc-agent-pro` adds the advisory layer
on top: queue-wait forecasting, submit-time planning, and walltime
right-sizing. It plugs in through the `hpc_agent.plugins` entry-point
group — installing the package is the entire opt-in; with it absent,
`hpc-agent` runs as a pure execution tool.

## What it adds

Installing this package registers these commands into the `hpc-agent`
CLI:

- `plan-submit` — score candidate submit constraints
- `predict-queue-wait` / `predict-start-time` — forecast when a job starts
- `best-submit-window` — rank upcoming submit windows
- `inspect-cluster` — per-node cluster snapshot
- `runtime-prior` — quantile rollup of past task runtimes
- `validate` — pre-submit `--test-only` timing probe
- `walltime-drift` / `house-edge` / `recommend-wait-alternative`

It also enables `resubmit` auto-right-sizing (cold-start memory buffer,
walltime arbitrage, daisy-chain) in place of the public package's
verbatim-override behaviour.

## Install

`hpc-agent` must be installed first — the plugin pins a compatible host
range:

```
pip install hpc-agent
pip install hpc-agent-pro
```

The plugin is discovered automatically; no configuration required.

### Optional: snapshot cron (for the LightGBM-residual predictor)

The `predict-start-time` primitive's LightGBM-residual model needs
queue-snapshot history (~7-14 days before useful). To accumulate it
automatically, install the snapshot + training cron:

```bash
hpc-agent install-cron --ssh-target alice@cluster.example.edu --experiment-dir .
```

The primitive installs two crontab entries idempotently — snapshot
every 5 minutes, training daily at 03:00 — both fingerprinted by their
target module so re-running detects existing entries and skips. The
three cron-invoked modules (`snapshot_squeue`, `train_wait_predictor`,
`extract_sacct_history`) live under `hpc_agent_pro._cron/` and ship
in this wheel, so the cron lines work in any pip-installed environment
— no source checkout required. The predictor still works in floor-only
mode (no LightGBM residual) when no model has been trained.

## Development

This package lives in the `hpc-agent` monorepo under `hpc-agent-pro/`,
built and tested independently of the host package:

```
cd hpc-agent-pro
uv run --extra dev pytest
uv build
```

Design notes for the forecasting internals — the queue-wait predictor
model and its architecture — are under [`docs/`](docs/).
