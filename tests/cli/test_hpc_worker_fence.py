"""The inline ``hpc-worker`` subagent is invoke-only.

Its frontmatter ``PreToolUse`` hook blocks any Bash command that isn't
``hpc-agent`` / ``git`` — the inline-path analog of the ``--bare`` worker's
``--allowedTools`` fence (and stricter: it also rejects shell chaining /
substitution, so ``hpc-agent x && rm -rf /`` can't smuggle a second command).
Subagent frontmatter ``tools:`` can't command-scope Bash, so a frontmatter
PreToolUse hook is the only mechanism that enforces this separately from the
parent session's permissions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_AGENT = Path(__file__).resolve().parents[2] / "src/slash_commands/agents/hpc-worker.md"


def _find_bash() -> str | None:
    """A real POSIX bash interpreter.

    On ``windows-latest`` the ``bash`` first on PATH is the WSL launcher stub
    in ``System32`` — with no distro installed it prints an error and exits 1
    for *any* input, so the fence hook never runs and every assertion fails on
    a spurious rc=1. Prefer Git Bash (always present on the runner) there; fall
    back to PATH ``bash`` elsewhere.
    """
    if sys.platform == "win32":
        for p in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ):
            if Path(p).is_file():
                return p
    return shutil.which("bash")


_BASH = _find_bash()


def _bash_has_jq() -> bool:
    """True iff the resolved bash can actually run ``jq`` (the hook shells it).

    Probes through *that* bash rather than the host PATH so a Git-Bash that
    can't see ``jq`` is detected (and skipped) instead of failing mid-hook.
    """
    if _BASH is None:
        return False
    try:
        return (
            subprocess.run(
                [_BASH, "-c", "command -v jq"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )
    except OSError:
        return False


needs_jq = pytest.mark.skipif(
    not _bash_has_jq(), reason="the hook needs a POSIX bash that can run jq"
)


def _hook_command() -> str:
    fm = _AGENT.read_text(encoding="utf-8").split("---")[1]
    doc = yaml.safe_load(fm)
    pre = doc["hooks"]["PreToolUse"]
    entry = next(e for e in pre if e["matcher"] == "Bash")
    return entry["hooks"][0]["command"]


def _rc(cmd: str) -> int:
    payload = json.dumps({"tool_input": {"command": cmd}})
    return subprocess.run(
        [_BASH, "-c", _hook_command()],
        input=payload,
        text=True,
        capture_output=True,
        timeout=30,
    ).returncode


@needs_jq
@pytest.mark.parametrize(
    "cmd",
    [
        "hpc-agent submit-flow --spec spec.json --experiment-dir .",
        "hpc-agent status --run-id r1",
        "git commit -m 'scaffold tasks.py + cli.py'",
    ],
)
def test_allows_hpc_agent_and_git(cmd: str) -> None:
    assert _rc(cmd) == 0


@needs_jq
@pytest.mark.parametrize(
    "cmd",
    [
        "ssh host qstat",
        "rsync -az a b",
        "scp a b",
        "scancel 12345",
        "qsub job.sh",
        "python -c import_os",
        "curl http://example.com",
        "hpc-agent submit && rm -rf /",  # chaining must not smuggle a 2nd command
        "git status | sh",  # pipe to a shell is chaining
        "rm -rf /",
    ],
)
def test_blocks_everything_else(cmd: str) -> None:
    assert _rc(cmd) == 2
