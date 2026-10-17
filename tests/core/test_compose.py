from __future__ import annotations

from pathlib import Path

import pytest

from short2short.core.compose import (
    ENC,
    FitMode,
    _escape_subtitle_text,
    aspect_size,
    audio_concat_argv,
    burn_subtitles_argv,
    compose_task,
    concat_argv,
    concat_list_text,
    escape_concat_path,
    escape_subtitles_filter_path,
    fit_clip_argv,
    mix_bgm_argv,
    mux_audio_argv,
    placeholder_argv,
)
from short2short.core.models import Aspect, Scene, SceneState
from short2short.errors import PathSecurityError, PipelineError


def test_aspect_size_reexport() -> None:
    assert aspect_size(Aspect.PORTRAIT) == (1080, 1920)
    assert aspect_size(Aspect.LANDSCAPE) == (1920, 1080)
    assert aspect_size(Aspect.SQUARE) == (1080, 1080)


def test_escape_concat_path() -> None:
    p = Path("C:/video/clip's test.mp4")
    escaped = escape_concat_path(p)
    assert "'\\''" in escaped


def test_concat_list_text() -> None:
    paths = [Path("C:/clip1.mp4"), Path("C:/clip2.mp4")]
    text = concat_list_text(paths)
    lines = text.splitlines(keepends=True)
    assert len(lines) == 2
    assert lines[0].startswith("file '")
    assert lines[0].endswith(".mp4'\n")
    assert lines[1].startswith("file '")
    assert lines[1].endswith(".mp4'\n")

    assert concat_list_text([]) == ""


def test_escape_subtitle_text_golden() -> None:
    assert _escape_subtitle_text("C:/Users/me/task/sub.srt") == "C\\:/Users/me/task/sub.srt"
    assert _escape_subtitle_text(r"C:\test\it's.srt") == r"C\:\\test\\it\'s.srt"


def test_escape_subtitles_filter_path() -> None:
    p = Path("C:/task/subtitles.srt")
    esc = escape_subtitles_filter_path(p)
    assert "\\:" in esc
    assert "\\" not in esc or "\\:" in esc


def test_fit_clip_argv_cover() -> None:
    src = Path("C:/src.mp4")
    dest = Path("C:/dest.mp4")
    argv = fit_clip_argv(
        "ffmpeg",
        src,
        dest,
        aspect=Aspect.PORTRAIT,
        duration=5.5,
        fit=FitMode.COVER,
    )
    assert argv[0] == "ffmpeg"
    assert argv[1:6] == ["-y", "-stream_loop", "-1", "-i", str(src)]
    assert argv[6:8] == ["-t", "5.500"]
    assert argv[8] == "-an"
    assert argv[9] == "-vf"
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in argv[10]
    assert argv[11 : 11 + len(ENC)] == ENC
    assert argv[-1] == str(dest)


def test_fit_clip_argv_contain() -> None:
    src = Path("C:/src.mp4")
    dest = Path("C:/dest.mp4")
    argv = fit_clip_argv(
        "ffmpeg",
        src,
        dest,
        aspect=Aspect.LANDSCAPE,
        duration=3.0,
        fit=FitMode.CONTAIN,
    )
    assert argv[6:8] == ["-t", "3.000"]
    assert "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080" in argv[10]


def test_fit_clip_argv_invalid_fit() -> None:
    with pytest.raises(ValueError):
        fit_clip_argv(
            "ffmpeg",
            Path("s.mp4"),
            Path("d.mp4"),
            aspect=Aspect.SQUARE,
            duration=1.0,
            fit="invalid",  # type: ignore[arg-type]
        )


def test_placeholder_argv() -> None:
    dest = Path("C:/work/scene_001.mp4")
    argv = placeholder_argv("ffmpeg", dest, aspect=Aspect.PORTRAIT, duration=4.25)
    assert argv == [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=1080x1920:d=4.250:r=30",
        "-an",
        *ENC,
        str(dest),
    ]


def test_concat_argv() -> None:
    list_file = Path("C:/work/concat.txt")
    dest = Path("C:/work/combined.mp4")
    argv = concat_argv("ffmpeg", list_file, dest)
    assert argv == [
        "ffmpeg",
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


def test_audio_concat_argv() -> None:
    list_file = Path("C:/work/audio_concat.txt")
    dest = Path("C:/audio/narration.mp3")
    argv = audio_concat_argv("ffmpeg", list_file, dest)
    assert argv == [
        "ffmpeg",
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


def test_mux_audio_argv() -> None:
    video = Path("C:/work/combined.mp4")
    audio = Path("C:/audio/narration.mp3")
    dest = Path("C:/work/with_audio.mp4")
    argv = mux_audio_argv("ffmpeg", video, audio, dest)
    assert argv == [
        "ffmpeg",
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


def test_burn_subtitles_argv() -> None:
    video = Path("C:/work/with_audio.mp4")
    srt = Path("C:/task/sub.srt")
    dest = Path("C:/task/final.mp4")
    argv = burn_subtitles_argv("ffmpeg", video, srt, dest, font_size=24)
    assert argv[0:4] == ["ffmpeg", "-y", "-i", str(video)]
    assert argv[4] == "-vf"
    assert "subtitles=" in argv[5]
    assert "FontSize=24" in argv[5]
    assert "PrimaryColour=&H00FFFFFF" in argv[5]
    assert argv[6:] == [
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


def test_mix_bgm_argv_long_duration() -> None:
    voice = Path("C:/voice.mp3")
    bgm = Path("C:/bgm.mp3")
    dest = Path("C:/mix.m4a")
    argv = mix_bgm_argv("ffmpeg", voice, bgm, dest, volume=0.15, voice_duration=10.0)
    assert argv[0:7] == ["ffmpeg", "-y", "-i", str(voice), "-stream_loop", "-1", "-i"]
    assert argv[7] == str(bgm)
    assert argv[8] == "-filter_complex"
    fc = argv[9]
    assert "volume=0.150" in fc
    assert "afade=t=out:st=7.000:d=3.000[bg]" in fc
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]" in fc
    assert argv[10:] == ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", str(dest)]


def test_mix_bgm_argv_short_duration() -> None:
    voice = Path("C:/voice.mp3")
    bgm = Path("C:/bgm.mp3")
    dest = Path("C:/mix.m4a")
    argv = mix_bgm_argv("ffmpeg", voice, bgm, dest, volume=0.2, voice_duration=2.0)
    fc = argv[9]
    assert "volume=0.200" in fc
    assert "afade=t=out:st=0.000:d=2.000[bg]" in fc


def test_compose_task_empty_scenes(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="No scenes to compose"):
        compose_task(
            bin="ffmpeg",
            task_dir=tmp_path,
            scenes=[],
            aspect=Aspect.PORTRAIT,
            narration=tmp_path / "narration.mp3",
            srt=None,
            bgm=None,
            bgm_volume=0.0,
            font_size=18,
            runner=lambda _: None,
        )


def test_compose_task_invalid_scene_state(tmp_path: Path) -> None:
    scene = Scene(
        index=0,
        narration="Hello",
        visual_prompt="A sunset",
        state=SceneState.PENDING,
        duration_seconds=3.0,
    )
    with pytest.raises(PipelineError, match="invalid state"):
        compose_task(
            bin="ffmpeg",
            task_dir=tmp_path,
            scenes=[scene],
            aspect=Aspect.PORTRAIT,
            narration=tmp_path / "narration.mp3",
            srt=None,
            bgm=None,
            bgm_volume=0.0,
            font_size=18,
            runner=lambda _: None,
        )


def test_compose_task_invalid_duration(tmp_path: Path) -> None:
    scene = Scene(
        index=0,
        narration="Hello",
        visual_prompt="A sunset",
        state=SceneState.DOWNLOADED,
        clip_relpath="clips/scene_000.mp4",
        duration_seconds=None,
    )
    with pytest.raises(PipelineError, match="duration missing or invalid"):
        compose_task(
            bin="ffmpeg",
            task_dir=tmp_path,
            scenes=[scene],
            aspect=Aspect.PORTRAIT,
            narration=tmp_path / "narration.mp3",
            srt=None,
            bgm=None,
            bgm_volume=0.0,
            font_size=18,
            runner=lambda _: None,
        )


def test_compose_task_downloaded_missing_clip_relpath(tmp_path: Path) -> None:
    scene = Scene(
        index=0,
        narration="Hello",
        visual_prompt="A sunset",
        state=SceneState.DOWNLOADED,
        clip_relpath=None,
        duration_seconds=3.0,
    )
    with pytest.raises(PipelineError, match="clip_relpath is None"):
        compose_task(
            bin="ffmpeg",
            task_dir=tmp_path,
            scenes=[scene],
            aspect=Aspect.PORTRAIT,
            narration=tmp_path / "narration.mp3",
            srt=None,
            bgm=None,
            bgm_volume=0.0,
            font_size=18,
            runner=lambda _: None,
        )


def test_compose_task_clip_path_traversal(tmp_path: Path) -> None:
    scene = Scene(
        index=0,
        narration="Hello",
        visual_prompt="A sunset",
        state=SceneState.DOWNLOADED,
        clip_relpath="../outside.mp4",
        duration_seconds=3.0,
    )
    with pytest.raises(PathSecurityError):
        compose_task(
            bin="ffmpeg",
            task_dir=tmp_path,
            scenes=[scene],
            aspect=Aspect.PORTRAIT,
            narration=tmp_path / "narration.mp3",
            srt=None,
            bgm=None,
            bgm_volume=0.0,
            font_size=18,
            runner=lambda _: None,
        )


def test_compose_task_full_pipeline_runner_order(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_file = clips_dir / "scene_000.mp4"
    clip_file.write_bytes(b"dummy clip")

    scene0 = Scene(
        index=0,
        narration="First",
        visual_prompt="Prompt 1",
        state=SceneState.DOWNLOADED,
        clip_relpath="clips/scene_000.mp4",
        duration_seconds=4.0,
    )
    scene1 = Scene(
        index=1,
        narration="Second",
        visual_prompt="Prompt 2",
        state=SceneState.FAILED_POLICY,
        duration_seconds=3.0,
    )

    narration_file = tmp_path / "audio" / "narration.mp3"
    narration_file.parent.mkdir(parents=True, exist_ok=True)
    narration_file.write_bytes(b"dummy narration")

    srt_file = tmp_path / "subtitle.srt"
    srt_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    bgm_file = tmp_path / "bgm.mp3"
    bgm_file.write_bytes(b"dummy bgm")

    called_commands: list[list[str]] = []

    def mock_runner(argv: list[str]) -> None:
        called_commands.append(argv)
        # Create destination file so subsequent checks find it if needed
        dest = Path(argv[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"output")

    result_path = compose_task(
        bin="ffmpeg",
        task_dir=tmp_path,
        scenes=[scene0, scene1],
        aspect=Aspect.PORTRAIT,
        narration=narration_file,
        srt=srt_file,
        bgm=bgm_file,
        bgm_volume=0.15,
        font_size=20,
        fit=FitMode.COVER,
        runner=mock_runner,
    )

    assert result_path == tmp_path / "final.mp4"
    assert len(called_commands) == 6

    # 1. Fit scene 0
    assert called_commands[0][0] == "ffmpeg"
    assert "-stream_loop" in called_commands[0]
    assert "scene_000.mp4" in called_commands[0][-1]

    # 2. Placeholder scene 1
    assert called_commands[1][0] == "ffmpeg"
    assert "lavfi" in called_commands[1]
    assert "scene_001.mp4" in called_commands[1][-1]

    # Concat file was written directly on disk
    concat_txt = tmp_path / "work" / "concat.txt"
    assert concat_txt.exists()
    content = concat_txt.read_text(encoding="utf-8")
    assert "scene_000.mp4" in content
    assert "scene_001.mp4" in content

    # 3. Concat clips
    assert called_commands[2][0] == "ffmpeg"
    assert "concat" in called_commands[2]
    assert "combined.mp4" in called_commands[2][-1]

    # 4. Mix BGM
    assert called_commands[3][0] == "ffmpeg"
    assert "-filter_complex" in called_commands[3]
    assert "narration_mix.m4a" in called_commands[3][-1]

    # 5. Mux audio
    assert called_commands[4][0] == "ffmpeg"
    assert "-shortest" in called_commands[4]
    assert "with_audio.mp4" in called_commands[4][-1]

    # 6. Burn subtitles
    assert called_commands[5][0] == "ffmpeg"
    assert "subtitles=" in called_commands[5][5]
    assert called_commands[5][-1] == str(tmp_path / "final.mp4")


def test_compose_task_no_bgm_no_srt(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "scene_000.mp4").write_bytes(b"dummy")

    scene0 = Scene(
        index=0,
        narration="First",
        visual_prompt="Prompt 1",
        state=SceneState.DOWNLOADED,
        clip_relpath="clips/scene_000.mp4",
        duration_seconds=2.0,
    )

    narration_file = tmp_path / "audio" / "narration.mp3"
    narration_file.parent.mkdir(parents=True, exist_ok=True)
    narration_file.write_bytes(b"dummy")

    called_commands: list[list[str]] = []

    def mock_runner(argv: list[str]) -> None:
        called_commands.append(argv)
        dest = Path(argv[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"output")

    result_path = compose_task(
        bin="ffmpeg",
        task_dir=tmp_path,
        scenes=[scene0],
        aspect=Aspect.LANDSCAPE,
        narration=narration_file,
        srt=None,
        bgm=None,
        bgm_volume=0.0,
        font_size=18,
        fit=FitMode.CONTAIN,
        runner=mock_runner,
    )

    assert result_path == tmp_path / "final.mp4"
    # Calls: 1. fit scene 0, 2. concat, 3. mux (no mix bgm, no burn srt)
    assert len(called_commands) == 3
    # Check mux received narration_file directly
    assert called_commands[2][5] == str(narration_file)
    # Check final.mp4 exists because with_audio.mp4 was replaced
    assert (tmp_path / "final.mp4").exists()


def test_compose_task_zero_bgm_volume(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "scene_000.mp4").write_bytes(b"dummy")

    scene0 = Scene(
        index=0,
        narration="First",
        visual_prompt="Prompt 1",
        state=SceneState.DOWNLOADED,
        clip_relpath="clips/scene_000.mp4",
        duration_seconds=2.0,
    )

    narration_file = tmp_path / "audio" / "narration.mp3"
    narration_file.parent.mkdir(parents=True, exist_ok=True)
    narration_file.write_bytes(b"dummy")

    bgm_file = tmp_path / "bgm.mp3"
    bgm_file.write_bytes(b"dummy bgm")

    called_commands: list[list[str]] = []

    def mock_runner(argv: list[str]) -> None:
        called_commands.append(argv)
        dest = Path(argv[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"output")

    compose_task(
        bin="ffmpeg",
        task_dir=tmp_path,
        scenes=[scene0],
        aspect=Aspect.LANDSCAPE,
        narration=narration_file,
        srt=None,
        bgm=bgm_file,
        bgm_volume=0.0,  # volume <= 0 skips mix
        font_size=18,
        fit=FitMode.COVER,
        runner=mock_runner,
    )

    # 1. Fit scene, 2. Concat, 3. Mux (BGM mix skipped)
    assert len(called_commands) == 3
    assert called_commands[2][5] == str(narration_file)
