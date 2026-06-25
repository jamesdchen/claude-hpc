---
name: build-interview-spec
verb: scaffold
side_effects:
  - writes-sidecar <experiment>/.hpc/interview_spec.json
idempotent: true
idempotency_key: experiment_dir
error_codes:
  - spec_invalid
backed_by:
  cli: hpc-agent build-interview-spec --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.incorporation.build_interview_spec.build_interview_spec
---
# build-interview-spec

Assembles an `InterviewSpec` from **discrete args** and writes it to
`.hpc/interview_spec.json`. The LLM passes values (a free-text `goal`, a
typed `task_generator` node, a provenance dict, an explicit
`tasks_py_mode`); it never composes the spec JSON. Because the spec is
built field-by-field in code, there is no hand-authored JSON for an
autonomous agent to fabricate a `task_generator` inside — the failure
mode of incident 1b (an agent inventing a sweep and justifying it with
"safe_defaults").

Replaces the `hpc-wrap-entry-point` SKILL Step 7's "write the spec to
`/tmp/interview_spec.json` and invoke `hpc-agent interview`" prose: the
agent now calls this verb with the resolved fields, and the framework
assembles + persists the canonical spec.

## Inputs / outputs

See `hpc_agent/schemas/build_interview_spec.{input,output}.json`. The
input carries the discrete fields; the output reports the written
`spec_path`, the `tasks_py_mode` it was assembled under, and an echo of
the load-bearing fields (`goal`, `task_count`, `has_task_generator`,
`has_entry_point`).

## REQUIRED_CALLER_FIELDS

`goal` and `task_generator` are the two fields the framework cannot
invent (the `field_partition.REQUIRED_CALLER_FIELDS` set):

- **`goal`** is required by the input model (`min_length=1`). An absent or
  empty goal is a schema rejection, surfaced as `spec_invalid`.
- **`task_generator`** is required *by mode* (see below), never inferred
  from absence.

## tasks_py_mode is ALWAYS explicit

The load-bearing discriminator is `tasks_py_mode` — `generator` or
`validate` — and it is **always passed explicitly**, never inferred from
whether `task_generator` happens to be present:

- **`generator`** — the typed recipe regenerates `.hpc/tasks.py`.
  REQUIRES `task_generator`; absence is `spec_invalid`.
- **`validate`** — the caller already wrote `.hpc/tasks.py` by hand; the
  interview only validates it. FORBIDS `task_generator` (a recipe here
  would silently regenerate over the caller's hand-written file).

Inferring the mode from `task_generator`'s absence — the trap a naive
"refuse if absent" gate falls into — would break the sanctioned
hand-written-tasks.py path (`ops/memory/interview.py:256-288`). Making
the mode explicit preserves both paths and makes the choice auditable.

## Single-author guarantee

After assembling the discrete fields, the primitive re-validates the
result through the *same* `InterviewSpec` model the `interview` primitive
consumes. A structurally-impossible combination (e.g. a `data_axis_hint`
on a `register_run` entry point, #260) therefore surfaces as
`spec_invalid` here, at assembly — not as a confusing failure two verbs
downstream. `build-interview-spec` emits exactly what `interview` accepts.

## Seam

This primitive only assembles and persists the spec. The `interview`
primitive consumes `.hpc/interview_spec.json` (or the same shape) to
materialize / validate `.hpc/tasks.py`; that seam is unchanged.
