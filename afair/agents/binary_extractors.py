"""Binary content extractors — turn PDF / audio / image bytes into text or
embeddings the rest of the pipeline can use.

Architecture
============

The substrate happily stores any binary blob (any mime, up to the size
cap) but the warm-path Extractor only understands text. Pre-multi-modal,
a stored PDF or screenshot was searchable by filename only — the actual
content was inert to recall.

This module bridges that gap. Each extractor is a small focused function:

  * ``extract_pdf_text(path)``  — pure-Python via pypdf, no system deps,
                                   no network call. Works for any PDF with
                                   a text layer.
  * ``transcribe_audio(...)``    — sends bytes to a Whisper-class model
                                   via litellm. Vendor-neutral via the
                                   model string (``openai/whisper-1``,
                                   ``deepgram/nova-3``, etc.).
  * ``describe_image(...)``      — sends the image as a multimodal
                                   message to a vision-capable LLM via
                                   litellm and returns the structured
                                   extraction. Same tool-use schema as
                                   the text extractor so downstream code
                                   doesn't branch on modality.

Each function isolates ITS provider call and ITS error class. The
warm-path Extractor decides which one to dispatch by inspecting the
event's mime + content_type — see ``extractor._run_extraction``.

All three are synchronous. They run inside the Extractor's
``ThreadPoolExecutor`` worker; concurrency is handled at the pool level.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import structlog

from .llm import LLMResponseError, _classify, _extract_tool_arguments

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)


# ── PDF ────────────────────────────────────────────────────────────────────


class PdfExtractionError(Exception):
    """PDF text extraction failed (corrupt file, encrypted, library bug)."""


# Per-PDF cap for the text we hand to the LLM. PDFs can be hundreds of
# pages; the LLM context cap + prompt truncation logic handles excess,
# but stopping the extraction itself early avoids loading megabytes of
# text into RAM only to truncate it later. 200 KB ≈ 50 pages of dense
# prose — far more than the text extractor will keep anyway.
_PDF_TEXT_HARD_CAP_BYTES = 200_000


def extract_pdf_text(path: Path) -> str:
    """Extract concatenated page text from a PDF on disk.

    Returns the full text up to ``_PDF_TEXT_HARD_CAP_BYTES``. Empty string
    when the PDF has no text layer (image-only / scanned PDF) — caller
    decides whether to fall back to image-vision per-page.

    Wraps pypdf's exception surface in :class:`PdfExtractionError` so the
    Extractor can record a single typed failure regardless of which
    internal exception fired.
    """
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as e:  # pragma: no cover -- dep is required
        msg = "pypdf is required for PDF extraction"
        raise PdfExtractionError(msg) from e

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # Try empty password (some PDFs are flagged encrypted but
            # accept ""). If that fails, surface as a typed error.
            try:
                reader.decrypt("")
            except Exception as e:  # pragma: no cover -- rare
                msg = f"PDF is password-protected: {e}"
                raise PdfExtractionError(msg) from e

        pieces: list[str] = []
        running_size = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception as e:  # pypdf is occasionally wobbly on a single page
                log.warning("pdf.page_extract_failed", error=str(e))
                continue
            if not text:
                continue
            pieces.append(text)
            running_size += len(text)
            if running_size >= _PDF_TEXT_HARD_CAP_BYTES:
                pieces.append("\n[TRUNCATED — PDF exceeded extractor cap]")
                break
        return "\n\n".join(pieces).strip()
    except PdfReadError as e:
        msg = f"PDF could not be read: {e}"
        raise PdfExtractionError(msg) from e


# ── audio (whisper-class) ──────────────────────────────────────────────────


class AudioTranscriptionError(Exception):
    """Whisper transcription failed (network, auth, format)."""


# litellm exposes whisper via ``litellm.transcription``. The model string
# format is the same as completion (``openai/whisper-1``, etc.). Whisper-1
# accepts files up to 25 MB; we let the caller cap upstream (the substrate
# already enforces a 10 MB cap on remember payloads).
DEFAULT_TRANSCRIPTION_MODEL = "openai/whisper-1"


def transcribe_audio(
    *,
    path: Path,
    model: str = DEFAULT_TRANSCRIPTION_MODEL,
    api_key: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Send an audio file to a Whisper-class model and return the transcript.

    Vendor-neutral via the model string. Failures raise
    :class:`AudioTranscriptionError` with the original cause so the
    Extractor can record a typed failed_interpretation row and move on.
    """
    import litellm

    try:
        with path.open("rb") as fh:
            kwargs: dict[str, Any] = {
                "model": model,
                "file": fh,
                "timeout": timeout,
            }
            if api_key is not None:
                kwargs["api_key"] = api_key
            response = litellm.transcription(**kwargs)
    except Exception as e:
        raise AudioTranscriptionError(str(e)) from e

    # litellm returns an object with a `.text` attribute or a dict with
    # "text". Defensive extraction matches the pattern in llm.py.
    text = getattr(response, "text", None)
    if text is None and hasattr(response, "get"):
        text = response.get("text")
    if not isinstance(text, str):
        msg = f"transcription response missing 'text': got {type(text).__name__}"
        raise AudioTranscriptionError(msg)
    return text.strip()


# ── image (vision-capable LLM) ─────────────────────────────────────────────


class ImageDescriptionError(Exception):
    """Image vision-call failed (network, auth, model can't do vision)."""


# Default vision model — Claude Haiku 4.5 has vision and is the cheapest
# vision-capable Anthropic model. The "claude-3-5-sonnet" tier handles
# more complex images but at ~5x cost. Phase 0 keeps Haiku for cost
# discipline; users override via VISION_MODEL env.
DEFAULT_VISION_MODEL = "anthropic/claude-haiku-4-5"


def describe_image(
    *,
    path: Path,
    mime: str,
    user_message: str,
    system_prompt: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    model: str = DEFAULT_VISION_MODEL,
    api_key: str | None = None,
    timeout: float = 60.0,
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """Send an image to a vision-capable LLM in tool-use mode.

    Returns the (parsed) tool arguments — same shape as
    :func:`afair.agents.llm.call_tool` so the Extractor's downstream
    code path doesn't branch on modality.

    The image is sent as a base64 data-URI inside an ``image_url`` content
    part — litellm normalizes this across providers (Anthropic, OpenAI,
    Gemini). Bytes are read from disk fresh each call (no caching — the
    blob is content-addressed, two calls for the same image are a sign
    of a higher-level bug we don't want to mask).
    """
    import litellm

    raw = path.read_bytes()
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_description,
                "parameters": tool_schema,
            },
        }
    ]
    tool_choice = {"type": "function", "function": {"name": tool_name}}

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if api_key is not None:
        kwargs["api_key"] = api_key

    try:
        response = litellm.completion(**kwargs)
    except Exception as e:
        # Reuse llm.py's classifier so the Extractor sees the same error
        # taxonomy regardless of modality.
        classified = _classify(e)
        raise ImageDescriptionError(str(classified)) from classified

    raw_args = _extract_tool_arguments(response, expected_name=tool_name)
    try:
        import json

        parsed = json.loads(raw_args)
    except (LLMResponseError, ValueError) as e:
        raise ImageDescriptionError(f"vision tool arguments not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ImageDescriptionError(
            f"vision tool arguments must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


# ── mime sniffing helper ───────────────────────────────────────────────────


def modality_for_mime(mime: str | None) -> str:
    """Classify a payload's mime into ``pdf`` | ``image`` | ``audio`` | ``other``.

    Centralized so the Extractor dispatch + tests share one truth.
    Image/audio are coarse matches; PDFs only when ``application/pdf``.
    """
    if not mime:
        return "other"
    mime_lower = mime.lower().split(";", 1)[0].strip()
    if mime_lower == "application/pdf":
        return "pdf"
    if mime_lower.startswith("image/"):
        return "image"
    if mime_lower.startswith("audio/"):
        return "audio"
    return "other"
