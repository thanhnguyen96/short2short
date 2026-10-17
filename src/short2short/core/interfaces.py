from __future__ import annotations

from pathlib import Path
from typing import Protocol

from short2short.core.models import Script, VoiceResult


class ScriptGenerator(Protocol):
    """Protocol for generating video scripts."""

    def generate(
        self, topic: str, *, lang: str, scene_count: int, seconds_per_scene: int
    ) -> Script: ...


class TTS(Protocol):
    """Protocol for text-to-speech synthesis."""

    def synthesize(self, text: str, dest: Path, *, voice: str) -> VoiceResult: ...
