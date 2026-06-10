# Proposal: the experiment-agnostic DAG kernel

Status: identity prototype landed (`compose_node_sha` in
`hpc_agent.state.run_sha` + property suite
`tests/state/test_node_sha_properties.py`). Topology, readiness, and
lineage wiring deferred — specified below, not implemented. Nothing is
wired into submit/dedup yet; landing this prototype changes no existing
run's identity (0-parent degeneracy, see "Recursive identity").

## Problem

[`campaign-seam.md`](../design/campaign-seam.md) deliberately excludes
"true DAG pipelines" ("Snakemake/Nextflow's job. A campaign is
*iteration*, not a pipeline"). That exclusion is about scope, not
possibility — but as written it leaves no record of *what* an in-scope
DAG layer would be if the exclusion were ever revisited, which invites
two failure modes:

1. A future feature request ("propagate stage N's outputs into stage
   N+1") gets answered with experiment-specific machinery (a privileged
   "posterior" field, typed stage names) that fails the
   [four-question boundary test](../internals/engineering-principles.md)
   (Q1: substrate, not semantics).
2. The pieces that are *already* agnostic and present — `prior_records()`
   artifact lineage, journal-authoritative terminal lifecycle, canonical
   content-hash identity — get re-invented instead of generalized.

This proposal records the residue: apply the boundary test to inter-run
dependency and keep exactly what survives. The answer is four pieces,
three of which exist in linear (campaign) form. One — recursive
identity — existed in no form, and is the prototype this proposal lands,
because without it the other three are unsafe to build: memoized resume
over a run graph that keys nodes by bare `cmd_sha` silently reuses a
stale child when an ancestor's params change.

## The kernel (everything that survives the boundary test)

| Piece | Core knows | Status |
|---|---|---|
| Partial order | node = a submit spec; edge = "before". Pure graph structure. | missing — campaign order is linear (`prior_records` index) |
| Readiness | "every parent reached an authoritative terminal lifecycle" (journal, not the filesystem `complete` flag) | exists per-run (`mark-run-terminal`, `monitor-flow` wait-terminal); missing the ∀-parents quantifier |
| Lineage | hand a node its parents' `run_id`s + `result_dirs` as opaque paths | exists linearly (`prior_records()["result_dirs"]`); list → set-of-parents adds no semantics |
| Recursive identity | `node_sha = H(canonical({node: cmd_sha, parents: sorted(set(parent node_shas))}))` | **landed** — `compose_node_sha`, unwired |

Everything outside the table is irreducibly caller-owned and must stay
out of core:

- **Edge meaning.** Core hands paths across an edge; format adaptation
  and validity checks are the experiment's. An edge is a set of opaque
  strings — exactly the `prior_records` discipline ("the framework hands
  back paths; the strategy decides what's inside").
- **Conditional topology.** "Only fan out if upstream converged" needs
  no predicate language: a node's `tasks.py` reads its parents' opaque
  artifacts and materializes `total() == 0` to veto itself. Campaigns
  already converge this way; a DAG node vetoing is the same convention
  at the only place user code already runs.
- **Stage vocabulary.** No stage names, no "objective", no typed
  inter-stage payloads.

## Recursive identity (the landed prototype)

`compute_cmd_sha` is parameter identity for a single run (#207). It does
not compose: if run B consumed run A's outputs and A's params change,
B's `cmd_sha` is unchanged, so a resubmit of B dedups against a result
computed from a *different* A. The Make/Nextflow `-resume` property —
never reuse a node whose ancestry changed — is expressible purely in
hashes, which makes it substrate by the boundary test (Q1: hashing and
key-sorting, no parameter meaning; Q3: stdlib-only; Q4: testable with
synthetic digests).

`compose_node_sha(cmd_sha, parent_node_shas)` is the Merkle step. Pinned
properties (the property suite is the contract while the function is
unwired):

- **0-parent degeneracy**: `compose_node_sha(c, []) == c`. Every
  existing run is a 0-parent node; today's dedup keys, sidecars, and
  journal entries need no migration.
- **Parents are a set**: order-invariant, duplicate-insensitive.
- **Ancestor propagation**: a grandparent change propagates through the
  parent digest into the child (tested transitively).
- Parameter identity, not code identity: parents fold in their
  *params*' digests, never executor bytes — the same #207 boundary as
  `cmd_sha`, including the `invalidate_on_code_change` opt-in story.

The campaign-iteration dedup salt (seam piece 2) is in hindsight a
degenerate case: salting identity with position-in-a-linear-order. The
general form salts with the identity of what the node depends on.

## Wiring plan (deferred, in dependency order)

1. `parents: [run_id, ...]` optional field on the submit spec; resolve
   each parent's recorded node_sha (sidecar), compute this node's via
   `compose_node_sha`, persist `node_sha` + `parent_run_ids` on the
   sidecar (v2 schema is additive-friendly).
2. `find_run_by_cmd_sha` grows a node-aware lookup: dedup keys on
   `node_sha` when parents are declared, bare `cmd_sha` otherwise
   (degeneracy makes these identical for 0-parent submits).
3. Readiness as a `validate`-verb primitive: given `parents`, read the
   journal lifecycle for each; ok iff all terminal-success. Composed by
   `submit-pipeline` the way `validate-campaign` is — independently
   skippable when no parents are declared.
4. Lineage injection: parents' `result_dirs` exposed to `tasks.py` the
   way `HPC_CAMPAIGN_ID` + `prior_records` are today (an env var naming
   the parent run_ids; a `parent_records(experiment_dir, run_ids)`
   accessor — a filter of existing sidecar reads, no SSH).
5. Topology stays caller-side. The agent surface (or an external
   orchestrator) walks the graph and fires submits as readiness allows —
   consistent with the campaign driver's on-disk-state-only design. A
   framework-side graph *runner* is out of scope until repeated
   mechanical agent walks justify a composite, per the
   `submit-pipeline`/`campaign-run` precedent.

## Non-goals

- No scheduler-native DAG features (`qsub -hold_jid`, SLURM
  dependencies): readiness must consult the journal lifecycle and
  aggregation state, not just scheduler exit — and cross-cluster edges
  exist. Scheduler holds also collide with the no-`scancel` invariant's
  "stop polling and let it expire" abandonment story.
- No retry/backfill policy at the graph level (mirrors the campaign
  loop's deliberate no-auto-retry stance).
- No early-kill interaction (#228 unchanged).
- Nothing in this proposal privileges any experiment vocabulary — a PR
  adding a typed inter-stage payload fails review by Q1 regardless of
  what this doc says.

## Open questions

1. Should `node_sha` fold in the parent edge's *selection* (which subset
   of a parent's `result_dirs` the child reads)? Current answer: no —
   selection is edge meaning, caller-owned; the child's `tasks.py`
   materializes whatever it selected into its own kwargs, which `cmd_sha`
   already hashes.
2. Does a parent's `tasks_py_sha` participate via
   `invalidate_on_code_change=True` transitively? Deferred with the same
   opt-in default as single-run dedup.

## Related

- [`campaign-seam.md`](../design/campaign-seam.md) — the exclusion this
  proposal scopes; seam pieces 1–3 (trial_token, iteration salt,
  `prior_records`)
- [`engineering-principles.md`](../internals/engineering-principles.md) —
  the boundary test applied throughout
- #207 — `cmd_sha` param-identity semantics (`node_sha` inherits them)
- #218 — strategy-agnostic campaign seam (tracking)
