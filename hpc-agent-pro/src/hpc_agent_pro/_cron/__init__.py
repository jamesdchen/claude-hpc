"""Cron-installable scripts shipped with the pro plugin.

Each module here exposes a CLI entry point invokable via ``python -m
hpc_agent_pro._cron.<module>``. They are NOT primitives — they are
the *targets* of cron lines installed by the ``install-cron``
primitive (``hpc_agent_pro._cron.install``).

Three modules live here:

* :mod:`snapshot_squeue` — every-5-minute writer of column-projected
  squeue snapshots into ``<experiment>/.hpc/squeue_snapshots/``. The
  queue-wait predictor reads these for its training data.
* :mod:`train_wait_predictor` — nightly refit of the LightGBM-residual
  regression from accumulated snapshots + sacct history.
* :mod:`extract_sacct_history` — sacct → JSON dump the trainer pairs
  with snapshots to derive ``(features, observed_overhead)`` rows.

They were promoted from the repo's top-level ``scripts/`` directory
into the package so a ``pip install hpc-agent-pro`` ships them. The
``install-cron`` primitive's cron lines reference them via
``python -m hpc_agent_pro._cron.<name>`` — no absolute repo path
required at install time.
"""
