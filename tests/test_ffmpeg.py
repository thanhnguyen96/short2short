from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from short2short.errors import Short2ShortError
from short2short.ffmpeg import default_runner, ffprobe_duration, run_ffmpeg


def test_run_ffmpeg_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, Any] = {}

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", mock_run)

    run_ffmpeg("ffmpeg", ["-i", "input.mp4", "output.mp4"])
    assert captured_kwargs.get("shell") is False


def test_run_ffmpeg_nonzero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            cmd, 1, stdout=b"", stderr=b"Error: api_key=sk-secret invalid"
        )

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(Short2ShortError) as exc_info:
        run_ffmpeg("ffmpeg", ["-i", "bad.mp4"])

    assert "exited with 1" in str(exc_info.value)
    # Ensure secrets are redacted from stderr
    assert "sk-secret" not in str(exc_info.value)
    assert "api_key=***" in str(exc_info.value)


def test_run_ffmpeg_type_error() -> None:
    with pytest.raises(TypeError):
        run_ffmpeg(123, ["-i", "input.mp4"])  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        run_ffmpeg("ffmpeg", ["-i", 123])  # type: ignore[list-item]


def test_default_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr("short2short.ffmpeg.run_ffmpeg", mock_run)

    default_runner(["ffmpeg", "-y", "-i", "in.mp4", "out.mp4"])
    mock_run.assert_called_once_with("ffmpeg", ["-y", "-i", "in.mp4", "out.mp4"])

    with pytest.raises(TypeError):
        default_runner([])


def test_ffprobe_duration_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sample_json = json.dumps({"format": {"duration": "12.345"}}).encode("utf-8")

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert "ffprobe" in cmd[0]
        return subprocess.CompletedProcess(cmd, 0, stdout=sample_json, stderr=b"")

    monkeypatch.setattr(subprocess, "run", mock_run)

    dur = ffprobe_duration("ffmpeg", tmp_path / "video.mp4")
    assert dur == pytest.approx(12.345)


def test_ffprobe_duration_missing_or_invalid_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mock_run_bad_json(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"not json", stderr=b"")

    monkeypatch.setattr(subprocess, "run", mock_run_bad_json)
    with pytest.raises(Short2ShortError):
        ffprobe_duration("ffmpeg", tmp_path / "video.mp4")

    def mock_run_no_dur(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"format": {}}).encode("utf-8"), stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", mock_run_no_dur)
    with pytest.raises(Short2ShortError):
        ffprobe_duration("ffmpeg", tmp_path / "video.mp4")

    def mock_run_nan(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"format": {"duration": "NaN"}}).encode("utf-8"), stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", mock_run_nan)
    with pytest.raises(Short2ShortError):
        ffprobe_duration("ffmpeg", tmp_path / "video.mp4")
