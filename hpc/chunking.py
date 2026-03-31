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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

__all__ = ["ChunkContext", "chunk_context", "collect_chunks"]


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


def collect_chunks(
    result_dir: str | Path,
    pattern: str = "results_chunk_*.csv",
    date_column: str = "date",
) -> pd.DataFrame:
    """Stitch chunk CSVs into a single sorted DataFrame.

    Companion to :func:`chunk_context` — handles the fan-in after parallel
    execution.  Returns an empty ``DataFrame`` if no matching files are found.

    Parameters
    ----------
    result_dir : str or Path
        Directory containing chunk CSV files.
    pattern : str
        Glob pattern for chunk files (default matches ``ctx.output_path()``).
    date_column : str
        Column to parse as datetime and use as sorted index.  Ignored if the
        column does not exist in the data.
    """
    import pandas as pd

    files = sorted(Path(result_dir).glob(pattern))
    if not files:
        return pd.DataFrame()

    dfs: list[pd.DataFrame] = []
    for f in files:
        try:
            dfs.append(pd.read_csv(f))
        except (OSError, pd.errors.ParserError, UnicodeDecodeError):
            continue

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    if date_column in combined.columns:
        combined[date_column] = pd.to_datetime(combined[date_column])
        combined = combined.set_index(date_column).sort_index()
    return combined
