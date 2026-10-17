from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import overload

from short2short.core.models import Scene, WordCue

logger = logging.getLogger(__name__)

PUNCTUATION: tuple[str, ...] = (".", "。", "！", "？", "!", "?", "…")  # noqa: RUF001


def format_srt_time(seconds: float) -> str:
    """Format seconds into SRT timestamp HH:MM:SS,mmm with millisecond rounding carry."""
    total_ms = max(0, round(seconds * 1000.0))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _ends_with_punct(text: str) -> bool:
    """Check if text ends with one of the configured sentence-ending punctuation marks."""
    return text.rstrip().endswith(PUNCTUATION)


def build_srt(scenes: Sequence[Scene], max_chars: int = 42) -> str:
    """Build SRT subtitle string from ordered scenes.

    Cumulative scene durations determine scene time offsets. Scenes without cues
    emit a single caption containing the narration. When cues are present, words are
    grouped until length > max_chars or a word ends with terminal punctuation.
    """
    captions: list[tuple[float, float, str]] = []
    current_offset = 0.0

    for scene in scenes:
        scene_offset = current_offset
        if scene.duration_seconds is not None:
            scene_duration = max(0.0, scene.duration_seconds)
        elif scene.word_cues:
            scene_duration = max(0.0, max(c.end for c in scene.word_cues))
        else:
            scene_duration = 0.0

        if not scene.word_cues:
            narration = scene.narration.strip()
            if narration:
                captions.append((scene_offset, scene_offset + scene_duration, narration))
        else:
            group: list[WordCue] = []
            for cue in scene.word_cues:
                group.append(cue)
                group_text = " ".join(c.text.strip() for c in group if c.text.strip())
                if len(group_text) > max_chars or _ends_with_punct(cue.text):
                    if group_text:
                        start_time = scene_offset + group[0].start
                        end_time = scene_offset + max(c.end for c in group)
                        captions.append((start_time, max(start_time, end_time), group_text))
                    group = []

            if group:
                group_text = " ".join(c.text.strip() for c in group if c.text.strip())
                if group_text:
                    start_time = scene_offset + group[0].start
                    end_time = scene_offset + max(c.end for c in group)
                    captions.append((start_time, max(start_time, end_time), group_text))

        current_offset += scene_duration

    if not captions:
        return ""

    blocks: list[str] = []
    for idx, (start, end, text) in enumerate(captions, start=1):
        start_str = format_srt_time(start)
        end_str = format_srt_time(end)
        blocks.append(f"{idx}\n{start_str} --> {end_str}\n{text}")

    return "\n\n".join(blocks) + "\n"


@overload
def write_srt(
    scenes: Sequence[Scene],
    dest: Path | str = ...,
    *,
    max_chars: int = 42,
) -> Path: ...


@overload
def write_srt(
    scenes: Path | str,
    dest: Sequence[Scene],
    *,
    max_chars: int = 42,
) -> Path: ...


def write_srt(
    scenes: Sequence[Scene] | Path | str,
    dest: Path | str | Sequence[Scene] = Path("subtitle.srt"),
    *,
    max_chars: int = 42,
) -> Path:
    """Write SRT subtitles to disk using UTF-8 encoding with LF newlines and no BOM."""
    if isinstance(scenes, (Path, str)) and not isinstance(dest, (Path, str)):
        scenes_seq: Sequence[Scene] = dest
        dest_path = Path(scenes)
    elif isinstance(dest, (Path, str)) and not isinstance(scenes, (Path, str)):
        scenes_seq = scenes
        dest_path = Path(dest)
    else:
        raise TypeError("write_srt requires a Sequence[Scene] and a destination Path")

    content = build_srt(scenes_seq, max_chars=max_chars)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8", newline="\n")
    return dest_path
