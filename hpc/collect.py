"""Core collector — generates .hpc/ artefacts from a project directory."""

from __future__ import annotations

__all__ = ["collect"]

import argparse
import ast
import hashlib
import importlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from hpc.manifest import load_manifest, normalize_profile

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


def _extract_executor_target(executor: str) -> tuple[str | None, str]:
    """Extract the module or script path from an executor command.

    Returns ``(module_or_path, kind)`` where *kind* is ``"module"`` or
    ``"script"``.  Returns ``(None, "")`` if parsing fails.
    """
    # python3 -m some.module ...
    m = re.search(r"-m\s+([\w.]+)", executor)
    if m:
        return m.group(1), "module"
    # python3 scripts/train.py ... or python scripts/train.py ...
    m = re.search(r"python[3]?\s+([\w./\\-]+\.py)", executor)
    if m:
        return m.group(1), "script"
    return None, ""


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


def _trace_target(
    target: str,
    kind: str,
    project_root: Path,
    expanded: set[str],
    visited: list[Path],
) -> tuple[list[Any], list[Path]]:
    """Trace imports for a single executor/aggregate target."""
    if kind == "module":
        module = target
    else:
        module = target.replace("/", ".").replace("\\", ".").removesuffix(".py")

    tree, visited = trace_imports(
        module, project_root, depth=2, _expanded=expanded, _visited_files=visited
    )
    return tree, visited


def _build_module_graph(
    manifest: dict[str, Any], project_root: Path
) -> tuple[dict[str, Any], list[Path]]:
    """Build module_graph.yaml from hpc.yaml manifest."""
    graph: dict[str, Any] = {}
    all_visited: list[Path] = []
    expanded: set[str] = set()

    profiles = manifest.get("profiles")
    if profiles is None:
        # Single-profile shorthand — treat as one profile named after the project
        profiles = {manifest.get("project", "default"): manifest}

    for prof_name, prof_cfg in profiles.items():
        stages = normalize_profile(prof_cfg)

        for stg_name, stg_cfg in stages.items():
            # Build the graph key
            if stg_name == "default" and len(stages) == 1:
                key = prof_name
            else:
                key = f"{prof_name}.{stg_name}"

            entries: list[Any] = []

            # Trace the run command
            run_cmd = stg_cfg.get("run", "")
            target, kind = _extract_executor_target(run_cmd)
            if target:
                tree, all_visited = _trace_target(
                    target, kind, project_root, expanded, all_visited
                )
                entries.extend(tree)

            # Trace the aggregate command
            results = stg_cfg.get("results", {})
            agg_cmd = results.get("aggregate_cmd", "")
            if agg_cmd:
                agg_target, agg_kind = _extract_executor_target(agg_cmd)
                if agg_target:
                    tree, all_visited = _trace_target(
                        agg_target, agg_kind, project_root, expanded, all_visited
                    )
                    entries.extend(tree)

            if entries:
                graph[key] = entries

    return graph, all_visited


# ---------------------------------------------------------------------------
# 2. Experiments
# ---------------------------------------------------------------------------


def _build_experiments(manifest: dict[str, Any], project_root: Path) -> dict[str, Any]:
    """Build experiments.yaml content from hpc.yaml fields."""
    result: dict[str, Any] = {}

    # --- experiment files ---
    exp_paths = manifest.get("experiment_paths")
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
    registries_cfg = manifest.get("registries")
    if registries_cfg and isinstance(registries_cfg, dict):
        registries: dict[str, Any] = {}
        for reg_name, reg_ref in registries_cfg.items():
            # Format: "module.path:ATTR"
            if not isinstance(reg_ref, str) or ":" not in reg_ref:
                continue
            mod_path, attr = reg_ref.rsplit(":", 1)
            try:
                mod = importlib.import_module(mod_path)
                val = getattr(mod, attr)
                if isinstance(val, dict):
                    registries[reg_name] = dict(val)
                elif isinstance(val, (list, tuple)):
                    registries[reg_name] = list(val)
                else:
                    registries[reg_name] = val
            except Exception:  # noqa: BLE001
                registries[reg_name] = None
        result["registries"] = registries

    return result


# ---------------------------------------------------------------------------
# 3. Meta
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

    manifest = load_manifest(project_root / "hpc.yaml")

    hpc_dir = project_root / ".hpc"
    hpc_dir.mkdir(exist_ok=True)

    # 1. Module graph
    module_graph, visited_files = _build_module_graph(manifest, project_root)
    (hpc_dir / "module_graph.yaml").write_text(
        yaml.safe_dump(module_graph, default_flow_style=False, sort_keys=False)
    )

    # 2. Experiments
    experiments = _build_experiments(manifest, project_root)
    if experiments:
        (hpc_dir / "experiments.yaml").write_text(
            yaml.safe_dump(experiments, default_flow_style=False, sort_keys=False)
        )

    # 3. Meta
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
