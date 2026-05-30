"""Unit tests for the binary-extractor module.

Tests don't hit live APIs — pypdf is exercised against a real (tiny)
PDF written inline, and the audio/image branches are tested with
mocked litellm so the dispatch + error-handling shape is covered
without burning quota.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from afair.agents.binary_extractors import (
    AudioTranscriptionError,
    ImageDescriptionError,
    PdfExtractionError,
    describe_image,
    extract_pdf_text,
    modality_for_mime,
    transcribe_audio,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── mime sniff ─────────────────────────────────────────────────────────────


def test_modality_for_mime_pdf() -> None:
    assert modality_for_mime("application/pdf") == "pdf"
    assert modality_for_mime("application/pdf; charset=utf-8") == "pdf"
    assert modality_for_mime("APPLICATION/PDF") == "pdf"


def test_modality_for_mime_image() -> None:
    assert modality_for_mime("image/png") == "image"
    assert modality_for_mime("image/jpeg") == "image"
    assert modality_for_mime("image/heic") == "image"


def test_modality_for_mime_audio() -> None:
    assert modality_for_mime("audio/mpeg") == "audio"
    assert modality_for_mime("audio/wav") == "audio"


def test_modality_for_mime_other_and_missing() -> None:
    assert modality_for_mime("text/plain") == "other"
    assert modality_for_mime(None) == "other"
    assert modality_for_mime("") == "other"
    assert modality_for_mime("application/zip") == "other"


# ── PDF ────────────────────────────────────────────────────────────────────


def _write_minimal_pdf(path: Path, text: str = "Hello afair PDF world") -> None:
    """Write a tiny but valid single-page PDF carrying ``text``.

    Avoids needing a real PDF fixture — pypdf is happy with this shape
    and the extracted text comes back as ``text``.
    """
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    # Inject a content stream that draws the text — Tf/Td/Tj are the
    # minimum PDF text operators.
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    stream = DecodedStreamObject()
    stream.set_data(content)
    writer.pages[0][NameObject("/Contents")] = stream
    # Register a font resource so /F1 is valid.
    from pypdf.generic import DictionaryObject

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    writer.pages[0][NameObject("/Resources")] = resources

    with path.open("wb") as fh:
        writer.write(fh)


def test_extract_pdf_text_reads_text_layer(tmp_path: Path) -> None:
    pdf_path = tmp_path / "test.pdf"
    _write_minimal_pdf(pdf_path, "afair vault hello")
    text = extract_pdf_text(pdf_path)
    assert "afair" in text.lower()


def test_extract_pdf_text_returns_empty_when_no_text(tmp_path: Path) -> None:
    """An entirely blank PDF (no text operators) yields empty string."""
    from pypdf import PdfWriter

    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as fh:
        writer.write(fh)
    assert extract_pdf_text(pdf_path) == ""


def test_extract_pdf_text_raises_on_corrupt_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "junk.pdf"
    corrupt.write_bytes(b"not a pdf at all, just nonsense")
    with pytest.raises(PdfExtractionError):
        extract_pdf_text(corrupt)


# ── audio ──────────────────────────────────────────────────────────────────


def test_transcribe_audio_uses_litellm(tmp_path: Path) -> None:
    audio_path = tmp_path / "fake.mp3"
    audio_path.write_bytes(b"fake-audio-bytes")

    class _FakeResponse:
        text = "this is the transcript"

    call_kwargs: dict[str, Any] = {}

    def fake_transcription(**kwargs: Any) -> _FakeResponse:
        call_kwargs.update(kwargs)
        return _FakeResponse()

    with patch("litellm.transcription", side_effect=fake_transcription):
        result = transcribe_audio(
            path=audio_path,
            model="openai/whisper-1",
            api_key="sk-test",
        )
    assert result == "this is the transcript"
    assert call_kwargs["model"] == "openai/whisper-1"
    assert call_kwargs["api_key"] == "sk-test"


def test_transcribe_audio_wraps_provider_failures(tmp_path: Path) -> None:
    audio_path = tmp_path / "x.wav"
    audio_path.write_bytes(b"x")

    def raise_(**_: Any) -> None:
        raise RuntimeError("upstream blew up")

    with (
        patch("litellm.transcription", side_effect=raise_),
        pytest.raises(AudioTranscriptionError, match="upstream blew up"),
    ):
        transcribe_audio(path=audio_path)


def test_transcribe_audio_rejects_response_without_text(tmp_path: Path) -> None:
    audio_path = tmp_path / "x.wav"
    audio_path.write_bytes(b"x")

    class _NoText:
        pass

    with (
        patch("litellm.transcription", return_value=_NoText()),
        pytest.raises(AudioTranscriptionError, match="missing 'text'"),
    ):
        transcribe_audio(path=audio_path)


# ── image ──────────────────────────────────────────────────────────────────


def _fake_completion_response(args_json: str, name: str = "record_extraction") -> Any:
    """Build a litellm-shaped completion response carrying tool args."""

    class _Func:
        def __init__(self, name: str, arguments: str) -> None:
            self.name = name
            self.arguments = arguments

    class _Call:
        def __init__(self, name: str, arguments: str) -> None:
            self.function = _Func(name, arguments)

    class _Message:
        def __init__(self, calls: list[_Call]) -> None:
            self.tool_calls = calls
            self.content = None

    class _Choice:
        def __init__(self, msg: _Message) -> None:
            self.message = msg

    class _Response:
        def __init__(self, choices: list[_Choice]) -> None:
            self.choices = choices

    return _Response([_Choice(_Message([_Call(name, args_json)]))])


def test_describe_image_returns_validated_extraction(tmp_path: Path) -> None:
    img_path = tmp_path / "shot.png"
    # Smallest valid 1x1 PNG payload
    img_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05"
        b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    extraction = {
        "best_guess_kind": "screenshot",
        "summary": "A blue button labelled Submit.",
    }
    args_json = '{"best_guess_kind":"screenshot","summary":"A blue button labelled Submit."}'

    with patch("litellm.completion", return_value=_fake_completion_response(args_json)):
        result = describe_image(
            path=img_path,
            mime="image/png",
            user_message="describe this",
            system_prompt="you are an extractor",
            tool_name="record_extraction",
            tool_description="record",
            tool_schema={"type": "object"},
            api_key="sk-test",
        )
    assert result == extraction


def test_describe_image_wraps_provider_failures(tmp_path: Path) -> None:
    img_path = tmp_path / "x.png"
    img_path.write_bytes(b"PNG")

    def raise_(**_: Any) -> None:
        raise ConnectionError("vision down")

    with (
        patch("litellm.completion", side_effect=raise_),
        pytest.raises(ImageDescriptionError, match="vision down"),
    ):
        describe_image(
            path=img_path,
            mime="image/png",
            user_message="x",
            system_prompt="x",
            tool_name="record_extraction",
            tool_description="x",
            tool_schema={"type": "object"},
        )


def test_describe_image_rejects_non_object_tool_arguments(tmp_path: Path) -> None:
    img_path = tmp_path / "x.png"
    img_path.write_bytes(b"PNG")

    with (
        patch(
            "litellm.completion",
            return_value=_fake_completion_response('"a string, not an object"'),
        ),
        pytest.raises(ImageDescriptionError, match="must be a JSON object"),
    ):
        describe_image(
            path=img_path,
            mime="image/png",
            user_message="x",
            system_prompt="x",
            tool_name="record_extraction",
            tool_description="x",
            tool_schema={"type": "object"},
        )
