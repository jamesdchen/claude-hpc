"""Chunking protocol for HPC-parallel executors.

Provides ``ChunkContext`` — a no-op locally (processes everything),
active on HPC (processes the assigned subset).  Experiment authors call
``chunk_context()`` and ``ctx.split()`` without thinking about
parallelisation; claude-hpc templates inject the env vars that drive it.

Typical usage in an executor::

    from hpc.chunking import chunk_context

    ctx = chunk_context()                       # no-op locally (0 of 1)
    my_range = ctx.split(range(train_win, N))   # full range locally
    results.to_csv(ctx.output_path())           # ./results_chunk_1.csv
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ChunkContext", "chunk_context"]


@dataclass(frozen=True)
class ChunkContext:
    """Immutable description of which chunk this process owns."""

    chunk_id: int  # 0-indexed
    total_chunks: int
    result_dir: Path

    def split(self, items: range | int) -> range:
        """Return this chunk's contiguous slice of *items*.

        Parameters
        ----------
        items : range or int
            A ``range`` of work indices, or an ``int`` shorthand for
            ``range(items)``.

        Returns
        -------
        range
            The sub-range assigned to this chunk.  When running locally
            (chunk 0 of 1) the full range is returned unchanged.
        """
        if isinstance(items, int):
            items = range(items)
        n = len(items)
        per = n // self.total_chunks
        rem = n % self.total_chunks
        # Distribute remainder: first `rem` chunks get one extra item.
        start = per * self.chunk_id + min(self.chunk_id, rem)
        end = start + per + (1 if self.chunk_id < rem else 0)
        return range(items.start + start, items.start + end)

    def output_path(self, prefix: str = "results_chunk") -> Path:
        """Standard chunk output path: ``{result_dir}/{prefix}_{chunk_id+1}.csv``."""
        return self.result_dir / f"{prefix}_{self.chunk_id + 1}.csv"


def chunk_context() -> ChunkContext:
    """Build a :class:`ChunkContext` from environment variables.

    claude-hpc job templates export ``CHUNK_ID``, ``TOTAL_CHUNKS``, and
    ``RESULT_DIR``.  When none are set (local development) the context
    defaults to chunk 0 of 1, which means "process everything".
    """
    return ChunkContext(
        chunk_id=int(os.environ.get("CHUNK_ID", "0")),
        total_chunks=int(os.environ.get("TOTAL_CHUNKS", "1")),
        result_dir=Path(os.environ.get("RESULT_DIR", ".")),
    )
