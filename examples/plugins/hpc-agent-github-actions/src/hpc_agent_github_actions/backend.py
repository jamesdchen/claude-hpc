"""``HPCBackend`` that fans hpc-agent task arrays out onto GitHub Actions.

A pure-API ("crowd-compute") backend, per
``docs/proposals/crowd-compute-backend.md``: no SSH, no shared filesystem. The
"scheduler" is the Actions REST API; an "array job of N tasks" is one workflow
run whose matrix has N cells; the "job id" is the Actions run id; results come
back as artifacts rather than over a shared mount.

How it slots into submit-flow's REAL call path
-----------------------------------------------
submit-flow's single-array path (``_make_single_array_submission``) builds a
command with :meth:`_build_command`, runs it with :meth:`_execute_command`, then
parses a job id out of stdout with ``JOB_ID_REGEX``. There is no shell command
to build, so :meth:`_build_command` encodes the dispatch intent and
:meth:`_execute_command` performs it — POST ``workflow_dispatch`` then resolve
the run id — returning a ``CompletedProcess`` whose stdout is the run id. (The
submit override therefore lives in ``_execute_command``, NOT the
``submit_array_tracked`` a marketplace skeleton stubs: that method is not on
submit-flow v1's path.)

The per-task kwargs are NOT shipped in the dispatch. Exactly as the SLURM
dispatcher calls ``resolve(SLURM_ARRAY_TASK_ID)`` on the compute node, the
workflow checks out the repo and each matrix cell resolves its kwargs from the
same ``.hpc/tasks.py`` against ``HPC_TASK_ID``. The dispatch carries only the
array size and the run identity (see ``workflow-template/fan-out.yml``).
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.infra.backends import BackendBuildContext, HPCBackend, register

from ._api import GitHubActionsAPI, GitHubAPIError

if TYPE_CHECKING:
    from pathlib import Path

# Config env — the API-shaped replacement for the SSH ssh_run / remote_repo pair
# the built-in backends take (consumed by ``from_build_context``).
REPO_ENV = "HPC_GHA_REPO"  # "owner/repo"
WORKFLOW_ENV = "HPC_GHA_WORKFLOW"  # workflow file name, e.g. "fan-out.yml"
REF_ENV = "HPC_GHA_REF"  # git ref the workflow runs on (default "main")
TOKEN_ENV = "GITHUB_TOKEN"  # PAT / Actions token: actions:write + actions:read

# Sentinel marking a ``_build_command`` payload so ``_execute_command`` knows it
# is an API dispatch request, not a shell argv.
_DISPATCH = "__gha_dispatch__"

# GitHub run statuses that mean "still going" (the alive bucket).
_ALIVE_STATUSES = frozenset(
    {"queued", "in_progress", "requested", "waiting", "pending", "action_required"}
)


def _parse_total(task_range: str | None) -> int:
    """Map submit-flow's 1-based ``"1-N"`` task range to the count N.

    ``None`` (a single non-array job, e.g. an MPI run) means one task.
    """
    if not task_range:
        return 1
    _, _, end = task_range.partition("-")
    return int(end or task_range)


@register("github-actions")
class GitHubActionsBackend(HPCBackend):
    """Fan task arrays out onto GitHub Actions runners."""

    scheduler_name = "github-actions"
    template_ext = ".yml"  # the deploy unit is the workflow file, not a scheduler script
    supports_test_only_eta = False

    def __init__(
        self,
        repo: str,
        workflow: str,
        ref: str = "main",
        token: str | None = None,
    ) -> None:
        self.repo = repo
        self.workflow = workflow
        self.ref = ref
        self.token = token or os.environ.get(TOKEN_ENV) or ""
        # No remote log directory on a runner; this only holds fetched copies.
        self.log_dir = os.path.join(".hpc", "gha-logs")
        self._api = GitHubActionsAPI(repo, self.token)

    @classmethod
    def from_build_context(cls, ctx: BackendBuildContext) -> GitHubActionsBackend:
        """Construct from submit-flow's build context, ignoring the SSH fields.

        Reads ``$HPC_GHA_REPO`` / ``$HPC_GHA_WORKFLOW`` / ``$HPC_GHA_REF`` /
        ``$GITHUB_TOKEN``. Missing required config fails loud with
        ``SpecInvalid`` (the pure-API analogue of a bad ssh_target) rather than
        dispatching into the void.
        """
        repo = os.environ.get(REPO_ENV)
        workflow = os.environ.get(WORKFLOW_ENV)
        token = os.environ.get(TOKEN_ENV)
        missing = [
            name
            for name, val in ((REPO_ENV, repo), (WORKFLOW_ENV, workflow), (TOKEN_ENV, token))
            if not val
        ]
        if missing:
            raise errors.SpecInvalid(
                "github-actions backend is missing required configuration: "
                f"{', '.join(missing)} must be set in the environment "
                "(see the plugin README)."
            )
        # Narrowed by the missing-check above; assertions keep the type checker happy.
        assert repo is not None and workflow is not None
        return cls(repo=repo, workflow=workflow, ref=os.environ.get(REF_ENV, "main"), token=token)

    # -- submission: no shell command, so encode the dispatch in _build_command
    #    and perform it in _execute_command (submit-flow's actual path). -------

    def _build_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        return [_DISPATCH, json.dumps({"task_range": task_range, "job_name": job_name})]

    def _execute_command(
        self,
        cmd: list[str],
        job_env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Perform the dispatch encoded by :meth:`_build_command`.

        Returns a ``CompletedProcess`` whose stdout is the Actions run id, so
        submit-flow's ``JOB_ID_REGEX`` parse and its
        ``RemoteCommandFailed``-on-nonzero handling both work unchanged. A
        GitHub API failure becomes a non-zero exit with the error on stderr.
        """
        if not cmd or cmd[0] != _DISPATCH:
            return subprocess.CompletedProcess(cmd, 3, "", f"not a gha dispatch payload: {cmd!r}")
        payload = json.loads(cmd[1])
        total = _parse_total(payload.get("task_range"))
        correlation = uuid.uuid4().hex
        inputs = {
            "correlation_id": correlation,
            "run_id": job_env.get("HPC_RUN_ID", ""),
            "total_tasks": str(total),
            "executor": job_env.get("EXECUTOR", ""),
            "cmd_sha": job_env.get("HPC_CMD_SHA", ""),
            "campaign_id": job_env.get("HPC_CAMPAIGN_ID", ""),
        }
        try:
            self._api.dispatch_workflow(self.workflow, self.ref, inputs)
            run_id = self._api.find_run(correlation=correlation)
        except GitHubAPIError as exc:
            return subprocess.CompletedProcess(cmd, 2, "", str(exc))
        return subprocess.CompletedProcess(cmd, 0, run_id, "")

    # -- liveness / state (host polls these from status / monitor / reconcile).

    def alive_job_ids(self, job_ids: list[str]) -> list[str]:
        """Subset of *job_ids* (Actions run ids) still running.

        A run that is absent (404) or ``completed`` is dropped; the host marks a
        vanished id ``abandoned`` on reconcile.
        """
        alive: list[str] = []
        for jid in job_ids:
            run = self._api.get_run(jid)
            if run is None:
                continue
            if str(run.get("status")) in _ALIVE_STATUSES:
                alive.append(jid)
        return alive

    @staticmethod
    def classify_scheduler_state(state: str) -> str:
        """Bucket a ``"<status>:<conclusion>"`` token into alive / error / held.

        Used by ``verify-submitted`` as a post-dispatch health check. A freshly
        dispatched run is ``queued`` / ``in_progress`` (alive); a finished run is
        bucketed by conclusion. ``cancelled`` maps to ``held`` — the GitHub
        analogue of a job cancelled by a newer dispatch (cf. ``preempted``).
        """
        status, _, conclusion = state.partition(":")
        if status != "completed":
            return "alive"
        if conclusion in {"failure", "startup_failure", "timed_out"}:
            return "error"
        if conclusion in {"cancelled", "action_required", "stale", "neutral", "skipped"}:
            return "held"
        return "alive"  # completed + success — ran cleanly

    # -- results / logs: no shared FS, so these come back over the API. The
    #    SSH path's rsync-pull and stderr-path reads have no backend hook to
    #    override, so these are offered as the building blocks to wire into
    #    aggregate / failures (see README "What still needs bridging"). --------

    def fetch_results(self, run_id: str, dest_dir: str) -> list[str]:
        """Download a run's artifacts into *dest_dir*; return the extracted dirs.

        The shared-filesystem replacement (the proposal's "each task ships data
        in and results out"). Downloads every artifact the run uploaded — the
        per-task ``task-*`` outputs and/or the ``reduced`` aggregate — and
        unzips each under ``dest_dir/<artifact-name>/``.
        """
        import zipfile

        os.makedirs(dest_dir, exist_ok=True)
        extracted: list[str] = []
        for art in self._api.list_artifacts(run_id):
            name = str(art.get("name", "artifact"))
            art_id = art.get("id")
            if not isinstance(art_id, int):
                continue
            zip_path = os.path.join(dest_dir, f"{name}.zip")
            self._api.download_artifact(art_id, zip_path)
            out_dir = os.path.join(dest_dir, name)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(out_dir)
            os.remove(zip_path)
            extracted.append(out_dir)
        return extracted

    def fetch_logs(self, run_id: str, dest_dir: str | None = None) -> str:
        """Download a run's job-logs zip; return its path.

        The instance-method replacement for the ``stderr_log_path`` staticmethod:
        Actions logs need the authenticated client, which a ``@staticmethod``
        can't hold. Defaults to writing under ``self.log_dir``.
        """
        dest_root = dest_dir or self.log_dir
        os.makedirs(dest_root, exist_ok=True)
        dest = os.path.join(dest_root, f"{run_id}-logs.zip")
        self._api.download_run_logs(run_id, dest)
        return dest
