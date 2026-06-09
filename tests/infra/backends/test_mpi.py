"""MPI / multi-rank submission behaviour (#293).

Covers the three PR2 seams: ``resource_flags`` MPI slot grammar per family,
the non-array (``array=False``) command path a single multi-rank job uses,
and the ``MpiSpec`` wire guards. The single-node ``resource_flags`` path is
unchanged and stays covered by the per-family suites.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hpc_agent._wire.workflows.submit_flow import MpiSpec, SubmitResources
from hpc_agent.infra.backends.sge import SGEBackend
from hpc_agent.infra.backends.slurm import SlurmBackend


def _slurm():
    return SlurmBackend(script="mpi.slurm", log_dir="logs")


def _sge():
    return SGEBackend(script="mpi.sh", log_dir="logs")


# --- resource_flags: MPI slot grammar -------------------------------------


def test_slurm_mpi_flags_emit_nodes_ntasks_topology():
    r = SubmitResources(
        mpi=MpiSpec(ranks=8, ranks_per_node=4, threads_per_rank=2, launcher="srun"),
        walltime_sec=3600,
        mem_mb=8000,
    )
    flags = _slurm().resource_flags(r)
    # nodes = ranks / ranks_per_node = 2; threads -> --cpus-per-task; walltime
    # and mem reuse the single-node helpers (ceil-minutes / M suffix).
    assert flags == [
        "--nodes",
        "2",
        "--ntasks",
        "8",
        "--ntasks-per-node",
        "4",
        "--cpus-per-task",
        "2",
        "--time",
        "60",
        "--mem",
        "8000M",
    ]


def test_slurm_mpi_without_ranks_per_node_omits_node_pinning():
    r = SubmitResources(mpi=MpiSpec(ranks=16, launcher="srun"))
    flags = _slurm().resource_flags(r)
    # No ranks_per_node -> let the scheduler pack; --nodes/--ntasks-per-node
    # are absent, --ntasks still pins the total rank count.
    assert "--ntasks" in flags and "16" in flags
    assert "--nodes" not in flags
    assert "--ntasks-per-node" not in flags
    # threads_per_rank defaults to 1 -> no --cpus-per-task noise.
    assert "--cpus-per-task" not in flags


def test_sge_mpi_flags_route_through_parallel_environment():
    r = SubmitResources(mpi=MpiSpec(ranks=16, launcher="mpirun", pe_name="mpi"), walltime_sec=7200)
    flags = _sge().resource_flags(r)
    assert flags == ["-pe", "mpi", "16", "-l", "h_rt=02:00:00"]


def test_pbspro_mpi_flags_emit_select_chunk():
    from hpc_agent.infra.backends._engine import ProfileBackend
    from hpc_agent.infra.backends.profile import PBSPRO_PROFILE

    class _PBS(ProfileBackend):
        profile = PBSPRO_PROFILE

    b = _PBS.__new__(_PBS)
    b.script = "mpi.pbs"
    b.log_dir = "logs"
    r = SubmitResources(
        mpi=MpiSpec(ranks=8, ranks_per_node=4, threads_per_rank=2, launcher="mpirun"),
        walltime_sec=3600,
        mem_mb=4000,
    )
    flags = b.resource_flags(r)
    # 2 chunks of 4 procs × 2 threads = 8 ncpus/chunk, ompthreads + mem folded in.
    assert flags == [
        "-l",
        "select=2:ncpus=8:mpiprocs=4:ompthreads=2:mem=4000mb",
        "-l",
        "walltime=01:00:00",
    ]


# --- non-array command path (single multi-rank job) ------------------------


def test_slurm_single_mpi_job_omits_array_and_uses_jobid_logs():
    cmd = _slurm()._build_command(None, "solve", {}, array=False)
    assert "--array" not in cmd
    # %A_%a (array) gives way to %j (single job id) so the log name is non-empty.
    assert "logs/%x_%j.out" in cmd
    assert "logs/%x_%j.err" in cmd
    assert cmd[-1] == "mpi.slurm"


def test_sge_single_mpi_job_omits_t_flag():
    cmd = _sge()._build_command(None, "solve", {}, array=False)
    assert "-t" not in cmd
    assert cmd[-1] == "mpi.sh"


def test_array_path_unchanged_when_array_true():
    # Regression: the default (array fan-out) command is byte-identical to before.
    cmd = _slurm()._build_command("1-10", "sweep", {})
    assert cmd[:4] == ["sbatch", "--array", "1-10", "--job-name"]
    assert "logs/%x_%A_%a.out" in cmd


# --- MpiSpec wire guards ---------------------------------------------------


def test_ranks_per_node_must_divide_ranks():
    with pytest.raises(ValidationError, match="does not evenly divide"):
        MpiSpec(ranks=6, ranks_per_node=4, launcher="srun")


def test_ranks_per_node_dividing_ranks_is_accepted():
    m = MpiSpec(ranks=12, ranks_per_node=3, launcher="srun")
    assert m.ranks == 12 and m.ranks_per_node == 3


def test_launcher_is_closed_set():
    with pytest.raises(ValidationError):
        MpiSpec(ranks=4, launcher="poe")  # type: ignore[arg-type]
