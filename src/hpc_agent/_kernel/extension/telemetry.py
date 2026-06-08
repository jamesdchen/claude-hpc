"""Canonical telemetry sink for the framework.

The codebase has five telemetry surfaces today (CLI envelope on stdout,
``print(..., stderr)`` from cluster-side dispatch / combiner, single-use
:mod:`logging`, single-use :func:`warnings.warn`, and the
``<run_id>.monitor.jsonl`` JSONL stream). They use three different
formats and there is no canonical sink — the JSONL stream is the
closest thing, but its writer is tucked inside :mod:`monitor_flow` and
so the slash-command surface had to inline its own copy of the same
flock-append routine. That inlining is what landed item A9 (the
slash-command surface and monitor_flow racing on un-flocked appends
torn-line bug).

This module is the small extraction A9 implies: one
:func:`record(event, payload, *, sink=...)` entry point that the
in-process callers can use, plus a flock-guarded JSONL writer for the
``monitor.jsonl`` sink so any future caller (e.g. campaign manager,
calibration loop) can tail the same file without re-inventing the
write discipline.

Sinks
-----

* ``"stderr-jsonl"`` — write the record as a JSON line to ``sys.stderr``.
  Use this for cluster-side primitives that must stay stdlib-only and
  cannot import this module; they inline the same shape so a tail-f
  on stderr produces the same JSONL stream.
* ``"monitor-jsonl"`` — append to ``<run_id>.monitor.jsonl`` under
  ``runs_dir(experiment_dir)``. Requires ``run_id`` and
  ``experiment_dir`` in *payload* or as additional arguments. Held
  under an exclusive flock so concurrent writers cannot interleave
  bytes (the A9 invariant).
* ``"otel"`` (alias ``"otlp"``) — emit each event as an OpenTelemetry
  span whose name is *event* and whose attributes are the flattened
  *payload* (so a submit / state-transition / resubmit / canary /
  campaign-decision record — including its structured ``reason`` and
  ``trial_token`` — shows up in Grafana or any OTLP backend). The
  OpenTelemetry SDK is an **optional** dependency: importing this module
  never pulls it in, and the import is deferred to the point the sink is
  actually selected. If the SDK is not installed, selecting this sink
  raises :class:`hpc_agent.errors.ConfigInvalid` with a pip hint rather
  than failing silently — the operator explicitly asked for OTLP export,
  so a clear error beats a silent no-op. Configure the exporter endpoint
  via the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` /
  ``OTEL_EXPORTER_OTLP_*`` env vars the SDK already reads.
* ``"none"`` — silently drop. The default when ``HPC_TELEMETRY_SINK``
  is unset.

Why we don't migrate ``dispatch.py`` / ``combiner.py``
-----------------------------------------------------

Those modules execute on the cluster where the framework package is
not installed (they ship as standalone Python files inside the
``.hpc/`` payload). Importing :mod:`hpc_agent._kernel.extension.telemetry` would
break that constraint. They keep their existing
``print(..., stderr)`` calls; if a future callsite needs JSONL it
should inline a tiny ``_record`` helper rather than pull in this
module.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.io import advisory_flock

if TYPE_CHECKING:
    from pathlib import Path


# Environment override. Tests / orchestrators set this to redirect
# telemetry to a known sink. Default is "none" so a stray
# :func:`record` call doesn't pollute stderr in production runs.
_ENV_VAR = "HPC_TELEMETRY_SINK"


@contextlib.contextmanager
def flock_append(target: Path):
    """Yield with an exclusive flock on a sibling ``.lock`` file for *target*.

    Thin wrapper around :func:`hpc_agent.infra.io.advisory_flock`
    that derives the lock path (``<target>.lock``). Ensures all writers
    to ``<run_id>.monitor.jsonl`` serialise their appends — without the
    flock, a concurrent monitor_flow tick and slash-command poll can
    produce a torn JSON line. On non-POSIX platforms ``advisory_flock``
    degrades to a no-op; the torn-line risk is documented as acceptable
    for non-production environments.
    """
    with advisory_flock(target.with_suffix(target.suffix + ".lock")):
        yield


def _resolve_sink(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    return os.environ.get(_ENV_VAR, "none")


# Cached OpenTelemetry tracer. Built on first ``otel``-sink emit and
# reused thereafter so we don't re-resolve the global provider per
# event. ``None`` means "not yet built"; a missing SDK raises before we
# ever cache.
_OTEL_TRACER: Any | None = None


def _otel_attr_value(value: Any) -> Any:
    """Coerce *value* into an OpenTelemetry-attribute-safe form.

    OTel attribute values must be ``bool | int | float | str`` or a
    homogeneous sequence of one of those. ``None`` is not permitted and
    nested dicts / lists-of-dicts are not. We pass primitives through
    untouched (so ``trial_token`` stays a string and numeric metrics
    stay numeric) and JSON-encode everything else — a structured
    ``reason`` dict survives as a queryable JSON string rather than
    being dropped.
    """
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, bool | int | float | str) for v in value
    ):
        return list(value)
    return json.dumps(value, sort_keys=True, default=str)


def _otel_tracer() -> Any:
    """Return the cached OpenTelemetry tracer, building it on first use.

    Raises :class:`hpc_agent.errors.ConfigInvalid` when the SDK is not
    installed. The import is local so that merely importing this module
    never hard-requires the optional ``opentelemetry`` packages.
    """
    global _OTEL_TRACER
    if _OTEL_TRACER is not None:
        return _OTEL_TRACER
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise errors.ConfigInvalid(
            "HPC_TELEMETRY_SINK='otel' needs the OpenTelemetry SDK. "
            "Install the optional extra: pip install 'hpc-agent[otel]' "
            "(or: pip install opentelemetry-sdk "
            "opentelemetry-exporter-otlp-proto-http). Point it at a "
            "collector with OTEL_EXPORTER_OTLP_ENDPOINT."
        ) from exc

    # Only install our own provider if the process hasn't already set
    # one up (an embedding host may own the global provider). The SDK's
    # default provider is a no-op ProxyTracerProvider; detect that.
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": "hpc-agent"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

    _OTEL_TRACER = trace.get_tracer("hpc_agent.telemetry")
    return _OTEL_TRACER


def _emit_otel(event: str, payload: dict[str, Any]) -> None:
    """Emit one event as a short-lived OpenTelemetry span.

    The span name is *event*; every *payload* field becomes a
    ``hpc.<key>`` attribute (coerced via :func:`_otel_attr_value`).
    Export failures are swallowed — telemetry must never crash the
    parent loop — but a *missing SDK* surfaces as ``ConfigInvalid`` from
    :func:`_otel_tracer` because that is operator misconfiguration, not
    a transient backend hiccup.
    """
    tracer = _otel_tracer()
    with (
        tracer.start_as_current_span(event) as span,
        contextlib.suppress(Exception),
    ):
        for key, value in payload.items():
            span.set_attribute(f"hpc.{key}", _otel_attr_value(value))


def record(
    event: str,
    payload: dict[str, Any],
    *,
    sink: str | None = None,
    monitor_jsonl_path: Path | None = None,
) -> None:
    """Record one telemetry event.

    *event* is a stable machine-readable name (``"tick"``,
    ``"poll"``, ``"campaign_step"``); *payload* is a dict of
    arbitrary JSON-serialisable fields. The serialised line is
    ``{"event": event, **payload}`` — callers pre-add ``run_id``,
    ``tick_id``, etc. to *payload* if useful.

    *sink* selects the destination; ``None`` defers to
    ``HPC_TELEMETRY_SINK`` (default ``"none"``). When
    ``sink == "monitor-jsonl"``, *monitor_jsonl_path* must be provided
    (the resolved path of ``<run_id>.monitor.jsonl``). The append is
    held under an exclusive flock; failures are swallowed so a flaky
    log volume cannot tank the parent operation.

    When ``sink in ("otel", "otlp")`` the event is exported as an
    OpenTelemetry span (see the module docstring). The OTel SDK is an
    optional dependency; a missing SDK raises
    :class:`hpc_agent.errors.ConfigInvalid`.
    """
    sink = _resolve_sink(sink)
    if sink == "none":
        return
    line = json.dumps({"event": event, **payload}, sort_keys=True)
    if sink == "stderr-jsonl":
        with contextlib.suppress(OSError):
            print(line, file=sys.stderr, flush=True)
        return
    if sink == "monitor-jsonl":
        if monitor_jsonl_path is None:
            raise errors.SpecInvalid("sink='monitor-jsonl' requires monitor_jsonl_path")
        try:
            with (
                flock_append(monitor_jsonl_path),
                monitor_jsonl_path.open("a", encoding="utf-8") as f,
            ):
                f.write(line + "\n")
        except OSError:
            # Telemetry writes must never crash the parent loop.
            pass
        return
    if sink in ("otel", "otlp"):
        _emit_otel(event, payload)
        return
    # Unknown sink — be silent rather than raise; we treat sink names
    # as a contract owned by the caller, not a hard schema.


__all__ = ["flock_append", "record"]
