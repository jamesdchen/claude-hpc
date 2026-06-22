# Proposal: the orchestrator ↔ backend boundary

Status: **proposal**. This page proposes finishing the boundary between
hpc-agent's *agentic orchestrator* (submit / monitor / aggregate / campaign
control) and the *execution backends* it drives (SGE / SLURM / PBS over SSH,
GitHub Actions and crowd-compute over pure API). It is the altitude above
[`crowd-compute-backend.md`](crowd-compute-backend.md): that proposal opened the
backend *seam* (config validation + construction); this one closes the remaining
*leaks* so the orchestrator depends only on the backend contract — no hardcoded
scheduler list, no assumption that every backend speaks SSH over a shared
filesystem. Increment 1 has landed (see below); the rest is staged.

It is the repo's own rule — *"core dispatches, never branches"* and the
four-question boundary test in
[`engineering-principles.md`](../internals/engineering-principles.md) — applied
at a coarser grain: the orchestrator is to backends what core is to a knowledge
package.

## Why

A backend that is not an SSH login node with a shared filesystem cannot today be
made a first-class citizen of submit / monitor / aggregate, even though the
construction seam already accepts it. The GitHub Actions plugin
([`examples/plugins/hpc-agent-github-actions/`](../../examples/plugins/hpc-agent-github-actions/))
made this concrete: it dispatches, polls, and pulls results through backend
hooks, but the orchestrator reaches *past* those hooks in ~a dozen places —
hardcoded scheduler enums reject the backend's name before it is constructed,
and the submit/monitor/aggregate flows assume `ssh_run` + `rsync`. The symptom
is a long tail of `if scheduler == …` / `Literal["sge", …]` sites; the cause is
that the orchestrator knows the *identity* and *transport* of its backends
instead of only their *contract*.

## The boundary already half-exists

The mechanism is in place; only the discipline is missing.

- **Contract:** `hpc_agent.infra.backends.HPCBackend` — an abstract base widened
  (B5) precisely so callers dispatch on capability hooks instead of scheduler
  branches.
- **Registry + discovery:** `register(name)` / `get_backend_class(name)` /
  `registered_backend_names()` (the last imports plugin `primitive_modules` for
  their `@register` side effect).
- **Construction seam:** `HPCBackend.from_build_context(BackendBuildContext)`
  (landed in `crowd-compute-backend.md`), which hands a registered backend every
  factory input and lets it ignore the SSH-shaped ones.

This is, almost exactly, Toil's `AbstractBatchSystem` + its registry +
`toil_batch_system_*` external-discovery convention, and the same shape as
Parsl's one-class-per-provider model. We are already on the mainstream design
(see Prior art). What remains is to stop the orchestrator from bypassing it.

## Prior art (and why *in-repo boundary first*, not a repo split)

A survey of how comparable systems split orchestrator from execution backend
(full cited findings available on request) converges hard on one sequence:
**establish the boundary in one package; extract into separate packages only
when a concrete trigger forces it.**

- **The contract is small everywhere.** Parsl's `ExecutionProvider` is
  `submit`/`status`/`cancel`; Toil's `AbstractBatchSystem` is four methods;
  Dask-Jobqueue delegates only the submit/cancel commands + directive header;
  Nextflow selects an executor by one config string. All keep task routing /
  dataflow in the core and delegate only *job lifecycle*. All ship their
  first-party backends **in-repo**.
  ([Parsl](https://raw.githubusercontent.com/Parsl/parsl/master/parsl/providers/base.py),
  [Toil](https://raw.githubusercontent.com/DataBiosphere/toil/master/src/toil/batchSystems/registry.py),
  [Nextflow](https://www.nextflow.io/docs/latest/executor.html))
- **Extraction is expensive and is done late.** Snakemake 8 (Dec 2023) moved
  executors to separate `snakemake-executor-plugin-*` packages and shipped a real
  regression in the freshly-split SLURM plugin — GPU `--gres=gpu:1` rejected
  ([snakemake#2701](https://github.com/snakemake/snakemake/issues/2701)). Airflow
  split providers into 140+ independently-versioned packages but **deliberately
  kept a single monorepo to avoid a "distributed monolith"** (maintainer
  retrospective:
  [modern-python-monorepo](https://pydevtools.com/blog/fosdem-talk-modern-python-monorepo/)).
  Kubernetes kept in-tree cloud/CSI as the default for **~5 years** of dual
  maintenance before removing code, and only after external drivers were
  CI-default and production-proven
  ([KEP-2395](https://github.com/kubernetes/enhancements/blob/master/keps/sig-cloud-provider/2395-removing-in-tree-cloud-providers/README.md)).
- **The triggers that justified extraction** were: independent/fast release
  cadence (Airflow cloud providers), an unbounded third-party long tail
  (Snakemake schedulers, Terraform's provider registry), heavy/optional deps you
  won't put in the core install (Parsl), or security blast-radius (k8s). **"We
  have two backends and want them tidy" is not on that list.**
- **The principles agree.** Ports & Adapters gives the decoupling *inside one
  package* (the core owns the port; "multiple adapters per port" is the normal
  case). The rule of three (Fowler/Roberts) and Metz's *"duplication is far
  cheaper than the wrong abstraction"* both warn that a port extracted from a
  half-built boundary + two backends is the prime wrong-abstraction candidate.
  *Honest caveat:* "monolith-first" is contested — Fowler published a rebuttal
  ([Don't start with a monolith](https://martinfowler.com/articles/dont-start-monolith.html))
  on his own site — but that rebuttal argues for getting module boundaries right
  early, which is exactly the in-repo work proposed here, not a reason to split
  repos.

**Conclusion for this proposal:** finish the in-repo boundary, enforce it with a
lint, and treat a physical package split as a *later, trigger-gated* decision
(see "When to extract").

## The contract (the port)

What the orchestrator is allowed to know about a backend. Most exists; the two
marked **(new)** landed in increment 1.

| Concern | Contract surface |
|---|---|
| Construct | `from_build_context(BackendBuildContext)` |
| Submit | `_build_command` · `_execute_command` · `JOB_ID_REGEX` · `resource_flags` · `_setup_log_dir` · afterok: `supports_afterok` / `_build_afterok_dependency_flag` |
| Liveness / state | `alive_job_ids` · `classify_scheduler_state` · `query_jobs` |
| Results / logs | `fetch_results` **(new)** · `fetch_logs` **(new)** · `stderr_log_path` |
| Capabilities | `requires_ssh` **(new)** · `scheduler_name` · `template_ext` · `supports_test_only_eta` |

One contract defect to fix as we go: several hooks are **staticmethods**
(`build_alive_check_cmd` / `parse_alive_output` / `build_scheduler_state_cmd` /
`stderr_log_path`) — an SSH-era shape that a pure-API backend *cannot* implement,
because a `@staticmethod` can't hold an authenticated client. The instance
methods (`alive_job_ids`, `fetch_results`, `fetch_logs`) are the correct shape
and already supersede them; the migration should route the orchestrator through
the instance methods and let the staticmethods fade.

## The leaks (what "closing the boundary" means)

Three classes, from the seam audit. Cited by symbol (not line) per the repo's
own anti-drift stance.

**Class A — enumeration** (the orchestrator hardcodes the backend *list*):
- `_wire/_shared.Scheduler` and `_wire/_shared.BackendName` — `Literal["sge",
  "slurm", "pbspro", "torque"]`; a plugin backend fails wire validation before
  construction.
- `ops/monitor/reconcile.py` `--scheduler` argparse `choices=(…)`.
- `ops/aggregate_preflight._SCHEDULERS`, `ops/scaffold_spec._BACKENDS`,
  `meta/campaign/deterministic_resolver` (validates the resolved name against the
  four), `_kernel/extension/capabilities.supported_schedulers`.

**Class B — transport** (the orchestrator assumes SSH + shared filesystem):
- *Submit:* `ops/submit_flow._run_shared_prelude` (ssh probe / `command -v uv` /
  `rsync_push` / `deploy_runtime`) — **branched in increment 1**;
  `ops/submit_preflight` `check-preflight`; `ops/preflight/check` cluster probes.
- *Monitor:* `ops/monitor/status._ssh_status_report`,
  `ops/monitor/reconcile._ssh_alive_job_ids` / `_reconcile_one`,
  `ops/monitor/logs_atom` + `infra/cluster_logs.fetch_task_logs` (ssh `tail`).
- *Aggregate:* `ops/aggregate_flow` — `_ensure_combined_waves` (combine over
  ssh), `_pull_and_validate_combiner` / `_cluster_final_reduce` / the summaries
  pull (`rsync_pull`).

**Class C — identity branches:** residual `if scheduler == …` ladders that
should read a capability or dispatch through the contract.

A subtlety the [WS4 contract](ws4-design-decisions.md) pins: the SSH verbs keep
`cli.requires_ssh=True` and their `ssh_run`/`rsync` calls (correct for the SSH
path); pure-API support is a **runtime branch added beside** the SSH path, never
a removal of it. `tests/contracts/test_requires_ssh_consistency.py` enforces
this and must stay green through every increment.

## Plan: staged increments

Each increment is independently committable, backward-compatible (built-in SSH
backends unchanged), and dormant for plugin backends until Class A is closed.

1. **Capabilities + the artifact hooks — LANDED.**
   `HPCBackend.requires_ssh` (default `True`), `fetch_results` / `fetch_logs`
   instance hooks (default `NotImplementedError`), and `backend_requires_ssh(name)`
   (reads the capability off the class via `registered_backend_names()` without
   constructing the backend). github-actions plugin sets `requires_ssh = False`.
   Submit prelude branches on it. Tests:
   `tests/infra/backends/test_requires_ssh_capability.py`,
   `tests/ops/test_submit_flow_pure_api.py`.
2. **Close Class A (enumeration).** Replace the `BackendName` / `Scheduler`
   `Literal`s and the hardcoded tuples / argparse `choices` with one
   `registered_backend_names()`-backed validator. Backward-compatible (the four
   built-ins still validate); a plugin backend becomes *expressible* as a spec —
   which unblocks the dormant increment-1 branch end-to-end.
3. **Close Class B — submit / preflight.** `check-preflight` and the cluster
   probes skip the SSH echo / uv checks for a `requires_ssh=False` backend
   (report "skipped: pure-API" rather than probing).
4. **Close Class B — monitor.** `status` / `reconcile` / `logs` obtain liveness
   via `backend.alive_job_ids` + `classify_scheduler_state` and logs via
   `backend.fetch_logs`, instead of `_ssh_*`. Requires constructing the backend
   in these ops (via `from_build_context` off the run record) — a new but
   declared dependency.
5. **Close Class B — aggregate.** For `requires_ssh=False`, fetch per-task
   results via `backend.fetch_results(run_id, dest)` and reduce locally with
   `execution.mapreduce.reduce`, skipping wave-combine + `rsync_pull`.
6. **Enforce the seam.** Extend the import-boundary lint
   (`scripts/lint_library_knowledge.py` is the precedent) so the orchestrator
   package cannot import a concrete backend module — the boundary's guarantee
   made mechanical, the "fitness function" that keeps Class A/C from recurring.

## Capability model

Prefer **explicit, declared capability flags** over duck-typed probing — the
validated pattern across CSI (capability RPCs), Airflow (`BaseExecutor`
booleans), and libcloud (a `features` dict that rejects unsupported ops *loudly*
with `LibcloudError`). `requires_ssh` is the first such flag; add narrowly-scoped
peers (e.g. `has_shared_filesystem`) only as a specific leak demands one. Avoid
`hasattr(backend, "fetch_results")`-style detection: `@runtime_checkable` /
`hasattr` is signature-blind ([PEP 544](https://peps.python.org/pep-0544/)) and
silently wrong when a method is present but mis-shaped. An unsupported operation
should fail loud, matching the `NotImplementedError` capability-hook convention.

## When to extract into separate packages

**Not yet.** Per the prior art, defer a physical split until a trigger fires:

- a backend needs heavy/optional dependencies that don't belong in the core
  install (the Parsl / Snakemake reason — and the four-question boundary test's
  Q4: platform-SDK correctness is only testable with the SDK, so it belongs in a
  plugin whose CI carries it);
- a backend's release cadence genuinely diverges from core (the Airflow
  providers reason); or
- we begin accepting third-party / out-of-tree backends (the Toil
  `toil_batch_system_*` / Terraform-registry reason).

When a trigger does fire, copy the proven playbook: a separately-SemVer'd
interface package (Snakemake's `snakemake-interface-executor-plugins`), a single
monorepo if possible (Airflow), the in-tree path kept as the default through a
dual-maintenance window with a compatibility shim (Kubernetes). The plugins
under [`examples/plugins/`](../../examples/plugins/) already prove the registry
path end-to-end, so this is a packaging decision, not an architecture one.

## Risks / alternatives considered

- **Widen only the loop's path** (touch just the github-actions surfaces): leaves
  the framework half-widened — some surfaces accept plugin backends, others
  reject them — a confusing partial state. Rejected in favor of closing Class A
  uniformly.
- **Split repos now:** manufactures a distributed monolith (lockstep releases)
  from two backends and a half-built boundary — the exact wrong-abstraction risk
  the rule of three warns about. Deferred behind explicit triggers.
- **Construct a backend in monitor/aggregate (increments 4–5):** adds a new
  dependency from those ops onto `from_build_context`. Accepted: it is the
  declared construction seam, and it replaces an *undeclared* dependency on
  `ssh_run` + a shared mount.
- **Blast radius of Class A:** the `BackendName` widening touches wire validation
  on every submit. Mitigated by being purely additive (the four built-ins keep
  validating) and by the contract test suite; it is a guard the
  `engineering-principles.md` "verify before widening" rule says to confirm
  intentful, which the github-actions case does.

## References

- [`crowd-compute-backend.md`](crowd-compute-backend.md) — the construction /
  config seam this builds on.
- [`ws4-design-decisions.md`](ws4-design-decisions.md) +
  `tests/contracts/test_requires_ssh_consistency.py` — the `requires_ssh`
  catalog-flag contract.
- [`engineering-principles.md`](../internals/engineering-principles.md) — "core
  dispatches, never branches"; the four-question boundary test; verify-before-
  widening; cite-sources-not-line-numbers.
- Prior art: Parsl, Toil, Dask-Jobqueue, Nextflow (backend designs); Snakemake 8,
  Airflow AIP-8 / AIP-51, Kubernetes KEP-2395 / CSI (extraction post-mortems);
  CSI / Airflow / libcloud (capability negotiation); Ports & Adapters, the rule
  of three, monolith-first and its rebuttal (principles).
