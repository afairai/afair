"""Embedding generation — vendor-neutral via litellm, with chunking for long inputs.

Provider is selected entirely by the ``model`` string per I5. Default
shipped in Phase 1 is ``openai/text-embedding-3-small`` (cheap, fast,
1536-dim), but the same code path works with Voyage AI, Cohere, Gemini,
or local Ollama models via litellm's standard format.

Embeddings are generated synchronously inside the warm-path Extractor's
background thread (after the LLM extraction call). On error the
substrate event is still durable; only the vector store doesn't get
populated for that event — recall gracefully falls back to FTS for it.

Long-input handling: text-embedding-3-small caps at 8192 tokens (~32K
chars of English prose). For inputs above the safety threshold, the
text is split into overlapping chunks, each chunk is embedded in a
single batched API call, and the resulting vectors are mean-pooled into
one document vector. The pooled vector is L2-normalized so cosine
similarity remains the right ranking function. Single-vector output
keeps the events_vec schema unchanged (one row per content_hash).
"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class EmbeddingError(Exception):
    """Any failure during embedding generation."""


# ── query-side embedding cache ────────────────────────────────────────────
# Substrate write embeddings (each event is unique) intentionally bypass
# this cache. The read-side recall path embeds the SAME query string over
# and over — caching there turns a 300ms OpenAI roundtrip into a dict
# lookup. Bounded LRU + per-entry TTL prevents unbounded memory growth
# AND prevents storing query embeddings forever even when the cache has
# spare capacity (LRU alone doesn't expire never-revisited entries).
_QUERY_CACHE_MAXSIZE = 256
_QUERY_CACHE_TTL_SECONDS = 3600  # 1 hour


class _QueryEmbeddingCache:
    """Thread-safe bounded LRU with per-entry TTL.

    Two-tier eviction:
      - LRU bound (size cap) — drops oldest-used when full.
      - TTL bound (age cap) — entries older than ``ttl_seconds`` are
        treated as misses on read. Prevents stale embeddings sitting
        in memory forever for queries that fire once and never repeat.

    Lock held only for cache mutation; the network call (on miss)
    runs unlocked so a slow OpenAI roundtrip doesn't block cache
    hits from concurrent recall calls.
    """

    def __init__(
        self,
        maxsize: int = _QUERY_CACHE_MAXSIZE,
        *,
        ttl_seconds: int = _QUERY_CACHE_TTL_SECONDS,
    ) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        # Value tuple: (vector, inserted_at_monotonic)
        self._cache: OrderedDict[tuple[str, str], tuple[list[float], float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.expired = 0

    def get_or_compute(self, *, model: str, text: str, api_key: str | None) -> list[float]:
        key = (model, text)
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                vec, ts = entry
                if now - ts <= self._ttl:
                    self._cache.move_to_end(key)
                    self.hits += 1
                    return list(vec)
                # Expired — treat as miss and drop the stale entry.
                del self._cache[key]
                self.expired += 1
        # Miss — compute outside the lock so other threads can keep hitting
        # the cache while we wait on the network.
        vec = embed_text(model=model, text=text, api_key=api_key)
        with self._lock:
            self._cache[key] = (list(vec), time.monotonic())
            self._cache.move_to_end(key)
            self.misses += 1
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)
        return vec

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self.hits = 0
            self.misses = 0
            self.expired = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "expired": self.expired,
                "size": len(self._cache),
                "maxsize": self._maxsize,
                "ttl_seconds": self._ttl,
            }


_query_cache = _QueryEmbeddingCache()


def embed_query(*, model: str, text: str, api_key: str | None = None) -> list[float]:
    """Cached variant of ``embed_text`` for read-side query strings.

    Same signature as ``embed_text`` — caller can swap them without other
    changes. Cache is process-local; survives across requests, dies with
    the server. Bounded to 256 distinct (model, text) pairs.
    """
    return _query_cache.get_or_compute(model=model, text=text, api_key=api_key)


def query_cache_stats() -> dict[str, int]:
    """Inspect the recall-query cache. Useful for /health or admin diagnostics."""
    return _query_cache.stats()


def reset_query_cache() -> None:
    """Empty the query cache. Used by tests; rarely called in production."""
    _query_cache.clear()


# ── chunking parameters ────────────────────────────────────────────────
# text-embedding-3-small hard cap is 8192 tokens. We use 3.5 chars/token
# as a conservative estimate (English prose averages ~4; we round down to
# leave margin for technical content and non-Latin scripts). Chunk size
# 24,000 chars ≈ 6,800 tokens, well below the cap. Overlap of 2,000 chars
# preserves cross-chunk context so paragraphs straddling a boundary are
# represented in both vectors.
_CHARS_PER_TOKEN = 3.5
_MODEL_TOKEN_CAP = 8192
_SAFETY_MARGIN = 0.85
_CHUNK_TOKEN_BUDGET = int(_MODEL_TOKEN_CAP * _SAFETY_MARGIN)  # ≈ 6963
_CHUNK_CHAR_BUDGET = int(_CHUNK_TOKEN_BUDGET * _CHARS_PER_TOKEN)  # ≈ 24370
_CHUNK_OVERLAP_CHARS = 2000


def _estimate_tokens(text: str) -> int:
    """Rough token estimate without depending on a vendor-specific tokenizer.

    Char-based estimation keeps us I5-neutral (no tiktoken, no Anthropic
    tokenizer pulled in). 3.5 chars/token is conservative — actual ratios
    range from ~3 (code, German compounds) to ~5 (simple English). The
    conservative value over-estimates input length, which is the safe
    direction for staying under the API cap.
    """
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def _chunk_text(
    text: str,
    *,
    chunk_chars: int = _CHUNK_CHAR_BUDGET,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split ``text`` into overlapping chunks at paragraph boundaries where possible.

    Algorithm:
      1. If ``text`` already fits in ``chunk_chars``, return ``[text]``.
      2. Otherwise, walk forward in steps of ``(chunk_chars - overlap_chars)``.
         At each step, take a window of up to ``chunk_chars`` and try to end
         it at the nearest paragraph break (``\\n\\n``) before the hard limit;
         fall back to sentence end, then to a word boundary, then to a hard
         char cut as last resort.

    Overlap exists so that a fact spanning a chunk boundary appears in both
    chunks' embeddings — improves retrieval for queries about content
    straddling sections.
    """
    if overlap_chars >= chunk_chars:
        msg = f"overlap_chars ({overlap_chars}) must be < chunk_chars ({chunk_chars})"
        raise ValueError(msg)
    if not text:
        return [""]
    if len(text) <= chunk_chars:
        return [text]

    step = chunk_chars - overlap_chars
    chunks: list[str] = []
    pos = 0
    n = len(text)

    while pos < n:
        end = min(pos + chunk_chars, n)
        if end == n:
            chunks.append(text[pos:end])
            break

        # Try to end at the nearest natural boundary BEFORE the hard limit,
        # within the last 25% of the window. If nothing found, hard-cut.
        soft_zone_start = pos + int(chunk_chars * 0.75)
        boundary = _find_boundary(text, soft_zone_start, end)
        if boundary > pos:
            end = boundary
        chunks.append(text[pos:end])

        # Advance by step, but never less than chunk_chars - overlap_chars
        # relative to the chosen end, to guarantee forward progress.
        pos = max(pos + step, end - overlap_chars)

    return chunks


def _find_boundary(text: str, start: int, end: int) -> int:
    """Return the position of the best natural break in text[start:end].

    Preference order: paragraph break (``\\n\\n``), sentence end
    (``. `` / ``! `` / ``? `` / ``. \\n``), then whitespace. Returns the
    position immediately AFTER the chosen delimiter (so the next chunk
    starts cleanly). Falls back to ``end`` if no boundary is found.
    """
    window = text[start:end]
    # paragraph break — preferred
    idx = window.rfind("\n\n")
    if idx >= 0:
        return start + idx + 2
    # sentence end
    for sep in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = window.rfind(sep)
        if idx >= 0:
            return start + idx + len(sep)
    # word boundary
    idx = window.rfind(" ")
    if idx >= 0:
        return start + idx + 1
    # no boundary in the soft zone — hard cut
    return end


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of a list of equal-length vectors, then L2-normalize.

    Normalization preserves cosine semantics: the mean of L2-normalized
    embeddings is no longer unit-length, so we renormalize once at the
    end. This gives a single document vector that represents the
    document's centroid in embedding space.
    """
    if not vectors:
        msg = "cannot mean-pool empty vector list"
        raise EmbeddingError(msg)
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        msg = "vectors have inconsistent dimensions"
        raise EmbeddingError(msg)

    pooled = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            pooled[i] += x
    n = float(len(vectors))
    pooled = [x / n for x in pooled]

    # L2-normalize so cosine similarity stays the right metric.
    norm = math.sqrt(sum(x * x for x in pooled))
    if norm <= 0.0:
        # Degenerate input — return the zero vector rather than dividing by zero.
        return pooled
    return [x / norm for x in pooled]


def embed_text(
    *,
    model: str,
    text: str,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> list[float]:
    """Generate an embedding vector for ``text``.

    Returns a Python list of floats. Length equals the model's dimension
    (the caller passes ``embedding_dim`` from Settings to allocate the
    sqlite-vec storage; this function trusts the model returns the right
    size).

    For inputs that would exceed the model's token cap, the text is
    automatically split into overlapping chunks; chunks are embedded in
    a single batched API call; vectors are mean-pooled and L2-normalized
    to produce one document vector. Caller doesn't need to know.

    Provider dispatch:
      - ``fastembed/<model>`` → local ONNX inference, no network. 5-30ms
        latency, $0 ongoing cost. First call downloads the model and
        caches in ~/.cache/fastembed.
      - Anything else → litellm's vendor-agnostic API surface
        (openai/, voyage/, gemini/, cohere/, anthropic/, ollama/, …).
    """
    chunks = _chunk_text(text or "(empty)")

    if model.startswith("fastembed/"):
        vectors = _embed_via_fastembed(model_name=model.removeprefix("fastembed/"), texts=chunks)
    else:
        vectors = _embed_via_litellm(model=model, texts=chunks, api_key=api_key, timeout=timeout)

    if len(vectors) != len(chunks):
        msg = f"embedding response returned {len(vectors)} vectors for {len(chunks)} chunks"
        raise EmbeddingError(msg)

    if len(vectors) == 1:
        return vectors[0]
    return _mean_pool(vectors)


def _embed_via_litellm(
    *, model: str, texts: list[str], api_key: str | None, timeout: float
) -> list[list[float]]:
    """Call litellm.embedding for vendor-neutral cloud providers."""
    # Lazy import — litellm is heavy at import time.
    import litellm

    try:
        if api_key is not None:
            response = litellm.embedding(model=model, input=texts, timeout=timeout, api_key=api_key)
        else:
            response = litellm.embedding(model=model, input=texts, timeout=timeout)
    except Exception as e:
        msg = f"embedding call failed: {e}"
        raise EmbeddingError(msg) from e

    return _vectors_from_response(response)


# ── fastembed (local ONNX) — lazy-loaded singleton per model name ─────────
# fastembed.TextEmbedding constructs an ONNX session that takes ~1-3 seconds
# the first time (download + load), so we keep it process-resident. Threadsafe
# via the lock; multiple recall threads share one model instance.
_fastembed_lock = threading.Lock()
_fastembed_models: dict[str, object] = {}


def _embed_via_fastembed(*, model_name: str, texts: list[str]) -> list[list[float]]:
    """Run ONNX embedding inference in-process. No network."""
    try:
        from fastembed import TextEmbedding
    except ImportError as e:
        msg = "fastembed not installed; pip install fastembed or remove fastembed/ prefix"
        raise EmbeddingError(msg) from e

    with _fastembed_lock:
        emb_model = _fastembed_models.get(model_name)
        if emb_model is None:
            try:
                emb_model = TextEmbedding(model_name=model_name)
            except Exception as e:
                msg = f"fastembed model {model_name!r} failed to load: {e}"
                raise EmbeddingError(msg) from e
            _fastembed_models[model_name] = emb_model

    try:
        raw = list(emb_model.embed(texts))  # type: ignore[attr-defined]
    except Exception as e:
        msg = f"fastembed inference failed: {e}"
        raise EmbeddingError(msg) from e

    # fastembed returns numpy arrays — convert to plain list[float] so the
    # rest of the code (mean-pool, serialize_vector) sees a uniform type.
    return [[float(x) for x in vec] for vec in raw]


def _vectors_from_response(response: object) -> list[list[float]]:
    """Pull the list of embedding vectors out of a litellm embedding response."""
    try:
        data = response.data  # type: ignore[attr-defined]
        if not data:
            msg = "embedding response had no data"
            raise EmbeddingError(msg)
    except AttributeError as e:
        msg = f"malformed embedding response object: {e}"
        raise EmbeddingError(msg) from e

    vectors: list[list[float]] = []
    for item in data:
        try:
            vec = item.get("embedding") if hasattr(item, "get") else item["embedding"]
        except (KeyError, TypeError) as e:
            msg = f"malformed embedding response item: {e}"
            raise EmbeddingError(msg) from e
        if not isinstance(vec, list) or not all(isinstance(v, (int, float)) for v in vec):
            msg = "embedding response had non-numeric vector"
            raise EmbeddingError(msg)
        vectors.append([float(v) for v in vec])
    return vectors


def serialize_vector(vec: Sequence[float]) -> bytes:
    """Pack a vector into sqlite-vec's expected wire format.

    sqlite-vec accepts vectors as raw little-endian float32 bytes when
    passed through a parameterized query. Using ``struct.pack`` is faster
    than going through numpy and avoids a numpy dependency.
    """
    return struct.pack(f"<{len(vec)}f", *vec)
