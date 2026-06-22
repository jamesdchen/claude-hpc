"""Construct the live :class:`HPCBackend` for an in-flight run record.

The monitor (#337 Increment 4) and aggregate (#337 Increment 5) transports
drive liveness / status / logs / results through a backend's *instance* hooks
(``alive_job_ids`` / ``fetch_logs`` / ``fetch_results``) when the backend is
pure-API (``requires_ssh=False``). Those hooks need a constructed backend, not
the class: a pure-API backend holds an authenticated client that the SSH-era
``@staticmethod`` hooks (``build_alive_check_cmd`` / ``stderr_log_path``) cannot.

This helper builds the backend the same way submit-flow did — through
``build_remote_backend`` → :meth:`HPCBackend.from_build_context` — so the
orchestrator never names a concrete backend module; it asks the registry to
construct one from the record's fields. A built-in SSH family and a registered
pure-API plugin backend are built identically; the caller branches on
``backend.requires_ssh``, never on the name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.infra.backends.remote_factory import build_remote_backend

if TYPE_CHECKING:
    from hpc_agent.infra.backends import HPCBackend
    from hpc_agent.state.run_record import RunRecord


def backend_for_record(record: RunRecord, *, scheduler: str | None = None) -> HPCBackend:
    """Build the ``HPCBackend`` instance for *record*.

    *scheduler* overrides ``record.backend`` for callers (reconcile / status)
    that already hold the scheduler name as an argument; otherwise the name
    recorded on the run is used. The record carries everything
    ``from_build_context`` needs — the backend name, the on-cluster ``script``,
    and the SSH-shaped ``ssh_target`` / ``remote_path`` a pure-API backend
    ignores in favour of its own env-sourced config.
    """
    name = scheduler or record.backend
    return build_remote_backend(
        backend_name=name,
        script=record.script,
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        pass_env_keys=None,
        job_env_keys=tuple(record.job_env or ()),
    )
