# External prior-art comparison: lara-hpc, remotemanager, SciMate HPC-Skills

_Analysis date: 2026-05-31. Source of this study: a deep read of three external
projects against hpc-agent, to answer "what can we learn / reuse / stop
reinventing?" The actionable findings are tracked as issues #204–#207; this
document is the durable rationale behind them._

## TL;DR

- **lara-hpc** is **not** an HPC library to harvest infrastructure from — it's a
  ~1.5k-LOC research prototype (BigDFT group, 2025 LLM hackathon) that delegates
  **all** remote execution to `remotemanager`. Its one genuinely-ahead idea is an
  **eval methodology** (→ #204).
- **remotemanager** is the real "wheel" lara chose. We should **not adopt it as a
  dependency** — on the two architecturally decisive axes (real scheduler
  reconciliation, typed errors) we are correctly and substantially ahead. But the
  adversarial port audit found **a few things it does better** worth porting
  (→ #206) or adapting (→ #207, plus smaller follow-ups below).
- **SciMate HPC-Skills** is a knowledge-only MIT skills pack — complementary to our
  engine, nothing executable to import; at most a narrow reference-only borrow.
- The strongest signal overall: where two mature, independent teams **converged**
  with us (subprocess-ssh, stdlib-on-cluster, content-hash dedup, local==remote
  testability), that's confirmation our "reinvention" is the correct engineering —
  not wasted effort.

## The three-repo landscape

| Project | What it actually is | Size | Relationship to us |
|---|---|---|---|
| [`lara-hpc`](https://gitlab.com/l_sim/lara-hpc) (v0.2.4) | OpenAI ReAct agent (LangGraph) doing RAG over docs; **one** HPC tool that delegates 100% to remotemanager | ~1.5k LOC | No code to reuse; one methodology to steal |
| [`remotemanager`](https://gitlab.com/l_sim/remotemanager) (v0.14.3) | Standalone remote-job library: SSH + templated schedulers + content-hash result caching | ~13.7k LOC | The real prior art; don't adopt, port ~2 ideas |
| [SciMate `HPC-Skills`](https://github.com/SciMate-AI/HPC-Skills) | MIT knowledge pack: 22 solver/cluster Claude/Codex skills (prose + templates) | ~24k lines md | Complementary; reference-only |
| **hpc-agent** (v0.8.1) | POSIX-native primitive toolbox / LLM plugin; ~50–66 `@primitive` ops; file-state; real scheduler reconciliation | ~43.7k LOC src, 186 test files | — |

## 1. lara-hpc — delegation, not infrastructure

lara hand-rolls **no** SSH/SLURM/sbatch (verified: zero such code). It is
`langchain-openai` + `create_react_agent` + an `ontoflow`/`OntoRAG` RAG layer,
tightly coupled to BigDFT. Its single HPC tool, `remote_run_code(function_source,
hostname, function_args)` (`MainAgent/utils.py`), does:

```
ast.parse(source)              # validate it's real Python
→ URL(host).cmd("pwd", timeout=1)   # cheap connectivity preflight
→ local dry-run (fn(**args) under BIGDFT_MPIDRYRUN=1)
→ remotemanager Dataset(...).run() / wait() / fetch_results()
→ {"Result": ...} | {"Error": ...}   # structured, never raises
```

A Claude-based system replaces that whole OpenAI/LangGraph layer, so the code
isn't reusable. Two **patterns** are:

- **Tiny tool surface.** lara gives the model exactly one tool and leans on
  docs/RAG for the rest. We're at the opposite pole (~50–66 primitives). Ours is
  right for our scope, but it's a standing reminder that surface area is a cost the
  model pays on every call — worth periodically auditing which primitives need to
  be `agent_facing`.
- **Validate-before-submit ladder** — including a **local dry-run before any
  remote work**. Our cluster-side canary is stronger, but it runs *after*
  rsync+deploy+submit. A cheap local executor smoke-run is complementary (→ #205).

### 1a. The eval methodology (the thing actually worth taking) → #204

lara's real contribution is a **"docstring-as-test" NL→behavior regression
suite**:

- Each task is a Python function: **body = ground-truth reference**, **docstring =
  the prompt** in two registers — `Evaluation-style` (precise spec) and
  `User-style` (casual "how do I…"). One gold result tests both spec-following and
  intent-understanding.
- Grading = `recursive_compare` (~15 lines): **float-tolerant, structural**
  comparison of the *result object*, not prose — the correct grader for scientific
  output, and the correct instinct generally.
- Gold outputs in version-controlled YAML, re-baselined via `--regen-results`;
  cheap/expensive split via pytest marks (`api`/`slow`, `--no-api`).

We have 186 unit/contract/snapshot test files but **no behavioral eval** of *"given
this NL request, did the agent resolve the right submit spec?"* Our stable JSON
envelopes make us well-positioned to adopt the design. **Honest caveat: in lara
this harness is aspirational** — the LLM-grading loop is commented out and only
11/30 tasks have gold results. Take the design, not the code.

## 2. remotemanager — don't adopt; port the narrow wins

### Why not adopt it as a dependency

It's a different execution model (ships an arbitrary Python *function* + args,
returns the deserialized return value) — not a drop-in under our `total()` /
`resolve(i)` → file-based map-reduce contract. And its weakest areas are our
strongest:

- **No scheduler integration at all.** Status = `cat` a manifest logfile the jobs
  append to; no `squeue`/`sacct`/`qstat`. A scheduler-killed job (OOM/TIMEOUT) that
  never writes `failed` **hangs in `submit pending` forever** — only a client-side
  `wait()` timeout saves you. We have real reconciliation (`infra/inspect/*`,
  `ops/monitor/reconcile.py`).
- **Anaemic errors:** `RuntimeError` + string-match; its one "custom" type isn't
  even an `Exception`. We have typed `HpcError` + 15 subclasses → 16 codes → 3 exit
  buckets, each with `retry_safe`/`remediation`.
- **Security footguns:** `SendableMixin` will `importlib.import_module` any class a
  db file names (protection explicitly disabled) and `Script` uses `eval`; never
  load untrusted db files/templates.
- **Bus factor (measured).** Alive and active — 4 years, 6,472 commits, releases
  through 2026 (v0.14.3), commits last week — but **99.5% of all commits are one
  author** (Louis Beal, CEA Grenoble / Univ. Grenoble Alpes, L_Sim — the BigDFT
  group; sole `remotemanager` dev, ~457 of last-12-mo commits vs 9 from everyone
  else). Well-maintained in *throughput*, single-point-of-failure in *resilience*.
  This shape argues for **track-and-borrow + contribute upstream, not depend on**:
  re-pull techniques as he hardens them (e.g. he fixed the SSH pipe-hang on
  2026-05-22) without betting our uptime on one person.

### The transport-seam question (revisited) → #209

Pressed on whether `remotemanager`'s `CMD` solves our most-patched seam (`ssh_run`)
better: **on hang-avoidance, yes** — its `_communicate_with_select` closes pipes on
process-exit instead of waiting for EOF, so a backgrounded child can't wedge the
read; ours catches that only via the blocking-`subprocess.run` timeout. **But three
measured facts make borrow-the-technique, not adopt-the-library, correct:** (1)
`from remotemanager.connection.cmd import CMD` transitively pulls in **44 internal
modules** (the whole library, incl. the `eval`/arbitrary-import `SendableMixin`
`CMD` inherits) — not separable; (2) ~18 of our 23 transport bugs are
Windows/OpenSSH/ssh-agent/OAuth, a surface `remotemanager` (clean POSIX
`Popen(shell=True)`) lacks entirely — adopting it *regresses* them; (3) bus factor
above. So #209 vendors the ~60-line select-loop technique (MIT, attributed) behind
our existing `ssh_argv` seam.

### Convergence = confirmation

On the fundamentals we and remotemanager independently reached the **same**
answers — strong evidence the design is right, not redundant:

| Decision | remotemanager | hpc-agent |
|---|---|---|
| Transport | subprocess ssh, **not** paramiko | subprocess ssh, **not** paramiko |
| Remote payload | source-shipped, **stdlib-only** on cluster | `_hpc_dispatch.py` **stdlib-only**, no `hpc_agent` import |
| Dedup | content-hash, persisted, **survives restarts** | `cmd_sha` + sidecars, survives restarts |
| Testability | `local==remote` path → test on localhost | same pattern |

### The port list (adversarial audit result)

Only **two** items clear the bar to port cleanly; four more are worth adapting the
*idea* using our better mechanisms. Everything else is ruled out — almost always
because it's welded to remotemanager's function-shipping / logfile-status model
that we deliberately rejected.

| # | Item | Verdict | Tracked |
|---|---|---|---|
| P1 | **Substitution value-coercion DSL** — inline min/max clamp + `math.ceil` + `format="time"` (sec→`HH:MM:SS`) + drop-line-on-empty (`script/substitution.py:297-327`). We scatter this across `infra/throughput.py`, `constraints.py`, validate gates, and hand-roll walltime formatting per scheduler. | **PORT** (semantics only; keep our `{{TOKEN}}` syntax) | **#206** |
| P2 | **Spurious-stderr allowlist** (`connection/validate_error.py`) — benign login-node noise (locale/MOTD/`bind: Address already in use`). We have deny-markers (`_SSH_THROTTLE_MARKERS`) but no benign allowlist. | **PORT** (tiny) | follow-up |
| P5 | **Source-hash in dedup key.** Their `Function.uuid = sha256(source)` makes a code edit force a re-run. Our `cmd_sha` hashes **only** `resolve(i)` params, so an executor-body edit with unchanged params dedups against the stale run. | **ADAPT** — opt-in `--invalidate-on-code-change` + drift warning; don't change default silently | **#207** |
| P7 | **Freshness guard on inter-stage inputs** (`dataset/dependency.py:374-401`). Our `stages.py` ordering (afterok/-hold_jid) is more correct, but stage→stage *data flow* has no freshness/provenance check — a stage can silently eat a stale upstream artifact. | **ADAPT** — port the idea via run_id provenance (not their mtime) | follow-up |
| P8 | **Ranked forward-only state machine** (`dataset/runnerstates.py`) — single ordered scale enabling clean `state >= "submit pending"` checks. | **ADAPT** — add rank to our `LifecycleState`/`TaskStatus` enums | follow-up (nice-to-have) |
| P3 | **Close-pipes-on-process-exit anti-hang** (`connection/cmd.py:714-731`) — avoids wedging on backgrounded children that inherit pipe FDs. | **ADAPT, conditional** — only if/when a streaming `capture=False` path lands; not needed today | follow-up |
| — | Source-shipping / `SendableMixin`; `DelayVar` + YAML-`Computer`-as-jobscript; positional bash-DAG; lock-free YAML db + hard version barriers; rsync `--checksum`; `--rsync-path="mkdir -p && rsync"`; OSError→file failover | **SKIP** | — |

Rationale for the big SKIPs: source-shipping/`SendableMixin` violates the
experiment-agnostic boundary and carries `eval`/arbitrary-import; `DelayVar`
overrides every dunder (`a == b` returns a `DelayVar`, not a bool) — a maintenance
hazard; YAML-`Computer`-as-jobscript breaks our `HPC_KW_*` / `_hpc_dispatch.py`
contract and bypasses the scheduler integration that makes us stronger; their
lock-free YAML db with hard version *barriers* is worse than our flock-guarded
JSONL journal + forward-migrating sidecars for unattended multi-process operation.

> Note: the audit also **corrected an earlier internal analysis claim** that
> `cmd_sha` folds in `executor + frozen_yaml_shas`. Source says otherwise
> (`run_sha.py` hashes `resolve(i)` only; `tasks_py_sha` is provenance/drift-only).
> Confirming the exact lines is part of #207.

## 3. SciMate HPC-Skills — complementary, reference-only

A 22-skill, ~24k-line MIT knowledge pack (solver domain: VASP, GROMACS, OpenFOAM,
LAMMPS, Gaussian… + cross-cutting `hpc-foundations` / `hpc-orchestration` /
`hpc-gpu-stack`). Knowledge-only: no app, no installer, no tests/CI; skills are
`SKILL.md` (description-triggered "Use when…") + progressive `references/*.md` +
`assets/templates/`.

Findings (two hopeful assumptions did **not** hold):

- **Error dicts mostly can't feed `ops/recover/failure_signatures.py`.** The
  cross-cutting/GPU dicts are prose "Pattern-ID" entries with **no matchable
  strings** — useless to our regex CATALOG. Only solver dicts (VASP 308-line,
  CalculiX) carry verbatim error strings, but their fixes are *input-deck edits*
  outside our bounded action set.
- **Don't import any `SKILL.md` verbatim** — house-style drift (we use
  `category:`, `allowed-tools:`, and explicit "Not for…(see <sibling>)" tails;
  they don't). Our skill `description` discipline is already stricter.
- **`hpc-orchestration/references/lifecycle-manual.md` is almost verbatim our
  recover loop, as prose** ("submit → monitor → classify failure → repair →
  resubmit; cap retries; require a concrete change between attempts; don't resubmit
  unchanged failing jobs"). External validation of our architecture — nothing to
  import.

The only modest win: house the **solver error dictionaries** + selected
**orchestration/foundations reference bodies** as read-only material under a new
`docs/hpc-knowledge/` (bodies only, outside `skills/`, MIT notice retained) that
the recover/status agent could consult when `classify()` returns `unknown`. Not a
live dependency. Not yet ticketed.

## What changed as a result

| Finding | Issue |
|---|---|
| Behavioral eval harness (NL request → resolved spec/envelope), graded structurally | [#204](https://github.com/jamesdchen/hpc-agent/issues/204) |
| Local executor dry-run / smoke-exec gate before the cluster canary | [#205](https://github.com/jamesdchen/hpc-agent/issues/205) |
| Centralize resource value-coercion (clamp + canonical walltime `HH:MM:SS`) — port P1 | [#206](https://github.com/jamesdchen/hpc-agent/issues/206) |
| Confirm `cmd_sha` excludes code identity; opt-in invalidate-on-code-change — adapt P5 | [#207](https://github.com/jamesdchen/hpc-agent/issues/207) |
| Vendor select-loop / close-pipes-on-exit into `ssh_run` (anti-hang); track `CMD`, don't depend — P3 | [#209](https://github.com/jamesdchen/hpc-agent/issues/209) |

Smaller un-ticketed follow-ups from the audit: P2 (spurious-stderr allowlist),
P7 (stage-input freshness via provenance), P8 (ranked state enum), and the optional
`docs/hpc-knowledge/` reference borrow from HPC-Skills. A possible collaboration
thread (contributing our Windows/ssh-agent hardening *upstream* to `remotemanager`,
which lacks it) is noted but deliberately not ticketed.
