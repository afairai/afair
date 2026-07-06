"""Parameter-chunker + batch-helper var-limit tests (P0-5b).

SQLite caps host parameters per statement. Batch helpers that expand a
Python list into ``IN (?, ?, ...)`` used to bind one variable per id, so a
large input raised ``sqlite3.OperationalError: too many SQL variables`` and
took the whole recall / cold-path cycle down. The chunker splits the list
below the safe limit; these tests prove the unit contract and that every
chunked helper survives a 40k-id input.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    latest_edge_confidence_batch,
    latest_edge_scores_batch,
    open_db,
    read_entities_batch,
)
from afair.substrate.entities import latest_edge_reviews_batch, resolve_canonical_batch
from afair.substrate.sqlutil import SQLITE_SAFE_VARIABLE_LIMIT, iter_param_chunks

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


# ── iter_param_chunks unit ───────────────────────────────────────────────────


def test_iter_param_chunks_partitions_exactly_once() -> None:
    items = list(range(2050))
    chunks = list(iter_param_chunks(items, size=900))
    assert [len(c) for c in chunks] == [900, 900, 250]
    # Every element appears in exactly one chunk, order preserved.
    assert [x for c in chunks for x in c] == items


def test_iter_param_chunks_empty_yields_nothing() -> None:
    assert list(iter_param_chunks([])) == []


def test_iter_param_chunks_default_size_is_safe_limit() -> None:
    items = list(range(SQLITE_SAFE_VARIABLE_LIMIT + 1))
    chunks = list(iter_param_chunks(items))
    assert len(chunks) == 2
    assert len(chunks[0]) == SQLITE_SAFE_VARIABLE_LIMIT


def test_iter_param_chunks_rejects_bad_size() -> None:
    with pytest.raises(ValueError, match="chunk size"):
        list(iter_param_chunks([1, 2, 3], size=0))


# ── batch helpers survive an over-limit id set ───────────────────────────────


def test_batch_helpers_survive_40k_ids(db: sqlite3.Connection) -> None:
    """40,000 ids is well past SQLite's 32,766 variable ceiling. Each chunked
    helper must return cleanly (empty dicts for unknown ids) with no
    OperationalError — the crash this fixes."""
    ids = [f"id-{i}" for i in range(40_000)]

    # No matching rows exist, but the queries must still EXECUTE without
    # tripping the variable limit.
    assert latest_edge_confidence_batch(db, ids) == {}
    assert latest_edge_scores_batch(db, ids) == {}
    assert latest_edge_reviews_batch(db, ids) == {}
    assert read_entities_batch(db, ids) == {}
    # resolve_canonical_batch resolves unknown ids to themselves (no merges).
    resolved = resolve_canonical_batch(db, ids)
    assert len(resolved) == 40_000
    assert resolved[ids[0]] == ids[0]
    assert resolved[ids[-1]] == ids[-1]
