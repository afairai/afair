"""Small SQLite helpers shared across the substrate.

The only inhabitant so far is the parameter chunker. SQLite compiles a
statement with a hard cap on host parameters (``SQLITE_MAX_VARIABLE_NUMBER``
— 32,766 since 3.32, 999 on older builds). Any batch helper that expands a
Python list into an ``IN (?, ?, ...)`` clause must chunk the list below that
ceiling; otherwise a large input raises
``sqlite3.OperationalError: too many SQL variables`` and takes the whole
recall / cold-path cycle down with it. The vaults where this bites are the
real ones — an operator whose own name is mentioned in tens of thousands of
events, an entity graph past ~33k edges.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

SQLITE_SAFE_VARIABLE_LIMIT = 900
"""Max host parameters packed into a single IN-list. Kept well under SQLite's
historical 999 floor so a helper stays safe regardless of the linked SQLite
build, with headroom for a few additional bound parameters in the same
statement."""


def iter_param_chunks[T](
    items: Sequence[T], size: int = SQLITE_SAFE_VARIABLE_LIMIT
) -> Iterator[list[T]]:
    """Yield ``items`` in lists of at most ``size``.

    Each element appears in exactly ONE chunk, so a caller can merge per-chunk
    result dicts without double-counting or cross-chunk conflict. Empty input
    yields nothing.
    """
    if size < 1:
        msg = "chunk size must be >= 1"
        raise ValueError(msg)
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
