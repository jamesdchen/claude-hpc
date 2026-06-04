# Escalation funnel — remaining flow-wiring seams

The escalation funnel (#230 → #231 → #234, plus #232's verify slice) landed as
five tested primitives on `claude/escalation-funnel`:

| primitive | issue | module |
|---|---|---|
| `Escalation` block (decision-as-data) | #231 | `_wire/fixtures/escalation.py` |
| `pending_verdict` holding state | #231/#234 | `state/journal.py`, `state/index.py` |
| context-keyed `resolve()` | #234 | `ops/recover/resolve.py` |
| service Tier 1 (passthrough + escalation) | #231 | `ops/recover/service.py` |
| manifest + verify-against-manifest | #232 | `ops/transfer/manifest.py` |

Each is a clean unit the *existing flows* can call. This note tracks the
remaining **flow-wiring seams** — connecting these primitives into the live
submit / campaign / aggregate pipelines — and why each is or isn't done yet.

## DONE — service-env export loop (#231 Tier 1)

`build_submit_spec` stamps a spec's `service_env` into `job_env` as the JSON
`HPC_SERVICE_ENV` var; the cluster-side dispatcher reads it and injects
`HPC_SERVICE_<KEY>` into every task env. The loop is closed end-to-end and
tested (`tests/incorporation/build/test_submit_spec.py::test_service_env_*`).

Trivial follow-on (not blocking, low value until a real service sweep exists):
thread `service_env` from the submit spec through `submit_flow` into
`write_run_sidecar(service_env=...)` so the address is also *recorded* on the
sidecar for resume/provenance (the field already exists, Phase 3).

## DEFERRED (ambiguous placement) — resolver → escalation in the recover path (#234)

**Seam:** the auto-retry decision point should call `resolve(failure_features)`
per failure cluster and route the verdict: `decided_by="code"` →
`resubmit_flow` with the refined overrides; `decided_by="judgement"` →
`mark_pending_verdict(run_id, escalation=...)` (park) + surface the escalation
block, so the campaign loop keeps progressing on unaffected work and treats
held runs as not-done (`find_held_runs`).

**Why not done:** unblocked by data, but the *placement* is an architectural
choice, not a mechanical wire:
- Where `resolve()` plugs in — `monitor_flow` auto-retry, `cli/status`'s
  resubmit decision, or a campaign-driver step — each implies a different
  owner for the decision and a different auto-act policy (auto-resubmit on
  `code` vs. always surface for confirmation).
- Building `failure_features` per cluster (the #230 vector) from
  `cluster_failures_by_fingerprint` output + the journal's `retries`/sidecar
  context is itself new glue with several reasonable shapes.

This wants a deliberate decision on the auto-act boundary before wiring. The
primitives (`resolve`, `mark_pending_verdict`, `find_held_runs`, the
`Escalation` block) are all ready; only the routing policy is open.

## BLOCKED (data profile) — inline rsync → stage-in/out brackets (#232)

**Seam:** extract the inline `rsync_push` (submit-flow) / `rsync_pull`
(aggregate-flow) into composable `stage-in` / `stage-out` bracket primitives
that build a manifest before transfer and `verify_manifest` after, replacing
the current exit-code-only check.

**Why not done:** the bracket *shape* is profile-dependent and #232's open
question (one shared dataset vs per-task node-local shards vs stage-out-heavy)
is unanswered — see the tradeoff note on issue #232. The profile-independent
core (`build_manifest` / `verify_manifest`, and `VerifyReport.failure_features`
routing a bad transfer into the escalation path) is already built and tested;
only the per-profile extraction waits on the answer.

## DEFERRED BY DESIGN (no trigger yet)

- **#234 memoization** of LLM verdicts into `recall` — wait until the
  `decided_by` tally (`resolve.tally_decisions`) shows a signature recurring in
  `judgement`. Building it earlier risks caching a stale/misapplied verdict
  (must key on the discriminated tuple + gate on `temporal_context`).
- **#231 Tier 2** service lifecycle (`up_command`/`health_check`/`teardown`) —
  wait for a user who actually needs the framework to manage the service
  process, not just consume it.
