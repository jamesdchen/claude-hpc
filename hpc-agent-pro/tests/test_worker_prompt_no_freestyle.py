"""Guard: pro's worker-prompt overlays must stay invoke-only.

The host fences the spawned/inline worker to ``Bash(hpc-agent:*)`` + ``git``
(see core ``_WORKER_ALLOWED_TOOLS`` and the ``hpc-worker`` PreToolUse hook).
Pro overlays the ``submit`` worker prompt via ``worker_prompt_assets``, so when
pro is installed the worker runs *pro's* prompt — which therefore must also be
free of executable freestyle (inline ``python``, ``hpc_agent`` imports, raw
``ssh``/``rsync``). Core's submit.md is guarded by its prefix snapshot; pro's
overlay had no guard and silently re-acquired the freestyle the fence forbids.
This test is that guard.

It scans the rendered text of every overlaid prompt for patterns that only a
non-``hpc-agent``/``git`` Bash command (or a python code fence) would produce.
``hpc-agent`` / ``git`` invocations and prose that *names* a forbidden command
to say "do NOT do this" are allowed; an executable ```python fence or an
``import``/``from hpc_agent`` statement is not.
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

# Workflows pro overlays (mirror plugin.py:worker_prompt_overlays).
_OVERLAID = ("submit",)

# Patterns that indicate the worker is being told to execute non-invoke code.
# Each is matched line-by-line; matches in fenced ```python blocks or as bare
# import / shell-out statements are failures.
_FORBIDDEN = (
    (re.compile(r"^```python\b"), "python code fence (worker can't shell python)"),
    (re.compile(r"^\s*(import|from)\s+hpc_agent\b"), "hpc_agent internal import"),
    (re.compile(r"^\s*from\s+hpc_agent_pro\b"), "hpc_agent_pro internal import"),
    (re.compile(r"\.write_text\("), "inline file write (use the Write tool)"),
    (re.compile(r"\bdatetime\.now\("), "inline python (use a primitive / Write tool)"),
    (re.compile(r"python3?\s+-c\b"), "python -c shell-out"),
    (re.compile(r"python\s+\.hpc/scaffold\.py"), "scaffold.py shell-out (use discover-runs)"),
)


def _prompt_text(workflow: str) -> str:
    return (files("hpc_agent_pro") / "worker_prompts" / f"{workflow}.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("workflow", _OVERLAID)
def test_pro_worker_prompt_has_no_executable_freestyle(workflow: str) -> None:
    text = _prompt_text(workflow)
    violations: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, why in _FORBIDDEN:
            if pattern.search(line):
                violations.append(f"{workflow}.md:{lineno}: {why}\n    {line.strip()}")
    assert not violations, (
        "pro worker prompt contains executable freestyle the worker fence forbids "
        "(route through `hpc-agent <verb>` or the Read/Write/Grep/Glob tools):\n\n"
        + "\n".join(violations)
    )


def test_overlaid_set_matches_plugin_manifest() -> None:
    """If pro starts overlaying another workflow, this test must cover it too."""
    from hpc_agent_pro.plugin import MANIFEST

    assert set(MANIFEST.worker_prompt_overlays) == set(_OVERLAID), (
        "pro's worker_prompt_overlays changed; update _OVERLAID in this test so "
        "the new overlay is also guarded against executable freestyle."
    )
