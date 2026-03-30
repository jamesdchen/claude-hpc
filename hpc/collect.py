"""Core collector — generates .hpc/ artefacts from a project directory."""

from __future__ import annotations

__all__ = ["collect"]

import argparse
import ast
import hashlib
import importlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from hpc._config import load_project_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_to_path(module: str, project_root: Path) -> Path | None:
    """Resolve a dotted module path to a .py file under *project_root*."""
    rel = module.replace(".", "/")
    # Try as a direct .py file first, then as a package __init__.
    candidate = project_root / f"{rel}.py"
    if candidate.is_file():
        return candidate
    candidate = project_root / rel / "__init__.py"
    if candidate.is_file():
        return candidate
    return None


def _extract_module_from_executor(executor: str) -> str | None:
    """Parse ``python3 -m some.module ...`` → ``"some.module"``."""
    m = re.search(r"-m\s+([\w.]+)", executor)
    return m.group(1) if m else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1. Module graph
# ---------------------------------------------------------------------------


def _local_imports_from_file(filepath: Path, project_root: Path) -> list[str]:
    """Return dotted module names imported by *filepath* that resolve locally."""
    try:
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError):
        return []

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_to_path(alias.name, project_root):
                    modules.append(alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and _module_to_path(node.module, project_root)
        ):
            modules.append(node.module)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for m in modules:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    return deduped


def trace_imports(
    module: str,
    project_root: Path,
    depth: int = 2,
    _expanded: set[str] | None = None,
    _visited_files: list[Path] | None = None,
) -> tuple[list[Any], list[Path]]:
    """Trace local imports via AST, returning a YAML-friendly nested list.

    Each entry is either a plain string (leaf) or a single-key dict mapping
    a relative path to its children list.

    *_expanded* tracks modules already expanded (for dedup across the tree).
    *_visited_files* accumulates every source file encountered.
    """
    if _expanded is None:
        _expanded = set()
    if _visited_files is None:
        _visited_files = []

    filepath = _module_to_path(module, project_root)
    if filepath is None:
        return [], _visited_files

    rel = str(filepath.relative_to(project_root))

    if filepath not in _visited_files:
        _visited_files.append(filepath)

    # Already expanded elsewhere → leaf.
    if module in _expanded:
        return [rel], _visited_files

    _expanded.add(module)

    if depth <= 0:
        return [rel], _visited_files

    children_modules = _local_imports_from_file(filepath, project_root)
    if not children_modules:
        return [rel], _visited_files

    children: list[Any] = []
    for child_mod in children_modules:
        child_path = _module_to_path(child_mod, project_root)
        if child_path is None:
            continue

        child_rel = str(child_path.relative_to(project_root))

        if child_mod in _expanded:
            # Already expanded — emit as leaf.
            children.append(child_rel)
            if child_path not in _visited_files:
                _visited_files.append(child_path)
        else:
            sub, _visited_files = trace_imports(
                child_mod,
                project_root,
                depth=depth - 1,
                _expanded=_expanded,
                _visited_files=_visited_files,
            )
            if len(sub) == 1 and isinstance(sub[0], str):
                children.append(sub[0])
            elif sub:
                children.extend(sub)

    if children:
        return [{rel: children}], _visited_files
    return [rel], _visited_files


def _build_module_graph(
    project: dict[str, Any], project_root: Path
) -> tuple[dict[str, Any], list[Path]]:
    """Build module_graph.yaml content. Returns (graph_dict, visited_files)."""
    stages = project.get("stages", {})
    graph: dict[str, Any] = {}
    all_visited: list[Path] = []

    for stage_name, stage_cfg in stages.items():
        executor = stage_cfg.get("executor", "")
        module = _extract_module_from_executor(executor)
        if module is None:
            continue
        tree, visited = trace_imports(module, project_root, depth=2)
        graph[stage_name] = tree
        for p in visited:
            if p not in all_visited:
                all_visited.append(p)

    return graph, all_visited


# ---------------------------------------------------------------------------
# 2. CLI help
# ---------------------------------------------------------------------------

_ARG_RE = re.compile(
    r"^\s+"
    r"(?:(-\w),\s+)?"  # optional short flag
    r"(--[\w-]+)"  # long flag
    r"(?:\s+\{([^}]+)\})?"  # optional choices
    r"(?:\s+(\S+))?"  # optional metavar
    r"(?:\s{2,}(.*))?$"  # optional help text
)
_DEFAULT_RE = re.compile(r"\(default:\s*(.+?)\)")
_METAVAR_TYPE_MAP: dict[str, str] = {
    "INT": "int",
    "FLOAT": "float",
    "STR": "str",
    "PATH": "str",
    "FILE": "str",
    "DIR": "str",
}


def parse_argparse_output(raw: str) -> list[dict[str, Any]]:
    """Parse argparse ``--help`` output into a list of arg dicts."""
    args: list[dict[str, Any]] = []
    for line in raw.splitlines():
        m = _ARG_RE.match(line)
        if not m:
            continue
        _short, long, choices_raw, metavar, help_text = m.groups()
        entry: dict[str, Any] = {"name": long}

        if choices_raw:
            entry["choices"] = [c.strip() for c in choices_raw.split(",")]

        if metavar:
            mapped = _METAVAR_TYPE_MAP.get(metavar.upper())
            if mapped:
                entry["type"] = mapped

        help_text = help_text or ""

        dm = _DEFAULT_RE.search(help_text)
        if dm:
            entry["default"] = dm.group(1)

        if "required" in help_text.lower():
            entry["required"] = True
        elif "default" not in entry and choices_raw is None:
            # No default and no choices — likely required.
            entry["required"] = True

        if help_text:
            cleaned = _DEFAULT_RE.sub("", help_text).strip()
            if cleaned:
                entry["help"] = cleaned

        args.append(entry)
    return args


def _run_help(module: str) -> dict[str, Any]:
    """Run ``python3 -m <module> --help`` and parse the output."""
    try:
        result = subprocess.run(
            ["python3", "-m", module, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw_help = result.stdout or result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"module": module, "raw_help": f"ERROR: {exc}", "args": []}

    parsed_args = parse_argparse_output(raw_help)
    return {"module": module, "raw_help": raw_help, "args": parsed_args}


def _build_cli_help(project: dict[str, Any]) -> dict[str, Any]:
    """Build cli_help.yaml content."""
    stages = project.get("stages", {})
    result: dict[str, Any] = {"stages": {}}

    for stage_name, stage_cfg in stages.items():
        entry: dict[str, Any] = {}

        executor = stage_cfg.get("executor", "")
        module = _extract_module_from_executor(executor)
        if module:
            entry["executor"] = _run_help(module)

        agg_cmd = stage_cfg.get("aggregate_cmd", "")
        agg_module = _extract_module_from_executor(agg_cmd) if agg_cmd else None
        if agg_module:
            entry["aggregate"] = _run_help(agg_module)

        if entry:
            result["stages"][stage_name] = entry

    return result


# ---------------------------------------------------------------------------
# 3. Experiments
# ---------------------------------------------------------------------------


def _build_experiments(project: dict[str, Any], project_root: Path) -> dict[str, Any]:
    """Build experiments.yaml content."""
    result: dict[str, Any] = {}

    # --- experiment files ---
    exp_paths = project.get("experiment_paths")
    if exp_paths:
        experiments: list[dict[str, Any]] = []
        for pattern in exp_paths:
            for match in sorted(project_root.glob(pattern)):
                try:
                    data = yaml.safe_load(match.read_text()) or {}
                except (yaml.YAMLError, OSError):
                    data = {}
                entry: dict[str, Any] = {
                    "path": str(match.relative_to(project_root)),
                }
                entry.update(data)
                experiments.append(entry)
        result["experiments"] = experiments

    # --- registries ---
    registries_cfg = project.get("registries")
    if registries_cfg:
        registries: dict[str, Any] = {}
        for reg_entry in registries_cfg:
            # Format: "module.path:ATTR"
            if ":" not in reg_entry:
                continue
            mod_path, attr = reg_entry.rsplit(":", 1)
            try:
                mod = importlib.import_module(mod_path)
                val = getattr(mod, attr)
                if isinstance(val, dict):
                    registries[attr.lower()] = dict(val)
                elif isinstance(val, (list, tuple)):
                    registries[attr.lower()] = list(val)
                else:
                    registries[attr.lower()] = val
            except Exception:  # noqa: BLE001
                registries[attr.lower()] = None
        result["registries"] = registries

    return result


# ---------------------------------------------------------------------------
# 4. Meta
# ---------------------------------------------------------------------------


def _build_meta(visited_files: list[Path], project_root: Path) -> dict[str, Any]:
    """Build _meta.yaml content."""
    sources: list[dict[str, str]] = []
    for fp in visited_files:
        sources.append(
            {
                "path": str(fp.relative_to(project_root)),
                "sha256": _sha256(fp),
            }
        )
    return {
        "collected_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect(project_root: Path | None = None) -> Path:
    """Run the full collection pipeline and write .hpc/ artefacts.

    Returns the path to the ``.hpc/`` directory.
    """
    if project_root is None:
        project_root = Path.cwd()
    project_root = project_root.resolve()

    project = load_project_config(project_root / "project.yaml")

    hpc_dir = project_root / ".hpc"
    hpc_dir.mkdir(exist_ok=True)

    # 1. Module graph
    module_graph, visited_files = _build_module_graph(project, project_root)
    (hpc_dir / "module_graph.yaml").write_text(
        yaml.safe_dump(module_graph, default_flow_style=False, sort_keys=False)
    )

    # 2. CLI help
    cli_help = _build_cli_help(project)
    (hpc_dir / "cli_help.yaml").write_text(
        yaml.safe_dump(cli_help, default_flow_style=False, sort_keys=False)
    )

    # 3. Experiments
    experiments = _build_experiments(project, project_root)
    if experiments:
        (hpc_dir / "experiments.yaml").write_text(
            yaml.safe_dump(experiments, default_flow_style=False, sort_keys=False)
        )

    # 4. Meta
    meta = _build_meta(visited_files, project_root)
    (hpc_dir / "_meta.yaml").write_text(
        yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    )

    return hpc_dir


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect .hpc/ artefacts.")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root directory (default: cwd)",
    )
    ns = parser.parse_args()
    out = collect(ns.project_root)
    print(f"Collected → {out}")
