"""Pydantic models that author the pro plugin's wire schemas under
``hpc_agent_pro/schemas/``.

Mirrors the host package's ``hpc_agent._wire`` posture: the JSON
files emitted into ``hpc_agent_pro/schemas/`` are the wire SoT that
external agents and the in-process validation boundary read; the
Pydantic models in this package are the authoring SoT.
``scripts/build_schemas.py`` walks this subpackage (plus
``hpc_agent._wire`` for core) and rewrites the JSON via
``model_json_schema()``. Pre-commit + CI gate the round-trip via
``--check``; ``--write`` regenerates.

To add a new schema: define the model under the matching
``queries/`` / ``validators/`` / ``workflows/`` subpackage, then
append a ``(model, json_path)`` entry to ``SCHEMA_REGISTRY`` in
``scripts/build_schemas.py`` and run ``--write``.
"""
