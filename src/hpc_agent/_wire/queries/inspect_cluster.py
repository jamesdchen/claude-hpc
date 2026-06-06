"""Pydantic model for the ``inspect-cluster`` query atom's output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import Scheduler


class _NodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    alloc_mem_pct: float | None = Field(default=None, ge=0, le=1)
    cpu_load_frac: float | None = Field(default=None, ge=0)
    gres: str | None = None
    gres_used: str | None = None
    active_features: list[str] | None = None
    co_tenants: list[dict[str, Any]] | None = None
    is_stressed: bool | None = None
    is_drained: bool | None = None


class InspectClusterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="inspect-cluster output data")

    cluster: str
    scheduler_kind: Scheduler
    now_iso: str = Field(description="UTC ISO-8601 timestamp of the snapshot.")
    nodes: list[_NodeSnapshot]
    errors: list[str] = Field(
        description="Non-fatal per-node errors (e.g. one node failed to report). Fatal cases raise ssh_unreachable instead.",
    )
    parallel_environments: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Scheduler 'parallel environments' — the named pools you target for "
            "(multi-rank) work: SGE PEs (qconf -spl/-sp), SLURM partitions "
            "(scontrol show partition), or PBS execution queues (qstat -Qf). Each "
            "carries at least {name, kind, slots}; kind is smp (single-node only), "
            "mpi (multi-node capable), or other, derived from the scheduler's "
            "node-span rule (SGE allocation_rule; SLURM/PBS MaxNodes/nodect=1 => "
            "smp). SGE entries add allocation_rule; SLURM/PBS add max_nodes. "
            "Optional for back-compat with snapshots written before #293."
        ),
    )
