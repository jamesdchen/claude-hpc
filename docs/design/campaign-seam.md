# Design: the strategy-agnostic campaign seam

> **Status:** proposal. Tracks [#218](https://github.com/jamesdchen/hpc-agent/issues/218).
> This describes a *target* design, not current behaviour. For what
> exists today see [`docs/workflows/campaign.md`](../workflows/campaign.md)
> and [`docs/internals/campaign-lifecycle.md`](../internals/campaign-lifecycle.md).

## Problem

The campaign loop is the substrate for *closed-loop* experiments: submit
→ observe → decide → submit again. The obvious integration is
hyperparameter optimisation (Optuna/Ax) via ask-tell. The trap is to
generalise *from* ask-tell — e.g. by adding a privileged typed
`objective: float` channel to the framework. That over-fits to one
campaign class and demotes every other to second-class.

The framework must stay **experiment-agnostic**: exactly the property
that makes `tasks.py`'s `resolve(i)` boundary work — the framework calls
it and moves the bytes; the experiment repo owns all meaning.

## Campaign classes, and why a scalar objective is the wrong primitive

"Ask-tell HPO" silently bundles three independent assumptions. Different
campaign classes violate different ones:

| Class | Decision driver | State carried between iterations | Early-kill? |
|---|---|---|---|
| HPO (Optuna/Ax/grid/random) | scalar/vector objective | scalars + token | no |
| Walk-forward / rolling backtest | **deterministic schedule — no objective** | a counter | no |
| Convergence / Monte-Carlo | a **statistic** over accumulated results | running estimate | no |
| Multi-objective / Pareto (NSGA-II) | objective is a **vector / frontier** | population + token | no |
| Population-Based Training | fitness **+ clone-and-perturb** | **checkpoints (artifacts)** | partial |
| RL self-play / iterative distillation | improves a model from **its own outputs** | **weights + replay buffer / corpus** | no |
| Active learning / data acquisition | model uncertainty → **which points to label** | **growing labeled set** | no |
| Hyperband / ASHA / async-PBT | intra-run intermediate values | scalars | **yes** |
| Multi-stage pipeline | sequential dependency | data between stages | no |

Three things break:

1. **No objective exists** — walk-forward, curriculum (driven by a schedule/count).
2. **The objective isn't a scalar** — Pareto vectors; or a statistic like accumulated variance.
3. **The carried state is an artifact, not a number** — PBT checkpoints, RL
   replay buffers, active-learning label sets, distillation corpora. This is
   the common, high-value case for autonomous-research-agent workloads.

A privileged scalar objective serves only the first row. It would make
the artifact-carrying and schedule-driven classes second-class — the
opposite of agnostic.

## What the framework already gets right

Today's design is *already more general than Optuna* and must not regress:

- `prior(experiment_dir, campaign_id)` returns **opaque per-iteration
  reduced-metric dicts** — arbitrary shape, no ascribed meaning.
- `campaign_dir()` reserves `.hpc/campaigns/<cid>/` for **arbitrary**
  strategy state (Optuna SQLite, PBT checkpoints, walk-forward cursor).
  The framework writes nothing inside.

## The seam: three universal pieces, zero objective concept

### 1. `trial_token` — opaque round-trip

Promote today's `_optuna_trial_number` leading-underscore convention to a
first-class field on the submit spec / `resolve()` return. The framework
guarantees it is (a) carried into the run sidecar verbatim and (b)
re-exposed paired with that iteration's results — and **never
interpreted**. It is bytes.

| Strategy | What it puts in `trial_token` |
|---|---|
| Optuna / Ax | `trial.number` |
| PBT | `(member_index, generation)` |
| Active learning | acquisition-batch id |
| Walk-forward | nothing (windows self-identify) |

### 2. Campaign-iteration dedup salt

`cmd_sha` is the SHA-256 of the materialised task list, which makes
re-submits dedup automatically — correct for a static `tasks.py`, a
footgun for any campaign that *deliberately* re-runs equal params
(Monte-Carlo accumulation, RL same-hyperparams-per-generation, the
documented stochastic-HPO collision in
[`campaign.md`](../workflows/campaign.md)). Fix it in the framework:
salt `cmd_sha` with the campaign-iteration ordinal (`len(prior)`) for
campaign-tagged submits, so iteration N never dedups against iteration M.

This frees `trial_token` to be *purely* a reconciliation token instead of
doubling as a dedup-buster the user has to hand-inject into `resolve()`.
Coordinate with [#207](https://github.com/jamesdchen/hpc-agent/issues/207)
(cmd_sha param-identity semantics).

### 3. Artifact / result-dir lineage in `prior()`

Have `prior()` additionally expose each past iteration's `result_dir`
paths (it already runs `reduce_metrics` over them — it has them). This
single addition unlocks the artifact-carrying classes:

- **PBT** — locate the checkpoint to clone.
- **RL self-play** — locate the previous generation's replay buffer.
- **Active learning** — locate the prior label set to extend.
- **Distillation** — locate the generated corpus.

Still fully opaque: the framework hands back paths; the strategy decides
what's inside.

### The objective stays a user-owned metrics key

There is no framework `objective`. HPO reads `metrics["val_loss"]`;
convergence reads `metrics["estimate"]` and computes its own variance;
walk-forward reads nothing. The framework never knows which key (if any)
is "the objective", nor the optimisation direction.

## Proposed `prior()` return shape

```python
# prior(experiment_dir, campaign_id) -> list[IterationRecord]  (oldest-first)
{
    "run_id": "…",
    "trial_token": <opaque, round-tripped from resolve()>,   # may be null
    "status": "complete" | "failed" | "timeout" | "abandoned",
    "metrics": {…},          # opaque reduced-metric dict (today's payload)
    "result_dirs": ["…"],    # NEW: per-task output dirs for artifact lineage
}
```

Everything except `result_dirs` exists today; the addition is additive
and back-compatible.

## Worked examples

### Optuna (scalar objective lives in a metrics key)

```python
study = optuna.create_study(
    storage=f"sqlite:///{campaign_dir()}/optuna.db",
    study_name=CID, direction="minimize", load_if_exists=True,
)
for past in prior(".", CID):                       # framework-supplied pairing
    study.tell(past["trial_token"], past["metrics"]["val_loss"])
def total():   return 0 if len(prior(".", CID)) >= MAX else 1
def resolve(i):
    t = study.ask()
    return {**t.params, "trial_token": t.number}   # framework round-trips, never reads
```

No executor-side `study.tell`, no `score_iter.py` helper, no `__import__`
hack — the rough edges in today's Recipe 2 disappear. (See
[#219](https://github.com/jamesdchen/hpc-agent/issues/219) for shipping a
tested scaffold.)

### Synchronous PBT (artifact lineage, no scalar-objective channel)

```python
past = prior(".", CID)
if past:
    ranked = sorted(past, key=lambda r: r["metrics"]["fitness"], reverse=True)
    survivors = ranked[: POP // 2]                 # truncation selection
    next_pop = [clone_and_perturb(r["result_dirs"][0]) for r in survivors] * 2
else:
    next_pop = [fresh_member(m) for m in range(POP)]
def total():   return 0 if generation(past) >= MAX_GEN else POP
def resolve(i):
    m = next_pop[i]
    return {**m.hparams, "init_ckpt": m.ckpt_path, "trial_token": (m.member, m.generation)}
```

`fitness` is just a key; `result_dirs` carries the checkpoints. The
framework imports neither an optimiser nor a notion of "fitness".

## Out of scope (deliberate exclusions)

- **Early-kill (Hyperband / ASHA / async-PBT)** — requires terminating
  running trials, colliding with the no-`scancel` invariant
  ([CONTRACT.md §Cancel/abort](../integrations/CONTRACT.md)). Synchronous
  "finish then select / don't-propagate" variants fit; async early-kill is
  a separate decision ([#228](https://github.com/jamesdchen/hpc-agent/issues/228)).
- **True DAG pipelines** — inter-stage dependency is Snakemake/Nextflow's
  job. A campaign is *iteration*, not a pipeline.

## Related issues

- [#218](https://github.com/jamesdchen/hpc-agent/issues/218) — this design (tracking)
- [#219](https://github.com/jamesdchen/hpc-agent/issues/219) — tested Optuna + PBT scaffolds
- [#207](https://github.com/jamesdchen/hpc-agent/issues/207) — cmd_sha semantics (dedup-salt coordination)
- [#228](https://github.com/jamesdchen/hpc-agent/issues/228) — early-kill vs no-scancel
