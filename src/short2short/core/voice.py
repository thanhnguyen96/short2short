from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import edge_tts
from edge_tts.exceptions import EdgeTTSException

from short2short.core.models import VoiceResult, WordCue
from short2short.errors import Short2ShortError

logger = logging.getLogger(__name__)

__all__ = ["EdgeTTS", "cues_from_chunks", "default_voice_for_lang"]

TICKS_PER_SECOND: float = 10_000_000.0


def default_voice_for_lang(lang: str, fallback: str = "vi-VN-HoaiMyNeural") -> str:
    """Return default voice for language code prefix (vi* or en*), else fallback."""
    normalized = lang.strip().lower()
    if normalized.startswith("vi"):
        return "vi-VN-HoaiMyNeural"
    if normalized.startswith("en"):
        return "en-US-AnaNeural"
    return fallback


def cues_from_chunks(chunks: Iterable[Mapping[str, Any]]) -> list[WordCue]:
    """Extract word timing cues from edge_tts metadata chunks.

    Prefers WordBoundary events. Falls back to SentenceBoundary events
    if no WordBoundary events are present.
    """
    chunk_list = list(chunks)
    word_chunks = [c for c in chunk_list if c.get("type") == "WordBoundary"]
    selected = (
        word_chunks
        if word_chunks
        else [c for c in chunk_list if c.get("type") == "SentenceBoundary"]
    )

    cues: list[WordCue] = []
    for c in selected:
        text = str(c.get("text") or "")
        offset = float(c.get("offset") or 0)
        duration = float(c.get("duration") or 0)
        start = max(0.0, offset / TICKS_PER_SECOND)
        end = max(start, (offset + duration) / TICKS_PER_SECOND)
        cues.append(WordCue(text=text, start=start, end=end))
    return cues


class EdgeTTS:
    """Edge TTS synthesizer implementing TTS protocol."""

    def __init__(self, rate: str = "+0%", timeout: float = 60.0) -> None:
        self.rate: str = rate
        self.timeout: float = float(timeout)

    def synthesize(self, text: str, dest: Path, *, voice: str) -> VoiceResult:
        """Synthesize text to audio file using Edge TTS.

        Args:
            text: Text narration to synthesize.
            dest: Destination audio file path.
            voice: Edge TTS voice name.

        Returns:
            VoiceResult containing audio path, duration, and word timing cues.

        Raises:
            Short2ShortError: If synthesis fails, audio is empty, or no timing cues.
        """

        async def _synthesize() -> tuple[bytes, list[Mapping[str, Any]]]:
            try:
                communicate = edge_tts.Communicate(
                    text, voice, rate=self.rate, boundary="WordBoundary"
                )
            except TypeError:
                communicate = edge_tts.Communicate(text, voice, rate=self.rate)

            audio_parts: list[bytes] = []
            meta_chunks: list[Mapping[str, Any]] = []

            async def _stream() -> None:
                async for chunk in communicate.stream():
                    chunk_type = chunk.get("type")
                    if chunk_type == "audio":
                        data = chunk.get("data")
                        if data:
                            audio_parts.append(data)
                    else:
                        meta_chunks.append(chunk)

            if self.timeout > 0:
                async with asyncio.timeout(self.timeout):
                    await _stream()
            else:
                await _stream()

            return b"".join(audio_parts), meta_chunks

        try:
            audio_bytes, metadata_chunks = asyncio.run(_synthesize())
        except TimeoutError as err:
            raise Short2ShortError(f"TTS timed out after {self.timeout}s") from err
        except EdgeTTSException as err:
            raise Short2ShortError(f"TTS synthesis failed: {err}") from err
        except Exception as err:
            if isinstance(err, Short2ShortError):
                raise
            raise Short2ShortError(f"TTS synthesis failed: {err}") from err

        if not audio_bytes:
            raise Short2ShortError("TTS produced empty audio")

        cues = cues_from_chunks(metadata_chunks)
        if cues:
            duration_seconds = cues[-1].end
        else:
            sentence_chunks = [c for c in metadata_chunks if c.get("type") == "SentenceBoundary"]
            if sentence_chunks:
                last = sentence_chunks[-1]
                offset = float(last.get("offset") or 0)
                duration = float(last.get("duration") or 0)
                duration_seconds = (offset + duration) / TICKS_PER_SECOND
            else:
                duration_seconds = 0.0

        if duration_seconds <= 0:
            raise Short2ShortError("tts produced no timing")

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(audio_bytes)

        return VoiceResult(
            path=dest,
            duration_seconds=duration_seconds,
            cues=cues,
        )
