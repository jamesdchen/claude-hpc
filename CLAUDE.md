# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repo. Loaded
automatically at the start of a session.

## Engineering principles

### Verify a guard can actually fire before classifying it as "intentional"

When you hit a constraint, a defensive default, an apparent duplication, or
anything that *looks* deliberate, do not default to "leave it, it's by
design." Establish **which** it is: check whether the protection can actually
fire, and whether changing it alters behavior a real path or a test would
notice. A guard that can never fire is inertia, not design — and a comment
asserting a reason ("so legacy X validates", "cluster-side baseline") is a
claim to verify, not evidence.

This cuts both ways — apply it before you *preserve* something **and** before
you *remove* it. Real examples from this codebase:

- **Looked intentional, was inert.** Output schemas typed `run_id` as a loose
  `str` "so legacy sidecars validate." But `run_sidecar_path` already
  validates every run_id against the strict `^[A-Za-z0-9._\-]+$` pattern at
  the filesystem layer, so the loose-output guard could never accept anything
  the strict one wouldn't — and the one case it *could* fire (the framework
  emitting a malformed id) is a bug it would hide rather than catch. Tightened
  to `RunIdStrict` on output.
- **Looked intentional, was misattributed.** `infra/parsing.py` was assumed to
  be a "cluster-side baseline" that couldn't import the package. It is not
  deployed to the cluster and the dispatcher never classifies — it only
  captures stderr. The framing was simply wrong. (The module's own docstring
  carried the same false rationale long after this lesson was recorded;
  corrected in 0.10.46 to the verified claim — `deploy_runtime` ships only
  `dispatch.py`, `combiner.py`, `metrics_io.py`, and the shell templates.
  Recording a lesson here without fixing the source it came from leaves the
  trap armed for the next reader.)
- **Looked like dead duplication, was load-bearing.**
  `runner_failures._FAILURE_CATEGORY_PATTERNS` looked like a removable
  duplicate of `failure_signatures.CATALOG`, but three tests iterate it as the
  canonical set of "categories the classifier can emit" to assert
  `FailureCategoryResubmittable` covers them. Removing it re-points a contract;
  it is not free.

The cheap, repeatable check: *can this protection actually fire, and does
changing it alter behavior a test or a real code path would notice?* Answer
that before classifying — for both keep and remove decisions.

### Library knowledge in core: the four-question boundary test

hpc-agent's core is *experiment*-agnostic, not *software*-agnostic: it never
encodes what a user's parameters mean, but it legitimately knows scheduler
dialects, MPI launchers, pandas rolling idioms, and PETSc checkpoint hooks.
"It's already in core" is not the justification — passing this test is.
Knowledge of a specific third-party library may live in core only when ALL
four hold:

1. **Substrate, not semantics.** The knowledge is about how to run / persist /
   schedule / classify / verify computation — never about what an experiment's
   parameters or search space mean (those stay caller-owned: `tasks.py`,
   free-text `task_kind`, no typed search spaces).
2. **Core dispatches, never branches.** Library names appear in core only at
   *declared assembly points* — enumerated in
   `scripts/lint_library_knowledge.py`, which CI runs. Everywhere else, core
   calls a library-agnostic contract (e.g. `checkpoint_formats.CheckpointFormat`,
   the axis-matcher dispatcher). Adding an assembly point is a reviewed edit to
   that list, not an incidental import.
3. **Import-safe on every runtime surface it reaches.** There are three
   surfaces with different import budgets: the installed control plane
   (anything), the run's cluster env (installed package; stdlib-only modules
   preferred), and the standalone-shipped files (`dispatch.py` / `combiner.py`
   / `metrics_io.py` — cannot import the package at all; duplication there is
   by design, see `_CHECKPOINT_RES`). Check the surface, not the repo.
4. **Core CI verifies it without the library installed.** Crafted fixtures
   (AST snippets, golden bytes like the PETSc Vec blocks) — if correctness is
   only testable with the real library, the knowledge belongs in a plugin
   whose CI carries the dependency, not in core.

When a knowledge family grows (a second solver adapter, a new matcher), the
trigger is: collapse any inline library-name branching into the family's
registry/dispatcher, and add the new module behind it — do not add a second
inline branch.
