from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import edge_tts
import pytest
from edge_tts.exceptions import NoAudioReceived

from short2short.core.interfaces import TTS
from short2short.core.voice import (
    EdgeTTS,
    cues_from_chunks,
    default_voice_for_lang,
)
from short2short.errors import Short2ShortError


def test_default_voice_for_lang() -> None:
    # Vietnamese prefixes
    assert default_voice_for_lang("vi") == "vi-VN-HoaiMyNeural"
    assert default_voice_for_lang("vi-VN") == "vi-VN-HoaiMyNeural"
    assert default_voice_for_lang("VI") == "vi-VN-HoaiMyNeural"
    assert default_voice_for_lang("vie") == "vi-VN-HoaiMyNeural"
    assert default_voice_for_lang("  vi-vn  ") == "vi-VN-HoaiMyNeural"

    # English prefixes
    assert default_voice_for_lang("en") == "en-US-AnaNeural"
    assert default_voice_for_lang("en-US") == "en-US-AnaNeural"
    assert default_voice_for_lang("EN") == "en-US-AnaNeural"
    assert default_voice_for_lang("english") == "en-US-AnaNeural"

    # Other languages with custom fallback
    assert default_voice_for_lang("fr", fallback="fr-FR-DeniseNeural") == "fr-FR-DeniseNeural"
    assert default_voice_for_lang("ja", fallback="ja-JP-NanamiNeural") == "ja-JP-NanamiNeural"
    assert default_voice_for_lang("", fallback="custom-voice") == "custom-voice"

    # Default fallback when omitted
    assert default_voice_for_lang("de") == "vi-VN-HoaiMyNeural"


def test_cues_from_chunks_word_boundary() -> None:
    chunks: list[dict[str, Any]] = [
        {"type": "audio", "data": b"audio-part-1"},
        {"type": "WordBoundary", "offset": 0, "duration": 5_000_000, "text": "Hello"},
        {"type": "WordBoundary", "offset": 6_000_000, "duration": 4_000_000, "text": "world"},
        {"type": "audio", "data": b"audio-part-2"},
    ]
    cues = cues_from_chunks(chunks)
    assert len(cues) == 2
    assert cues[0].text == "Hello"
    assert cues[0].start == pytest.approx(0.0)
    assert cues[0].end == pytest.approx(0.5)

    assert cues[1].text == "world"
    assert cues[1].start == pytest.approx(0.6)
    assert cues[1].end == pytest.approx(1.0)


def test_cues_from_chunks_prefers_word_boundary_over_sentence_boundary() -> None:
    chunks: list[dict[str, Any]] = [
        {"type": "SentenceBoundary", "offset": 0, "duration": 20_000_000, "text": "Hello world."},
        {"type": "WordBoundary", "offset": 0, "duration": 10_000_000, "text": "Hello"},
        {"type": "WordBoundary", "offset": 10_000_000, "duration": 10_000_000, "text": "world"},
    ]
    cues = cues_from_chunks(chunks)
    assert len(cues) == 2
    assert [c.text for c in cues] == ["Hello", "world"]


def test_cues_from_chunks_fallback_to_sentence_boundary() -> None:
    chunks: list[dict[str, Any]] = [
        {"type": "audio", "data": b"audio"},
        {"type": "SentenceBoundary", "offset": 0, "duration": 25_000_000, "text": "Sentence one."},
        {
            "type": "SentenceBoundary",
            "offset": 26_000_000,
            "duration": 20_000_000,
            "text": "Sentence two.",
        },
    ]
    cues = cues_from_chunks(chunks)
    assert len(cues) == 2
    assert cues[0].text == "Sentence one."
    assert cues[0].start == pytest.approx(0.0)
    assert cues[0].end == pytest.approx(2.5)

    assert cues[1].text == "Sentence two."
    assert cues[1].start == pytest.approx(2.6)
    assert cues[1].end == pytest.approx(4.6)


def test_cues_from_chunks_empty_and_no_cues() -> None:
    assert cues_from_chunks([]) == []
    assert cues_from_chunks([{"type": "audio", "data": b"..."}]) == []


def test_cues_from_chunks_missing_fields() -> None:
    chunks: list[dict[str, Any]] = [
        {"type": "WordBoundary", "offset": None, "duration": None, "text": None},
    ]
    cues = cues_from_chunks(chunks)
    assert len(cues) == 1
    assert cues[0].text == ""
    assert cues[0].start == pytest.approx(0.0)
    assert cues[0].end == pytest.approx(0.0)


class MockCommunicate:
    def __init__(
        self,
        text: str,
        voice: str = "default",
        *,
        rate: str = "+0%",
        boundary: str | None = None,
        chunks: list[dict[str, Any]] | None = None,
        raise_type_error_on_boundary: bool = False,
        stream_delay: float = 0.0,
    ) -> None:
        if raise_type_error_on_boundary and boundary is not None:
            raise TypeError("__init__() got an unexpected keyword argument 'boundary'")
        self.text = text
        self.voice = voice
        self.rate = rate
        self.boundary = boundary
        self.chunks = chunks if chunks is not None else []
        self.stream_delay = stream_delay

    async def stream(self) -> AsyncGenerator[dict[str, Any]]:
        if self.stream_delay > 0:
            await asyncio.sleep(self.stream_delay)
        for chunk in self.chunks:
            yield chunk


def test_edge_tts_synthesize_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded_calls: list[dict[str, Any]] = []

    def fake_communicate(
        text: str, voice: str, *, rate: str = "+0%", boundary: str | None = None
    ) -> MockCommunicate:
        recorded_calls.append({"text": text, "voice": voice, "rate": rate, "boundary": boundary})
        return MockCommunicate(
            text,
            voice,
            rate=rate,
            boundary=boundary,
            chunks=[
                {"type": "audio", "data": b"ID3-header-and-mp3-bytes"},
                {"type": "WordBoundary", "offset": 0, "duration": 10_000_000, "text": "Xin"},
                {
                    "type": "WordBoundary",
                    "offset": 10_000_000,
                    "duration": 15_000_000,
                    "text": "chao",
                },
            ],
        )

    monkeypatch.setattr(edge_tts, "Communicate", fake_communicate)

    tts: TTS = EdgeTTS(rate="+5%", timeout=30.0)
    dest = tmp_path / "nested" / "audio.mp3"
    result = tts.synthesize("Xin chao", dest, voice="vi-VN-HoaiMyNeural")

    assert result.path == dest
    assert dest.is_file()
    assert dest.read_bytes() == b"ID3-header-and-mp3-bytes"
    assert result.duration_seconds == pytest.approx(2.5)
    assert len(result.cues) == 2
    assert result.cues[0].text == "Xin"
    assert result.cues[1].text == "chao"
    assert result.cues[1].end == pytest.approx(2.5)

    assert len(recorded_calls) == 1
    assert recorded_calls[0]["boundary"] == "WordBoundary"
    assert recorded_calls[0]["rate"] == "+5%"
    assert recorded_calls[0]["voice"] == "vi-VN-HoaiMyNeural"


def test_edge_tts_synthesize_boundary_type_error_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded_boundaries: list[str | None] = []

    def fake_communicate(
        text: str, voice: str, *, rate: str = "+0%", boundary: str | None = None
    ) -> MockCommunicate:
        recorded_boundaries.append(boundary)
        return MockCommunicate(
            text,
            voice,
            rate=rate,
            boundary=boundary,
            raise_type_error_on_boundary=True,
            chunks=[
                {"type": "audio", "data": b"mp3-audio"},
                {"type": "SentenceBoundary", "offset": 0, "duration": 18_000_000, "text": "Retry."},
            ],
        )

    monkeypatch.setattr(edge_tts, "Communicate", fake_communicate)

    tts = EdgeTTS()
    dest = tmp_path / "audio.mp3"
    result = tts.synthesize("Retry.", dest, voice="en-US-AnaNeural")

    # First attempt passed boundary="WordBoundary", second attempt omitted boundary
    assert recorded_boundaries == ["WordBoundary", None]
    assert result.duration_seconds == pytest.approx(1.8)
    assert len(result.cues) == 1
    assert result.cues[0].text == "Retry."


def test_edge_tts_synthesize_sentence_boundary_duration_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_communicate(
        text: str, voice: str, *, rate: str = "+0%", boundary: str | None = None
    ) -> MockCommunicate:
        return MockCommunicate(
            text,
            voice,
            chunks=[
                {"type": "audio", "data": b"mp3-data"},
                {
                    "type": "SentenceBoundary",
                    "offset": 0,
                    "duration": 30_000_000,
                    "text": "Full sentence.",
                },
            ],
        )

    monkeypatch.setattr(edge_tts, "Communicate", fake_communicate)

    tts = EdgeTTS()
    dest = tmp_path / "audio.mp3"
    result = tts.synthesize("Full sentence.", dest, voice="vi-VN-HoaiMyNeural")

    assert result.duration_seconds == pytest.approx(3.0)
    assert len(result.cues) == 1
    assert result.cues[0].text == "Full sentence."


def test_edge_tts_synthesize_empty_audio_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_communicate(
        text: str, voice: str, *, rate: str = "+0%", boundary: str | None = None
    ) -> MockCommunicate:
        return MockCommunicate(
            text,
            voice,
            chunks=[
                {"type": "WordBoundary", "offset": 0, "duration": 10_000_000, "text": "Word"},
            ],
        )

    monkeypatch.setattr(edge_tts, "Communicate", fake_communicate)

    tts = EdgeTTS()
    dest = tmp_path / "audio.mp3"
    with pytest.raises(Short2ShortError, match="empty audio"):
        tts.synthesize("Word", dest, voice="vi-VN-HoaiMyNeural")


def test_edge_tts_synthesize_no_timing_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_communicate(
        text: str, voice: str, *, rate: str = "+0%", boundary: str | None = None
    ) -> MockCommunicate:
        return MockCommunicate(
            text,
            voice,
            chunks=[
                {"type": "audio", "data": b"audio-without-metadata"},
            ],
        )

    monkeypatch.setattr(edge_tts, "Communicate", fake_communicate)

    tts = EdgeTTS()
    dest = tmp_path / "audio.mp3"
    with pytest.raises(Short2ShortError, match="tts produced no timing"):
        tts.synthesize("Text", dest, voice="vi-VN-HoaiMyNeural")


def test_edge_tts_synthesize_edge_tts_exception_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FailingCommunicate:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def stream(self) -> AsyncGenerator[dict[str, Any]]:
            raise NoAudioReceived("No audio received from service")
            yield {}  # unreachable generator yield

    monkeypatch.setattr(edge_tts, "Communicate", FailingCommunicate)

    tts = EdgeTTS()
    dest = tmp_path / "audio.mp3"
    with pytest.raises(Short2ShortError, match="TTS synthesis failed"):
        tts.synthesize("Text", dest, voice="vi-VN-HoaiMyNeural")


def test_edge_tts_synthesize_timeout_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_communicate(
        text: str, voice: str, *, rate: str = "+0%", boundary: str | None = None
    ) -> MockCommunicate:
        return MockCommunicate(
            text,
            voice,
            stream_delay=1.0,
            chunks=[{"type": "audio", "data": b"late"}],
        )

    monkeypatch.setattr(edge_tts, "Communicate", fake_communicate)

    tts = EdgeTTS(timeout=0.05)
    dest = tmp_path / "audio.mp3"
    with pytest.raises(Short2ShortError, match="timed out"):
        tts.synthesize("Text", dest, voice="vi-VN-HoaiMyNeural")
