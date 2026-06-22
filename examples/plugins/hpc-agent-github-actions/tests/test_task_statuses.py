"""``GitHubActionsBackend.task_statuses`` derives per-task status over the API.

No live GitHub calls: a fake pooled API stands in for ``get_run`` /
``list_artifacts`` so the test pins the mapping (uploaded ``task-<i>`` artifact →
complete; missing-but-run-alive → running; missing-and-run-done → failed) the
host monitor reads through the ``HPCBackend.task_statuses`` hook (#337 follow-up).
"""

from __future__ import annotations

import pytest
from hpc_agent_github_actions.backend import GitHubActionsBackend


class _FakeAPI:
    """Stand-in for one pooled ``GitHubActionsAPI`` account."""

    def __init__(self, run_status: str, artifact_task_ids: list[int]) -> None:
        self._run_status = run_status
        self._artifacts = [{"name": f"task-{i}", "id": i} for i in artifact_task_ids]

    def get_run(self, run_id: str) -> dict[str, object] | None:
        return {"status": self._run_status}

    def list_artifacts(self, run_id: str) -> list[dict[str, object]]:
        return list(self._artifacts)


def _backend_with(api: _FakeAPI) -> GitHubActionsBackend:
    backend = GitHubActionsBackend(repo="o/r", token="t")
    backend._accounts = [api]  # type: ignore[assignment]
    return backend


def test_completed_tasks_from_artifacts_while_run_alive() -> None:
    # tasks 0,2 uploaded; run still in progress → the rest are 'running'.
    backend = _backend_with(_FakeAPI(run_status="in_progress", artifact_task_ids=[0, 2]))

    statuses = backend.task_statuses(["999"], total_tasks=4)

    assert statuses == {0: "complete", 1: "running", 2: "complete", 3: "running"}


def test_missing_tasks_fail_once_run_is_done() -> None:
    # Run finished; tasks 0 uploaded, 1 never did → 1 is 'failed', not 'running'.
    backend = _backend_with(_FakeAPI(run_status="completed", artifact_task_ids=[0]))

    statuses = backend.task_statuses(["999"], total_tasks=2)

    assert statuses == {0: "complete", 1: "failed"}


@pytest.mark.parametrize("total", [0, 1, 3])
def test_keys_span_total_tasks(total: int) -> None:
    backend = _backend_with(_FakeAPI(run_status="completed", artifact_task_ids=[]))
    statuses = backend.task_statuses(["999"], total_tasks=total)
    assert sorted(statuses) == list(range(total))
