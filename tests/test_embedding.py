"""Embedding-layer tests — chunking, mean-pool, truncation. No live API calls."""

from __future__ import annotations

import math
from typing import Any

import pytest

from neverforget.agents import embedding

# ── chunking ───────────────────────────────────────────────────────────────


def test_short_text_returns_single_chunk() -> None:
    assert embedding._chunk_text("hello world") == ["hello world"]


def test_empty_text_returns_single_empty_chunk() -> None:
    assert embedding._chunk_text("") == [""]


def test_long_text_splits_into_overlapping_chunks() -> None:
    text = "x" * (embedding._CHUNK_CHAR_BUDGET * 3)
    chunks = embedding._chunk_text(text)
    assert len(chunks) >= 3
    # No chunk exceeds the budget.
    for c in chunks:
        assert len(c) <= embedding._CHUNK_CHAR_BUDGET
    # Concatenated chunks (minus overlap) cover everything — sanity check
    # we don't drop content.
    joined = "".join(chunks)
    assert len(joined) >= len(text)


def test_chunks_prefer_paragraph_boundaries() -> None:
    """When a paragraph break sits inside the soft zone, the chunk ends there."""
    head = "a" * 20_000
    body_para = "\n\n" + ("b" * 5_000)
    tail = "\n\n" + ("c" * 5_000)
    text = head + body_para + tail

    chunks = embedding._chunk_text(text, chunk_chars=22_000, overlap_chars=1_000)
    # First chunk ends right after the FIRST paragraph break (delimiter
    # included in the closing chunk so the boundary character pair isn't
    # orphaned). Verify the chunk contains all the 'a's and ends with the
    # paragraph delimiter that follows them.
    assert "a" * 20_000 in chunks[0]
    assert chunks[0].endswith("\n\n")
    # Second chunk starts cleanly with the next paragraph's content.
    assert chunks[1].startswith("b") or chunks[1].startswith("a")


def test_chunks_overlap_so_no_content_falls_through() -> None:
    """A unique marker straddling chunk boundary appears in two chunks."""
    chunk_chars = 1_000
    overlap_chars = 200
    head = "h" * 850
    marker = "<MARKER>"
    tail = "t" * 1_000
    text = head + marker + tail

    chunks = embedding._chunk_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    # Marker is in head zone but near boundary — should appear in chunk 0,
    # and overlap pulls it (or part of it) into chunk 1.
    assert marker in chunks[0]
    # Some content from chunk 0's tail must be re-present in chunk 1's head.
    assert chunks[0][-100:] in (chunks[1] if len(chunks) > 1 else "")


def test_chunks_make_forward_progress_on_worst_case() -> None:
    """Pathological no-boundary text still terminates."""
    text = "x" * (embedding._CHUNK_CHAR_BUDGET * 5)
    chunks = embedding._chunk_text(text)
    assert chunks  # didn't return empty
    # And the final character of text is in the LAST chunk
    assert chunks[-1].endswith("x")


def test_overlap_must_be_less_than_chunk_size() -> None:
    with pytest.raises(ValueError):
        embedding._chunk_text("anything", chunk_chars=100, overlap_chars=100)


# ── mean-pool ──────────────────────────────────────────────────────────────


def test_mean_pool_of_single_vector_is_normalized_original() -> None:
    v = [3.0, 0.0, 4.0]  # length 5 → normalized to (0.6, 0, 0.8)
    pooled = embedding._mean_pool([v])
    norm = math.sqrt(sum(x * x for x in pooled))
    assert math.isclose(norm, 1.0, abs_tol=1e-6)
    assert math.isclose(pooled[0], 0.6, abs_tol=1e-6)
    assert math.isclose(pooled[2], 0.8, abs_tol=1e-6)


def test_mean_pool_averages_then_normalizes() -> None:
    pooled = embedding._mean_pool([[1.0, 0.0], [0.0, 1.0]])
    # Mean is (0.5, 0.5); after L2 normalization → (1/√2, 1/√2)
    expected = 1.0 / math.sqrt(2)
    assert math.isclose(pooled[0], expected, abs_tol=1e-6)
    assert math.isclose(pooled[1], expected, abs_tol=1e-6)


def test_mean_pool_rejects_inconsistent_dimensions() -> None:
    with pytest.raises(embedding.EmbeddingError, match="inconsistent"):
        embedding._mean_pool([[1.0, 0.0], [1.0, 0.0, 0.0]])


def test_mean_pool_rejects_empty_list() -> None:
    with pytest.raises(embedding.EmbeddingError, match="empty"):
        embedding._mean_pool([])


def test_mean_pool_handles_zero_vector_gracefully() -> None:
    """Two opposing unit vectors cancel out; we must not divide by zero."""
    pooled = embedding._mean_pool([[1.0, 0.0], [-1.0, 0.0]])
    assert pooled == [0.0, 0.0]


# ── end-to-end embed_text with mocked litellm ──────────────────────────────


class _FakeEmbedding:
    """Minimal litellm.embedding stand-in for tests."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [{"embedding": v} for v in vectors]


def test_embed_text_short_input_is_single_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_inputs: list[list[str]] = []

    def fake_embedding(**kwargs: Any) -> _FakeEmbedding:
        captured_inputs.append(kwargs["input"])
        return _FakeEmbedding([[0.5, 0.5]])

    import litellm

    monkeypatch.setattr(litellm, "embedding", fake_embedding)

    result = embedding.embed_text(model="openai/text-embedding-3-small", text="short doc")
    assert len(captured_inputs) == 1
    assert captured_inputs[0] == ["short doc"]
    assert result == [0.5, 0.5]


def test_embed_text_long_input_is_chunked_and_pooled(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_inputs: list[list[str]] = []

    def fake_embedding(**kwargs: Any) -> _FakeEmbedding:
        captured_inputs.append(kwargs["input"])
        # Return one vector per chunk, all the same so the mean-pool is
        # deterministic and L2-normalized to the same direction.
        return _FakeEmbedding([[1.0, 0.0] for _ in kwargs["input"]])

    import litellm

    monkeypatch.setattr(litellm, "embedding", fake_embedding)

    text = "paragraph.\n\n" * 5_000  # ~65 KB → well over the chunk budget
    result = embedding.embed_text(model="openai/text-embedding-3-small", text=text)

    # One API call, list of chunks (>=2).
    assert len(captured_inputs) == 1
    assert len(captured_inputs[0]) >= 2
    # Pooled result is L2-normalized; identical inputs → (1.0, 0.0).
    assert math.isclose(result[0], 1.0, abs_tol=1e-6)
    assert math.isclose(result[1], 0.0, abs_tol=1e-6)


def test_embed_text_wraps_litellm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_embedding(**_: Any) -> _FakeEmbedding:
        raise RuntimeError("upstream is down")

    import litellm

    monkeypatch.setattr(litellm, "embedding", fake_embedding)

    with pytest.raises(embedding.EmbeddingError, match="embedding call failed"):
        embedding.embed_text(model="openai/text-embedding-3-small", text="x")


def test_embed_text_rejects_response_with_mismatched_vector_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_embedding(**kwargs: Any) -> _FakeEmbedding:
        # Ask for N chunks but return only one vector.
        return _FakeEmbedding([[1.0, 0.0]])

    import litellm

    monkeypatch.setattr(litellm, "embedding", fake_embedding)

    text = "p\n\n" * 20_000
    with pytest.raises(embedding.EmbeddingError, match="vectors for"):
        embedding.embed_text(model="openai/text-embedding-3-small", text=text)


# ── token estimation ──────────────────────────────────────────────────────


def test_token_estimate_is_conservative() -> None:
    """3.5 chars/token; we round up, so 'aaaa' (4 chars) should be ≥ 2."""
    assert embedding._estimate_tokens("aaaa") >= 2
    assert embedding._estimate_tokens("") == 0
