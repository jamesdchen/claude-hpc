# hpc-agent post-reorg handoff

A self-contained briefing for a cold session picking up the architecture
work. Reads in ~10 minutes; everything you need to resume is here.

## 1. Where main stands

Version: **0.5.0** (cut 2026-05-24).

Top-level src layout:

```
src/hpc_agent/
├── _kernel/            kernel surface — small, reviewable
│   ├── registry/       primitive + operations + plugins
│   ├── contract/       schema + layout invariants
│   ├── lifecycle/      lifecycle + invoke + playbook + run
│   └── extension/      capabilities + spawn_prompt + telemetry +
│                       version + worker_prompts/ (md procedures)
├── _wire/              Pydantic v2 wire models (was _schema_models)
├── infra/              transport + clusters + time + io + cluster_status
│                       + cluster_logs + throughput + constraints + ...
│                       (cross-cutting substrate, NOT a subject)
├── state/              journal + run_record + index + runs + discover
│                       + runtime_prior + session.py barrel
├── ops/<subject>/      operational subjects — each a vertical concern:
│                       aggregate, clusters, memory, monitor, preflight,
│                       recover, submit, validate
├── meta/<subject>/     "operations about operations" — currently only
│                       campaign/ (driver + atoms + validate workflow)
├── models/             ONE occupant: mapreduce/ (cluster-side combiner)
├── incorporation/      atoms that incorporate user code (axes_init,
│                       classify_axis, export_package, build/)
├── cli/                CLI surface: dispatch.py is the entry point;
│                       per-domain dispatchers (submit, lifecycle, ...);
│                       _dispatch.py + _helpers.py are the adapter SDK
├── runner.py           single-file cross-subject primitive bridge +
│                       back-compat shim (NOT a subject; lives at root)
├── agent_cli.py        thin back-compat shim re-exporting cli/dispatch
└── schemas/            generated JSON schemas (from _wire/ via build_schemas.py)
```

Entry points:
- `hpc-agent` → `hpc_agent.cli.dispatch:main`
- `hpc-campaign-driver` → `hpc_agent.meta.campaign.driver:main`

## 2. The architectural rules

**Subjects (under `ops/` and `meta/`) may not import from each other.**
Enforced by `scripts/lint_subject_imports.py` — no allow-list, no
exceptions. Two escape hatches:

1. **Cross-subject helper sharing** — extract to `infra/`. The lint
   permits `from hpc_agent.infra.* import …` from any subject. Example:
   `ssh_status_report` lives in `infra/cluster_status.py` so aggregate
   AND recover can both reach it without crossing into monitor.

2. **Cross-subject primitive calls** — route through `hpc_agent.runner`
   (the package-root file, not a subject). The lint permits it. Example:
   `meta/campaign/validate.py` calls four `ops/validate/` primitives via
   `from hpc_agent.runner import …`. `scripts/lint_runner_shim.py` gates
   what's allowed through (only `@primitive`-decorated symbols + a small
   explicit allow-list for legacy helpers).

3. **Declarative composition** — `@primitive(composes=["primitive-name"])`
   accepts string names. Resolution is lazy: stashed in
   `_PENDING_COMPOSES`, drained by `_finalize_composes()` at the end of
   `register_primitives()` after every module has imported. Order-
   agnostic; no atoms-before-composites requirement on `_PRIMITIVE_PACKAGES`.

**Registry is auto-discovered.** `register_primitives()` walks the six
roots in `_PRIMITIVE_PACKAGES` (`ops`, `meta`, `incorporation`, `state`,
`cli`, `_kernel.extension`). Every public submodule is imported; the
`@primitive(...)` decorator side-effect populates `_REGISTRY`.
**Adding a primitive in any of these roots requires zero config.**
Adding a primitive in a brand-new top-level package is the only case
that touches `_PRIMITIVE_PACKAGES`.

**Subject `__init__.py` files are docstring-only.** Enforced by
`scripts/lint_subject_init.py`. No eager re-exports.

## 3. Lints + gates (live in CI + pre-commit)

| Script | What it gates |
|---|---|
| `lint_subject_imports.py` | no cross-subject reach |
| `lint_subject_init.py` | docstring-only subject `__init__.py` |
| `lint_runner_shim.py` | runner.py re-exports only @primitives + allow-list |
| `lint_pure_files.py` | @pure files have no I/O imports/calls |
| `lint_skill_command_sync.py` | skills ↔ slash commands are 1:1 |
| `bake_operations_json.py --check` | operations.json reflects registry |
| `build_operations_index.py --check` | docs/generated/operations.md current |
| `build_primitive_frontmatter.py --check` | docs/primitives/*.md frontmatter current |
| `build_schemas.py --check` | schemas/*.json generated from _wire/ |

All exit 0 on main.

## 4. Open backlog (prioritized)

### P1 — runner.py back-compat allow-list shrink (unambiguous)

`scripts/lint_runner_shim.py:_BACK_COMPAT_NONPRIMITIVES` has 10 entries.
Eight have ZERO callers outside `runner.py` itself per
`grep -rn "from hpc_agent.runner import …<name>"`:

```
annotate_clusters_with_retry_advice    0 callers — drop
fingerprint_stderr_tail                0 — drop
build_provenance                       0 — drop
verify_combiner_artifact               0 — drop
verify_per_task_outputs                0 — drop
write_remote_provenance                0 — drop
fetch_task_logs                        0 — drop
derive_resubmit_request_id             0 — drop
DEFAULT_AUTO_RETRY_POLICY              2 — KEEP
cluster_failures_by_fingerprint        3 — KEEP
build_job_env                          2 — KEEP
```

Drop the 8 zero-caller entries from `runner.py`'s imports + `__all__` +
the lint's allow-list. Functions still exist at their canonical homes
(`ops/aggregate/runner.py`, `ops/recover/runner_failures.py`, etc.) —
the shim just stops advertising. ImportError for any external caller
still using them is the correct signal.

### P2 — delete `recommend-partition` (dead primitive)

`src/hpc_agent/ops/submit/recommend_partition.py` declares itself
"composed into plan-throughput and submit-flow" but `grep -n partition`
in those two workflow modules returns nothing. Zero call sites anywhere
in src/, slash_commands/, or skills. Git log shows only refactor moves
on the file. The composition was either removed long ago or never
delivered.

Verdict: delete. Includes:
- `src/hpc_agent/ops/submit/recommend_partition.py`
- `src/hpc_agent/_wire/queries/recommend_partition.py`
- `src/hpc_agent/schemas/recommend_partition.{input,output}.json`
- `tests/ops/submit/test_recommend_partition.py`
- `docs/primitives/recommend-partition.md`
- regenerate `operations.json` + `docs/generated/operations.md` after

Git history preserves the implementation if someone wants it back.

### P3 — Tier B local cleanups (judgment calls)

- **Flatten `models/mapreduce/` → `mapreduce/`** at the top level.
  `models/` has one occupant; a directory with one child is a smell.
  *Unless you have a plan for other `models/` occupants (forecasting,
  pro plugin planning models) — confirm before flattening.*
- **Rename `incorporation/` → `build/`** (or similar). It's the only
  verb-shaped role directory in a sea of noun-shaped ones (`ops/`,
  `meta/`, `state/`, `infra/`). *Counter: "incorporation" may be
  intentional domain jargon ("incorporating user code into the
  framework") — keep if so.*
- **Tag `state/session.py` with `# Remove in 0.6.0`.** It's the Wave-4
  back-compat barrel re-exporting `journal`/`run_record`/`index`. The
  comment-driven removal worked for `framework_subdir` in 0.5.0
  (PR #106 was the overdue cleanup); same trick here.

### P4 — Document the cross-subject seam acceptance

Add a short "Cross-subject composition" section to `docs/architecture.md`
saying: "5 cross-subject `composes=` exist intentionally. They route
through `hpc_agent.runner`. `lint_runner_shim.py` gates what crosses.
*Do not propose collapsing them into per-subject inlining — that
violates DRY for no architectural gain. Do not propose moving them to
a global `workflows/` directory — the reorg explicitly moved them OUT
of one, picking 'subject = primary effect' over 'workflows are their
own subject.'*"

This converts an open question ("should we eliminate these seams?")
into a documented decision so the next contributor doesn't re-litigate.

## 5. Explicit non-goals — do not propose

These were considered and intentionally NOT done:

- **Re-introducing a global `flows/` or `workflows/` directory.** Reorg
  deliberately distributed workflows to their primary subject
  (`ops/submit/flow.py`, `ops/aggregate/flow.py`, etc.). Moving them
  back would undo the work.
- **Inlining cross-subject composition.** Making `aggregate-flow`
  re-poll status itself instead of composing the monitor primitive
  violates DRY. The seams are the design.
- **Restoring `_PRIMITIVE_MODULES` or the lint that policed it.**
  Auto-discovery via `pkgutil.walk_packages` is the SoT now.
- **Re-introducing the `PER_FILE_ALLOWED_IMPORTS` allow-list.** PR #98
  eradicated it. Cross-subject is either `infra/` (helper) or
  `hpc_agent.runner` (primitive call). No exceptions.

## 6. Latent issues the reorg surfaced (already fixed, recorded for context)

- **Mock-path drift** (PR #103). Tests mocked `hpc_agent.ops.monitor.status.ssh_status_report`
  but production code used `infra.cluster_status.ssh_status_report`
  after PR #96 extracted. 6 tests silently passed; 8 silently failed.
  Lesson: when a helper moves, grep for `mock.patch("…<old path>…")`
  AND run pytest (not just imports + ruff).
- **`composes=` order dependency** (PR #98 → PR #108). String-name
  composes required atoms-before-composites order in
  `_PRIMITIVE_MODULES`. Tests that imported a composer directly
  bypassed `register_primitives()` and decoration failed. Fixed by lazy
  resolution in PR #108.
- **Lint skip-rule false-positive** (PR #107). `set(p.parts) & {"build", "dist"}`
  matched `incorporation/build/`, silently skipping 4 real primitive
  modules. Now `SKIP_AT_REPO_ROOT` only matches first component.
- **`_PRIMITIVE_MODULES` ordering bug** (PR #107). `canary_verify`
  (workflow composite) was in atoms section; only worked because 3
  stale entries (`agent_cli`, `infra.clusters`, `ops.recover.batching`)
  transitively imported `monitor.status` first. Removing the stale
  entries forced the move to composites. Lazy resolution (#108)
  eliminated this class of bug entirely.
- **Doc-and-changelog lag.** `docs/architecture.md` described pre-reorg
  state through ~15 PRs (PR #101 refreshed it). `CHANGELOG.md` said
  "wire surface unchanged" then PR #106 removed 3 public functions —
  PR #107 corrected. Lesson: doc + CHANGELOG live in the same PR as the
  code change.
- **`.pth` clobbering in parallel agents.** `pip install -e .` writes
  a shared `.pth` file at one global path. Parallel agents in
  worktrees clobber each other and silently see the wrong source. Use
  `PYTHONPATH=$(pwd)/src python …` for verification instead of editable
  install when running parallel agents.
- **`Path(__file__).parents[N]`-fragility.** PR #105 (test reorg)
  broke 4 tests using parent-counting. PR #108 added `tests/_paths.py`
  with `REPO_ROOT`/`SRC_DIR`/`SCHEMAS_DIR`/`TEMPLATES_DIR` constants
  that climb to find `pyproject.toml` once. Migrate new test code to
  use these.
- **The `Remove in 0.4.0` overrun.** `framework_subdir`/`runs_subdir`/
  `tasks_path` were tagged for 0.4.0 removal in a code comment but the
  0.4.0 release missed the cleanup. PR #106 caught it for 0.5.0.
  Lesson: version-tagged deprecation comments work but need a release
  checklist that searches for `Remove in <version>`.

## 7. Test layout

Tests mirror src 1:1 post-PR #105:

```
tests/
├── _kernel/{registry,contract,lifecycle,extension}/
├── _wire/
├── _paths.py             ← repo-anchor constants
├── cli/
├── contracts/            ← public-API + boundary + lint contract tests
├── fixtures/
├── incorporation/{,build,template}/
├── infra/
├── integration/
├── meta/campaign/{,atoms}/
├── models/mapreduce/
├── ops/{aggregate,clusters,memory,monitor,preflight,recover,submit,validate}/
├── scripts/              ← tests of build scripts
├── state/
├── test_errors.py        ← top-level errors.py mirror
├── test_runner.py        ← top-level runner.py mirror
├── template/
├── worker_prompts/{,fixtures}/
└── conftest.py           ← register_primitives() at import time + helpers
```

Baseline: **1741 passed, 3 skipped** (`pyarrow` + `rich` optional deps;
self-qos validator by design).

## 8. Picking up the work — quick start for a cold session

```bash
# 1. Sync
git fetch origin main
git checkout main
git pull

# 2. Verify gates pass
PYTHONPATH=$(pwd)/src pytest -q                                   # 1741 passed
ruff check . && ruff format --check .                              # clean
PYTHONPATH=$(pwd)/src python -m mypy src/hpc_agent                # clean
for s in lint_subject_imports lint_subject_init lint_runner_shim \
         lint_pure_files lint_skill_command_sync; do
  PYTHONPATH=$(pwd)/src python scripts/$s.py
done

# 3. Verify backlog assumptions still hold (caller counts may shift)
for sym in annotate_clusters_with_retry_advice fingerprint_stderr_tail \
           build_provenance verify_combiner_artifact verify_per_task_outputs \
           write_remote_provenance fetch_task_logs derive_resubmit_request_id; do
  echo "$sym: $(grep -rn "from hpc_agent.runner import.*$sym" src/ tests/ \
                  hpc-agent-pro/ 2>/dev/null | grep -v __pycache__ | wc -l)"
done
# If any moved from 0 → nonzero, re-evaluate P1.

grep -rn "recommend_partition\|recommend-partition" src/ slash_commands/ \
  --include="*.py" --include="*.md" 2>&1 | \
  grep -v __pycache__ | grep -v _wire/queries | grep -v primitives/ \
  | grep -v ops/submit/recommend_partition.py
# Should still be empty — confirms P2 deletability.

# 4. Pick a P-item and ship as its own PR
```

When picking up: **read this whole file**, then check section 5 ("non-goals")
before proposing any architectural change. Section 6 ("latent issues") is
useful priming for what kinds of bugs the reorg taught us to watch for.

## 9. Pointers

- Architecture: `docs/architecture.md` (refreshed PR #101)
- Adding a primitive: `docs/internals/adding-a-primitive.md`
- Wire contract for integrators: `docs/integrations/CONTRACT.md`
- Boundary contract: `docs/reference/boundary-contract.md`
- Audit history: `docs/internals/audit-history.md`
- Reorg PRs (in order): #79–83 (Phase 1) · #87–92 (Wave 2/3) · #94 (PR 5a kernel) · #95 (PR 5b wire) · #96 (Wave-3 cleanup) · #97 (Wave 4) · #98 (allow-list eradication) · #99 (pro plugin) · #100 (docs follow-up) · #101 (architecture.md refresh) · #102 (PR 5c agent_cli) · #103 (test fixes) · #104 (comment scrub) · #105 (tests reorg) · #106 (forwarder removal) · #107 (0.5.0 cut + lint fix) · #108 (registry hardening) · *and whatever closes P1 + P2*
