from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from short2short.core.compose import FitMode, compose_task
from short2short.core.models import Aspect, Scene, SceneState
from short2short.ffmpeg import default_runner, ffprobe_duration


@pytest.mark.integration
def test_compose_task_integration_end_to_end(tmp_path: Path) -> None:
    """Run real ffmpeg end-to-end burning real SRT on Windows path with drive colon."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not found on PATH")

    task_dir = tmp_path / "task_integration"
    task_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = task_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = task_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create real clip for scene 0 (1.0s video)
    clip_path = clips_dir / "scene_000.mp4"
    res_clip = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=1.0:r=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip_path),
        ],
        capture_output=True,
    )
    assert res_clip.returncode == 0, res_clip.stderr.decode("utf-8", errors="replace")

    # 2. Create real narration mp3 (2.0s sine wave)
    narration_path = audio_dir / "narration.mp3"
    res_narration = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2.0",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(narration_path),
        ],
        capture_output=True,
    )
    assert res_narration.returncode == 0, res_narration.stderr.decode("utf-8", errors="replace")

    # 3. Create real BGM mp3 (3.0s sine wave)
    bgm_path = task_dir / "bgm.mp3"
    res_bgm = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=3.0",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(bgm_path),
        ],
        capture_output=True,
    )
    assert res_bgm.returncode == 0, res_bgm.stderr.decode("utf-8", errors="replace")

    # 4. Create real SRT file on Windows path with drive colon
    srt_path = task_dir / "subtitles.srt"
    srt_content = (
        "1\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "First scene text\n\n"
        "2\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "Second scene text\n"
    )
    srt_path.write_text(srt_content, encoding="utf-8")

    # 5. Define scenes: scene 0 downloaded (1.0s), scene 1 failed_policy (1.0s placeholder)
    scene0 = Scene(
        index=0,
        narration="First scene",
        visual_prompt="Blue sky",
        state=SceneState.DOWNLOADED,
        clip_relpath="clips/scene_000.mp4",
        duration_seconds=1.0,
    )
    scene1 = Scene(
        index=1,
        narration="Second scene",
        visual_prompt="Black screen",
        state=SceneState.FAILED_POLICY,
        duration_seconds=1.0,
    )

    # 6. Compose task
    final_path = compose_task(
        bin="ffmpeg",
        task_dir=task_dir,
        scenes=[scene0, scene1],
        aspect=Aspect.PORTRAIT,
        narration=narration_path,
        srt=srt_path,
        bgm=bgm_path,
        bgm_volume=0.2,
        font_size=18,
        fit=FitMode.COVER,
        runner=default_runner,
    )

    # 7. Verify outputs
    assert final_path == task_dir / "final.mp4"
    assert final_path.exists()
    assert final_path.stat().st_size > 0

    duration = ffprobe_duration("ffmpeg", final_path)
    assert duration == pytest.approx(2.0, abs=0.2)


@pytest.mark.integration
def test_compose_task_integration_without_subtitles_or_bgm(tmp_path: Path) -> None:
    """Run real ffmpeg end-to-end without subtitles or BGM to verify os.replace path."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not found on PATH")

    task_dir = tmp_path / "task_nosub"
    task_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = task_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = task_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    clip_path = clips_dir / "scene_000.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x240:d=1.0:r=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip_path),
        ],
        check=True,
        capture_output=True,
    )

    narration_path = audio_dir / "narration.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.0",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(narration_path),
        ],
        check=True,
        capture_output=True,
    )

    scene0 = Scene(
        index=0,
        narration="Single scene",
        visual_prompt="Green background",
        state=SceneState.DOWNLOADED,
        clip_relpath="clips/scene_000.mp4",
        duration_seconds=1.0,
    )

    final_path = compose_task(
        bin="ffmpeg",
        task_dir=task_dir,
        scenes=[scene0],
        aspect=Aspect.LANDSCAPE,
        narration=narration_path,
        srt=None,
        bgm=None,
        bgm_volume=0.0,
        font_size=18,
        fit=FitMode.CONTAIN,
        runner=default_runner,
    )

    assert final_path == task_dir / "final.mp4"
    assert final_path.exists()
    assert final_path.stat().st_size > 0

    duration = ffprobe_duration("ffmpeg", final_path)
    assert duration == pytest.approx(1.0, abs=0.2)
