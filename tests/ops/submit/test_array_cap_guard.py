"""Increment 1 of #339: the array-cap fail-loud guard in submit-flow.

A sweep larger than the backend's platform cap (GitHub Actions = 256) or a
cluster's declared ``constraints.max_array_size`` must be rejected with a clean
``SpecInvalid`` *before* any dispatch, rather than surfacing as a low-signal
platform error after qsub. A backend with no cap (the SSH families) and a
cluster that declares no limit leave ≤cap sweeps byte-for-byte unaffected.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.ops.submit_flow import (
    _cluster_array_cap,
    _effective_array_cap,
    _enforce_array_cap,
)


def _backend(cap):  # type: ignore[no-untyped-def]
    """Minimal stand-in whose *class* carries ``max_array_size``.

    The guard reads the cap off ``type(backend)`` (a capability, not per-run
    state), so the attribute must live on the class, not the instance.
    """
    return type("FakeBackend", (), {"max_array_size": cap})()


# --------------------------------------------------------------------------- #
# _cluster_array_cap — only a *declared* limit counts.
# --------------------------------------------------------------------------- #


def test_cluster_cap_none_when_cluster_falsy() -> None:
    assert _cluster_array_cap(None) is None
    assert _cluster_array_cap("") is None


def test_cluster_cap_reads_declared_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": 100}}},
    )
    assert _cluster_array_cap("hpc1") == 100


def test_cluster_cap_none_when_no_constraints_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # A cluster that declares no constraints must NOT synthesise the
    # ClusterConstraints default (1000) — today's behaviour is unbounded.
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"scheduler": "slurm"}},
    )
    assert _cluster_array_cap("hpc1") is None


def test_cluster_cap_none_when_unknown_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"other": {"constraints": {"max_array_size": 100}}},
    )
    assert _cluster_array_cap("hpc1") is None


def test_cluster_cap_swallows_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict:
        raise RuntimeError("no clusters.yaml here")

    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", _boom)
    assert _cluster_array_cap("hpc1") is None


def test_cluster_cap_ignores_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``True`` is an int subclass; a stray bool must not be read as a cap of 1.
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": True}}},
    )
    assert _cluster_array_cap("hpc1") is None


# --------------------------------------------------------------------------- #
# _effective_array_cap — reconcile backend + cluster, smaller wins.
# --------------------------------------------------------------------------- #


def test_effective_cap_none_when_both_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", dict)
    assert _effective_array_cap(_backend(None), "hpc1") is None


def test_effective_cap_backend_only() -> None:
    assert _effective_array_cap(_backend(256), None) == 256


def test_effective_cap_smaller_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": 100}}},
    )
    # backend 256 vs cluster 100 -> 100
    assert _effective_array_cap(_backend(256), "hpc1") == 100
    # backend 50 vs cluster 100 -> 50
    assert _effective_array_cap(_backend(50), "hpc1") == 50


# --------------------------------------------------------------------------- #
# _enforce_array_cap — the guard itself.
# --------------------------------------------------------------------------- #


def test_guard_fires_over_cap() -> None:
    with pytest.raises(errors.SpecInvalid) as exc:
        _enforce_array_cap(
            _backend(256), total_tasks=300, backend_name="github-actions", cluster=None
        )
    msg = str(exc.value)
    assert "300" in msg and "256" in msg and "github-actions" in msg


def test_guard_silent_at_cap() -> None:
    # Exactly at the cap is allowed (one full array).
    _enforce_array_cap(_backend(256), total_tasks=256, backend_name="github-actions", cluster=None)


def test_guard_silent_under_cap() -> None:
    _enforce_array_cap(_backend(256), total_tasks=10, backend_name="github-actions", cluster=None)


def test_guard_noop_when_uncapped() -> None:
    # SSH family: None cap + no declared cluster limit -> never fires, even huge.
    _enforce_array_cap(_backend(None), total_tasks=10_000, backend_name="slurm", cluster=None)


def test_guard_fires_on_cluster_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": 100}}},
    )
    with pytest.raises(errors.SpecInvalid) as exc:
        _enforce_array_cap(_backend(None), total_tasks=500, backend_name="slurm", cluster="hpc1")
    assert "100" in str(exc.value) and "hpc1" in str(exc.value)
