# The judgement floor

*Where the framework cannot decide for you — and why that set is small,
named, and metered.*

hpc-agent's job is to take an experiment and run it trustworthily and
"automagically." Almost all of that is deterministic code: primitives with
declared side effects, idempotency, journal/dedup guarantees, and validated
JSON envelopes. The trust lives there. The agentic layer is the fuzzy glue
that makes brittle determinism robust and removes the toil — and the
architecture's standing goal is to keep that glue **thin, typed, reversible,
and shrinking**.

This page is the map of what's left when the glue is as thin as it can be:
the **irreducible judgement points** — the handful of places where a
deterministic primitive legitimately abstains and hands a choice back to the
caller. Everything *not* on this list is either already code or on its way to
becoming code (see [the ratchet](#the-ratchet-how-a-judgement-point-becomes-code)).

This page is **descriptive**. The normative copy is the per-workflow
`decisions` contract enforced by `parse_worker_report` and the regression-gated
meter `scripts/count_llm_touchpoints.py`; cited below, not restated.

## The two-number model

`scripts/count_llm_touchpoints.py` measures, per worker prompt, "how much of
the spine is still narrated in prose for the LLM to execute." It tracks two
quantities with opposite trajectories:

- **`total_touchpoints`** = `branches + stop_gates + retry_loops` — deterministic
  control flow not *yet* absorbed into a workflow composite. The toil-glue.
  **Expected to drop** as composites chain branches/gates/loops into code under
  a single envelope. Re-growing it trips the `--check` gate.
- **`escalation_points`** — "the legitimate LLM residual: the judgement points
  the deterministic layer *cannot* decide." Tracked separately, excluded from
  the total, and **expected to stay.**

> "The goal is to drive `total_touchpoints` down toward the irreducible
> `escalation_points`, not to delete the escalation points themselves."
> — `scripts/count_llm_touchpoints.py`

That sentence is the whole identity in one line: shrink the toil, keep the
judgement. This page enumerates what "the judgement" actually *is*.

(The meter's `escalation_points` is a regex line-count *proxy*. The authoritative
floor is the set of `decisions` points each worker prompt flags as **judgement**
— enforced by `parse_worker_report`, which rejects any unlisted point ID and
requires a non-empty `why` at every judgement point.)

## The floor: seven judgement points

Across all four host workflows, the deterministic layer abstains in exactly
seven places. Each has a primitive that *tries first* and resolves the common
case in code (`decided_by="code"`); only the residual escalates
(`decided_by="judgement"`, `needs_decision=true`).

| Workflow | Judgement point | What's being decided | Adjudicator (tries first) | Resolves in code when… | Residual that escalates |
|---|---|---|---|---|---|
| **submit** | `axis_class` | Is the parallelization axis a plain Cartesian grid, or a stateful series needing warm-up? | `classify-axis-easy` → `classify-axis` | matcher is confident: `independent` / `bounded_halo` / `sequential` / `no_loop_detected` | `unclassifiable` / `function_not_found` → the LLM axis decision tree |
| **status** | `surface` | Snapshot vs wait-until-terminal | caller's `blocking` spawn field | `blocking` is set (it almost always is) | `blocking` absent **and** context unmistakably wants a synchronous wait |
| **status** | `resubmit` | Retry the failed tasks, or accept the failure? | `failures` classification + sidecar `auto_retry` gate | category is recoverable (`oom_killed` / `cluster_timeout` / `node_failure` / `preempted`) and attempts remain | non-recoverable (`spec_invalid` / `executor_crash`) — giving up is the caller's call |
| **aggregate** | `partial_handling` | Some waves failed `combiner_max_retries` — proceed on partial data, or force-retry? | `decide-partial-handling` | `retry` / `proceed` is mechanically determinable | acceptability of `missing_fraction` *for your purpose* → `accept-partial` vs `force-retry-failed` |
| **campaign** | `path` | Manual grid vs strategy-driven (Optuna/PBT/…) | `classify-campaign-path` | source signals are conclusive | `path="unclassifiable"` → choose among `candidates` |
| **campaign** | `decide` | Continue the loop or stop? | `campaign-advance` (deterministic ladder) | every `continue` / `stop_*` / `wait_in_flight` branch | the *substance* of the next iteration is user strategy code (`study.ask()`), which the framework never owns |
| **campaign** | `concurrency` | How many iterations in flight (K)? | `decide-concurrency` | `sequential` (no async support, or no headroom) | within a safe `max_in_flight` bound, *how aggressive* — pick K ∈ [1, max] |

Seven. And four of them (`axis_class`, `partial_handling`, `path`, `concurrency`)
resolve the 80–90% case in code and escalate only the genuinely-ambiguous tail.
That is the measured size of "automagic": the deterministic spine carries the
run; the fuzzy glue is consulted at seven seams, most of them rarely.

### Why each one is irreducible

- **`axis_class`** — a wrong guess *silently* mishandles a stateful series
  (no warm-up → plausible-but-wrong results). The prompt is explicit: "Do NOT
  infer an axis from code, and do NOT default to cartesian." A *recorded*
  `cartesian` verdict (the matcher confidently found no series) and an *absent*
  verdict are never conflated — absence escalates. The residual is exactly the
  patterns the AST matcher can't recognise; widening the matcher shrinks it,
  but novel carried-state shapes will always reach the tail.
- **`partial_handling`** — `decide-partial-handling` computes `missing_fraction`
  mechanically, but "is 12% missing acceptable?" depends on *what the run is
  for*, which the framework deliberately never models (the
  experiment-agnostic boundary). The arithmetic is code; the acceptability is yours.
- **`decide` / `path` / `concurrency`** — the campaign loop's continue/stop and
  manual-vs-strategy classification resolve in code, but the optimiser
  substance (Optuna, RandomSearch, PBT) "lives as user-imported Python
  libraries; the framework ships **zero** strategy code." The framework
  orchestrates the loop; the empirical *reasoning* is held at the caller seam
  by design.
- **`surface` / `resubmit`** — mostly caller- or category-determined; the
  residual is a true authority call (synchronous-wait intent; whether a
  non-recoverable failure warrants giving up vs a code fix).

## A second class: human-authority handoffs

Distinct from the judgement floor — and *not* counted as `escalation_points` —
is a set of points where deterministic code **detects** a condition it cannot
act on because only the **user** can authorise the next move or perform an
out-of-band fix. These STOP and hand back, but the resolver is a human action
or an upstream skill, not an in-loop LLM choice:

| Handoff | Detected by | Why code can't proceed |
|---|---|---|
| Resume vs fresh | `resolve-submit-inputs` → `prior_run_found` | a live prior run matches this `cmd_sha`; only the user chooses resume-vs-rerun |
| Environment not ready | `check-preflight` → `setup_required` | SSH agent / PATH / reachability failing — the user fixes their machine (`hpc-agent setup`) |
| Campaign rule violation | `validate-campaign` / `validate-stochastic-marker` → `fail` | hard gate, **no `--force` by design**; the user edits `tasks.py` / `playbook.yaml` |
| Mature repo, no contract | `detect-entry-point` / `resolve-submit-inputs` → `needs_scaffold_interview` | scaffolding needs an interview a headless worker can't run; the user adds `@register_run` or runs the wrap workflow |
| Canary gate failed | `submit-pipeline` → `canary_failed` | the main array never launched; a real defect needs a real fix |

These are the boundary of *automagic* in the other direction: not "the model
must judge" but "the framework must not act without you." Keeping them as hard
stops (rather than auto-`--force`) is what makes the orchestrator trustworthy.

## The ratchet: how a judgement point becomes code

The floor is not frozen — it's the *target* a ratchet drives toward. Three
mechanisms move work from glue into code, and the `decided_by` field is the
visible boundary between them:

1. **Confident fast-paths.** `classify-axis-easy` is a stdlib-only AST matcher
   that runs *before* the LLM axis tree and records directly on a confident
   hit; only `unclassifiable` / `function_not_found` fall through. Each pattern
   the matcher learns is a slice of `axis_class` that stops escalating.
2. **Code-only resolvers.** `meta/campaign/deterministic_resolver.py` is an
   injectable `JudgementResolver` that runs the campaign's `decide`/cold-`submit`
   steps "in code by chaining the EXISTING deterministic primitives — zero
   worker/LLM spawn when the common path is fully `decided_by="code"`." What it
   *can't* chain it labels **residue** — and that residue is precisely this floor.
3. **Composite absorption.** Every branch/gate/loop a workflow composite
   (`submit-pipeline`, `status-pipeline`, `campaign-run`) folds into one
   envelope is one fewer `total_touchpoint` the prose has to narrate. This is
   the number the meter watches fall.

**The non-goal is driving the floor to zero.** Acceptability-for-your-purpose,
strategy substance, and human authority are *supposed* to escalate — coding
them away would mean the framework deciding things it has no business deciding
(the experiment-agnostic boundary). The ratchet shrinks `total_touchpoints`;
it leaves `escalation_points` standing.

## Where this is checked

- **Authoritative — the `decisions` contract.** Each worker prompt declares its
  exact allowed `point` IDs (submit: 7, status: 4, aggregate: 4, campaign: 6)
  and flags the judgement subset. `parse_worker_report` rejects any other ID and
  rejects an empty `why` at a judgement point. Inventing a point ID fails the
  envelope even when the cluster work succeeded.
- **Metered — the touchpoint baseline.** `scripts/count_llm_touchpoints.py`
  emits `scripts/llm_touchpoints_baseline.json`; `tests/contracts/test_llm_touchpoints.py`
  gates it in CI. A prompt edit that moves a count without regenerating the
  baseline is a CI failure — so the glue surface cannot grow silently.
- **`decisions` vs `anomalies`.** Judgement points go in the strict enumerated
  `decisions` record with `chosen`/`rejected`/`why`. Everything else
  (stop conditions, warnings, stderr tails) goes in free-form `anomalies`.
  When in doubt, prefer `anomalies` — the floor is deliberately small.

## Reading guide

- **Shrinking the floor legitimately** → teach a fast-path matcher a new
  confident sub-case (`classify-axis-easy`), or extend a code resolver
  (`deterministic_resolver`) to chain one more deterministic primitive. Both
  move work below the `decided_by="code"` line without removing the residual.
- **Not the floor** → a `total_touchpoint` (a branch/gate/loop still narrated
  in prose) is *toil-glue*, not judgement. The fix is a composite that absorbs
  it, not a doc entry here.
- **Permanent residual** → if a point requires knowing what the experiment is
  *for* (acceptability), what the user's strategy *is* (optimiser substance), or
  the user's *authorisation* (resume, force, fix), it stays. That is the agent
  seam, by design.

## See also

- [`engineering-principles.md`](engineering-principles.md) — the decide/act and
  library-knowledge boundaries that keep the deterministic core honest.
- [`../architecture.md`](../architecture.md) — the decide/act boundary and the
  `@primitive` verb/side-effect table.
- `scripts/count_llm_touchpoints.py` — the meter, with the per-marker regexes.
- `src/hpc_agent/meta/campaign/deterministic_resolver.py` — the code-only
  resolver and its `residue` accounting.
