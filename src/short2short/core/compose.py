from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from short2short.core.models import Aspect, Scene, SceneState, aspect_size
from short2short.errors import PipelineError
from short2short.ffmpeg import Runner
from short2short.security import safe_join

logger = logging.getLogger(__name__)

ENC: list[str] = [
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-crf",
    "20",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
]


class FitMode(StrEnum):
    """Aspect ratio fitting strategy for clips."""

    COVER = "cover"
    CONTAIN = "contain"


def escape_concat_path(path: Path) -> str:
    """Escape file path for ffmpeg concat demuxer list."""
    return path.resolve().as_posix().replace("'", "'\\''")


def _escape_subtitle_text(text: str) -> str:
    """Escape path or text for ffmpeg subtitles filter."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def escape_subtitles_filter_path(path: Path) -> str:
    """Escape resolved POSIX path for ffmpeg subtitles filter option."""
    return _escape_subtitle_text(path.resolve().as_posix())


def concat_list_text(paths: Sequence[Path]) -> str:
    """Generate contents of an ffmpeg concat demuxer file."""
    return "".join(f"file '{escape_concat_path(p)}'\n" for p in paths)


def fit_clip_argv(
    bin: str,
    src: Path,
    dest: Path,
    *,
    aspect: Aspect,
    duration: float,
    fit: FitMode,
) -> list[str]:
    """Build ffmpeg argv to scale or pad clip to target aspect and duration."""
    w, h = aspect_size(aspect)
    d = f"{duration:.3f}"
    if fit == FitMode.COVER:
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps=30,format=yuv420p"
        )
    elif fit == FitMode.CONTAIN:
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"
        )
    else:
        raise ValueError(f"Unknown fit mode: {fit!r}")

    return [
        bin,
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(src),
        "-t",
        d,
        "-an",
        "-vf",
        vf,
        *ENC,
        str(dest),
    ]


def placeholder_argv(
    bin: str,
    dest: Path,
    *,
    aspect: Aspect,
    duration: float,
) -> list[str]:
    """Build ffmpeg argv for solid black placeholder video."""
    w, h = aspect_size(aspect)
    d = f"{duration:.3f}"
    return [
        bin,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={w}x{h}:d={d}:r=30",
        "-an",
        *ENC,
        str(dest),
    ]


def concat_argv(bin: str, list_file: Path, dest: Path) -> list[str]:
    """Build ffmpeg argv to concatenate video clips."""
    return [
        bin,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-an",
        *ENC,
        str(dest),
    ]


def audio_concat_argv(bin: str, list_file: Path, dest: Path) -> list[str]:
    """Build ffmpeg argv to concatenate audio files into mp3."""
    return [
        bin,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c:a",
        "libmp3lame",
        "-q:a",
        "4",
        str(dest),
    ]


def mux_audio_argv(bin: str, video: Path, audio: Path, dest: Path) -> list[str]:
    """Build ffmpeg argv to multiplex video and audio tracks."""
    return [
        bin,
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(dest),
    ]


def burn_subtitles_argv(
    bin: str,
    video: Path,
    srt: Path,
    dest: Path,
    *,
    font_size: int,
) -> list[str]:
    """Build ffmpeg argv to hardcode subtitles onto video."""
    esc = escape_subtitles_filter_path(srt)
    vf = (
        f"subtitles='{esc}':charenc=UTF-8:force_style='FontName=Arial,"
        f"FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline=2,Alignment=2,MarginV=40'"
    )
    return [
        bin,
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]


def mix_bgm_argv(
    bin: str,
    voice: Path,
    bgm: Path,
    dest: Path,
    *,
    volume: float,
    voice_duration: float,
) -> list[str]:
    """Build ffmpeg argv to mix background music with narration track."""
    fade = min(3.0, voice_duration)
    st = max(0.0, voice_duration - 3.0)
    fc = (
        f"[1:a]volume={volume:.3f},afade=t=out:st={st:.3f}:d={fade:.3f}[bg];"
        f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )
    return [
        bin,
        "-y",
        "-i",
        str(voice),
        "-stream_loop",
        "-1",
        "-i",
        str(bgm),
        "-filter_complex",
        fc,
        "-map",
        "[a]",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(dest),
    ]


def compose_task(
    *,
    bin: str,
    task_dir: Path,
    scenes: Sequence[Scene],
    aspect: Aspect,
    narration: Path,
    srt: Path | None,
    bgm: Path | None,
    bgm_volume: float,
    font_size: int,
    fit: FitMode = FitMode.COVER,
    runner: Runner,
) -> Path:
    """Compose full task video from scenes, narration, subtitles, and background music."""
    if not scenes:
        raise PipelineError("No scenes to compose")

    for scene in scenes:
        if scene.state not in (SceneState.DOWNLOADED, SceneState.FAILED_POLICY):
            raise PipelineError(f"Scene {scene.index} has invalid state for compose: {scene.state}")
        if scene.duration_seconds is None or scene.duration_seconds <= 0:
            raise PipelineError(
                f"Scene {scene.index} duration missing or invalid: {scene.duration_seconds}"
            )
        if scene.state == SceneState.DOWNLOADED and not scene.clip_relpath:
            raise PipelineError(f"Scene {scene.index} is DOWNLOADED but clip_relpath is None")

    work_dir = safe_join(task_dir, "work")
    work_dir.mkdir(parents=True, exist_ok=True)

    scene_clip_paths: list[Path] = []
    for scene in scenes:
        dest_clip = safe_join(work_dir, f"scene_{scene.index:03d}.mp4")
        assert scene.duration_seconds is not None
        if scene.state == SceneState.DOWNLOADED:
            assert scene.clip_relpath is not None
            src_clip = safe_join(task_dir, scene.clip_relpath)
            argv = fit_clip_argv(
                bin,
                src_clip,
                dest_clip,
                aspect=aspect,
                duration=scene.duration_seconds,
                fit=fit,
            )
        else:
            argv = placeholder_argv(
                bin,
                dest_clip,
                aspect=aspect,
                duration=scene.duration_seconds,
            )
        runner(argv)
        scene_clip_paths.append(dest_clip)

    concat_file = safe_join(work_dir, "concat.txt")
    concat_file.write_text(concat_list_text(scene_clip_paths), encoding="utf-8")

    combined_mp4 = safe_join(work_dir, "combined.mp4")
    runner(concat_argv(bin, concat_file, combined_mp4))

    audio_for_mux = narration
    if bgm is not None and bgm_volume > 0:
        total_voice_duration = sum(
            s.duration_seconds for s in scenes if s.duration_seconds is not None
        )
        narration_mix = safe_join(work_dir, "narration_mix.m4a")
        runner(
            mix_bgm_argv(
                bin,
                narration,
                bgm,
                narration_mix,
                volume=bgm_volume,
                voice_duration=total_voice_duration,
            )
        )
        audio_for_mux = narration_mix

    with_audio = safe_join(work_dir, "with_audio.mp4")
    runner(mux_audio_argv(bin, combined_mp4, audio_for_mux, with_audio))

    final_mp4 = safe_join(task_dir, "final.mp4")
    if srt is not None:
        runner(burn_subtitles_argv(bin, with_audio, srt, final_mp4, font_size=font_size))
    else:
        if with_audio.exists():
            os.replace(with_audio, final_mp4)

    return final_mp4
