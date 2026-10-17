from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from short2short.core.models import Aspect, JobState, aspect_size
from short2short.errors import ProviderError
from short2short.ffmpeg import default_runner
from short2short.providers.base import VideoProvider
from short2short.providers.fake import FakeProvider


def test_fake_provider_implements_protocol() -> None:
    provider = FakeProvider()
    assert isinstance(provider, VideoProvider)
    assert provider.name == "fake"
    assert provider.max_clip_seconds == 30
    assert provider.paid is False
    assert provider.poll_interval_seconds == 0.0
    assert provider.poll_timeout_seconds == 10.0


def test_fake_provider_custom_bin_and_runner() -> None:
    custom_runner = MagicMock()
    provider = FakeProvider(bin="custom-ffmpeg", runner=custom_runner)
    assert provider.bin == "custom-ffmpeg"
    assert provider.runner is custom_runner


def test_fake_provider_default_runner() -> None:
    provider = FakeProvider()
    assert provider.runner is default_runner


def test_submit_aspect_codes() -> None:
    provider = FakeProvider()
    id_p = provider.submit("test", Aspect.PORTRAIT, 5, 0)
    assert id_p.startswith("fake-p-0-5-")

    id_l = provider.submit("test", Aspect.LANDSCAPE, 5, 0)
    assert id_l.startswith("fake-l-0-5-")

    id_s = provider.submit("test", Aspect.SQUARE, 5, 0)
    assert id_s.startswith("fake-s-0-5-")


def test_submit_aspect_str_values() -> None:
    provider = FakeProvider()
    id_p = provider.submit("test", "9:16", 5, 0)  # type: ignore[arg-type]
    assert id_p.startswith("fake-p-0-5-")


def test_submit_seeds() -> None:
    provider = FakeProvider()
    id_none = provider.submit("test", Aspect.PORTRAIT, 5, None)
    assert id_none.startswith("fake-p-0-5-")

    id_seed = provider.submit("test", Aspect.PORTRAIT, 5, 42)
    assert id_seed.startswith("fake-p-42-5-")

    id_neg = provider.submit("test", Aspect.PORTRAIT, 5, -7)
    assert id_neg.startswith("fake-p--7-5-")


def test_submit_seconds_clamping() -> None:
    provider = FakeProvider()
    id_normal = provider.submit("test", Aspect.PORTRAIT, 8, 0)
    assert id_normal.startswith("fake-p-0-8-")

    id_zero = provider.submit("test", Aspect.PORTRAIT, 0, 0)
    assert id_zero.startswith("fake-p-0-1-")

    id_neg = provider.submit("test", Aspect.PORTRAIT, -10, 0)
    assert id_neg.startswith("fake-p-0-1-")

    id_max = provider.submit("test", Aspect.PORTRAIT, 30, 0)
    assert id_max.startswith("fake-p-0-30-")

    id_exceed = provider.submit("test", Aspect.PORTRAIT, 100, 0)
    assert id_exceed.startswith("fake-p-0-30-")


def test_submit_invalid_aspect() -> None:
    provider = FakeProvider()
    with pytest.raises(ProviderError) as exc_info:
        provider.submit("test", "bad_aspect", 5, 0)  # type: ignore[arg-type]
    assert "Unsupported aspect ratio" in str(exc_info.value)
    assert exc_info.value.provider == "fake"


def test_poll_valid_job_id() -> None:
    provider = FakeProvider()
    job_id = provider.submit("prompt", Aspect.LANDSCAPE, 10, seed=3)
    status = provider.poll(job_id)
    assert status.state == JobState.SUCCEEDED
    assert status.job_id == job_id
    assert status.error is None


def test_poll_invalid_job_id() -> None:
    provider = FakeProvider()
    invalid_ids = [
        "not-a-job-id",
        "fake-x-0-5-12345678",
        "fake-p-abc-5-12345678",
        "fake-p-0-xyz-12345678",
        "fake-p-0-5-1234",
        "fake-p-0-5-123456789",
        "fake-p-0-5-",
    ]
    for bad_id in invalid_ids:
        with pytest.raises(ProviderError) as exc_info:
            provider.poll(bad_id)
        assert exc_info.value.provider == "fake"
        assert "Unknown job_id format" in str(exc_info.value)


def test_download_builds_argv_and_calls_runner(tmp_path: Path) -> None:
    captured_argv: list[list[str]] = []

    def mock_runner(argv: list[str]) -> None:
        captured_argv.append(argv)

    provider = FakeProvider(bin="ffmpeg-test", runner=mock_runner)
    job_id = provider.submit("prompt", Aspect.PORTRAIT, 6, seed=1)
    dest = tmp_path / "subdir" / "clip.mp4"

    res = provider.download(job_id, dest)
    assert res == dest

    assert len(captured_argv) == 1
    argv = captured_argv[0]
    expected_w, expected_h = aspect_size(Aspect.PORTRAIT)
    assert argv == [
        "ffmpeg-test",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s={expected_w}x{expected_h}:d=6:r=30",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(dest),
    ]


def test_download_palette_rotation(tmp_path: Path) -> None:
    palette_colors = ["red", "blue", "green", "yellow", "purple", "orange"]
    captured: list[list[str]] = []
    provider = FakeProvider(runner=captured.append)

    for seed, expected_color in enumerate(palette_colors):
        captured.clear()
        job_id = provider.submit("prompt", Aspect.SQUARE, 5, seed=seed)
        dest = tmp_path / f"color_{seed}.mp4"
        provider.download(job_id, dest)
        color_arg = next(arg for arg in captured[0] if arg.startswith("color=c="))
        assert f"color=c={expected_color}:" in color_arg

    # seed 6 cycles back to red
    captured.clear()
    job_id = provider.submit("prompt", Aspect.SQUARE, 5, seed=6)
    dest = tmp_path / "color_6.mp4"
    provider.download(job_id, dest)
    color_arg = next(arg for arg in captured[0] if arg.startswith("color=c="))
    assert "color=c=red:" in color_arg


def test_download_aspect_dimensions(tmp_path: Path) -> None:
    captured: list[list[str]] = []
    provider = FakeProvider(runner=captured.append)

    for aspect in [Aspect.PORTRAIT, Aspect.LANDSCAPE, Aspect.SQUARE]:
        captured.clear()
        job_id = provider.submit("prompt", aspect, 5, seed=0)
        dest = tmp_path / f"{aspect.name}.mp4"
        provider.download(job_id, dest)
        w, h = aspect_size(aspect)
        color_arg = next(arg for arg in captured[0] if arg.startswith("color=c="))
        assert f"s={w}x{h}:" in color_arg


def test_download_invalid_job_id(tmp_path: Path) -> None:
    provider = FakeProvider(runner=lambda _: None)
    dest = tmp_path / "clip.mp4"
    with pytest.raises(ProviderError) as exc_info:
        provider.download("malformed_id", dest)
    assert exc_info.value.provider == "fake"
    assert "Unknown job_id format" in str(exc_info.value)
