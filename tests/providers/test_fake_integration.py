from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from short2short.core.models import Aspect, JobState
from short2short.ffmpeg import ffprobe_duration
from short2short.providers.fake import FakeProvider

has_ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg or ffprobe not on PATH"),
]


def test_fake_provider_real_ffmpeg_landscape(tmp_path: Path) -> None:
    provider = FakeProvider(bin="ffmpeg")
    job_id = provider.submit("draw mountain landscape", Aspect.LANDSCAPE, seconds=2, seed=1)

    status = provider.poll(job_id)
    assert status.state == JobState.SUCCEEDED

    dest = tmp_path / "landscape.mp4"
    result = provider.download(job_id, dest)
    assert result == dest
    assert dest.exists()
    assert dest.stat().st_size > 0

    duration = ffprobe_duration("ffmpeg", dest)
    assert pytest.approx(duration, abs=0.3) == 2.0


def test_fake_provider_real_ffmpeg_portrait(tmp_path: Path) -> None:
    provider = FakeProvider(bin="ffmpeg")
    job_id = provider.submit("draw portrait avatar", Aspect.PORTRAIT, seconds=1, seed=2)

    status = provider.poll(job_id)
    assert status.state == JobState.SUCCEEDED

    dest = tmp_path / "portrait.mp4"
    result = provider.download(job_id, dest)
    assert result == dest
    assert dest.exists()
    assert dest.stat().st_size > 0

    duration = ffprobe_duration("ffmpeg", dest)
    assert pytest.approx(duration, abs=0.3) == 1.0
