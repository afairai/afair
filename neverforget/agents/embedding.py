"""Embedding generation — vendor-neutral via litellm.

Provider is selected entirely by the ``model`` string per I5. Default
shipped in Phase 1 is ``openai/text-embedding-3-small`` (cheap, fast,
1536-dim), but the same code path works with Voyage AI, Cohere, Gemini,
or local Ollama models via litellm's standard format.

Embeddings are generated synchronously inside the warm-path Extractor's
background thread (after the LLM extraction call). On error the
substrate event is still durable; only the vector store doesn't get
populated for that event — recall gracefully falls back to FTS for it.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class EmbeddingError(Exception):
    """Any failure during embedding generation."""


def embed_text(
    *,
    model: str,
    text: str,
    api_key: str | None = None,
    timeout: float = 15.0,
) -> list[float]:
    """Generate an embedding vector for ``text``.

    Returns a Python list of floats. Length equals the model's dimension
    (the caller passes ``embedding_dim`` from Settings to allocate the
    sqlite-vec storage; this function trusts the model returns the right
    size).
    """
    # Lazy import — litellm is heavy at import time.
    import litellm

    try:
        if api_key is not None:
            response = litellm.embedding(
                model=model, input=[text], timeout=timeout, api_key=api_key
            )
        else:
            response = litellm.embedding(model=model, input=[text], timeout=timeout)
    except Exception as e:
        msg = f"embedding call failed: {e}"
        raise EmbeddingError(msg) from e

    try:
        data = response.data
        if not data:
            msg = "embedding response had no data"
            raise EmbeddingError(msg)
        first = data[0]
        # litellm returns either dict-like or object-like; cover both
        vec = first.get("embedding") if hasattr(first, "get") else first["embedding"]
    except (AttributeError, KeyError, IndexError, TypeError) as e:
        msg = f"malformed embedding response: {e}"
        raise EmbeddingError(msg) from e

    if not isinstance(vec, list) or not all(isinstance(v, (int, float)) for v in vec):
        msg = "embedding response had non-numeric vector"
        raise EmbeddingError(msg)
    return [float(v) for v in vec]


def serialize_vector(vec: Sequence[float]) -> bytes:
    """Pack a vector into sqlite-vec's expected wire format.

    sqlite-vec accepts vectors as raw little-endian float32 bytes when
    passed through a parameterized query. Using ``struct.pack`` is faster
    than going through numpy and avoids a numpy dependency.
    """
    return struct.pack(f"<{len(vec)}f", *vec)
