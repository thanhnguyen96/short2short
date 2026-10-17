from __future__ import annotations

from pathlib import Path

import pytest

from short2short.config import AppConfig
from short2short.core.models import Aspect, JobState, JobStatus
from short2short.errors import ConfigError
from short2short.providers.base import VideoProvider
from short2short.providers.registry import clear, get, names, register


class DummyProvider:
    name = "dummy"
    max_clip_seconds = 10
    paid = False

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def submit(self, prompt: str, aspect: Aspect, seconds: int, seed: int | None) -> str:
        return "dummy-job"

    def poll(self, job_id: str) -> JobStatus:
        return JobStatus(state=JobState.SUCCEEDED, job_id=job_id)

    def download(self, job_id: str, dest: Path) -> Path:
        return dest


def test_registry_lifecycle() -> None:
    clear()
    assert names() == []

    register("dummy", lambda cfg: DummyProvider(cfg))
    register("second", lambda cfg: DummyProvider(cfg))

    assert names() == ["dummy", "second"]

    cfg = AppConfig()
    p = get("dummy", cfg)
    assert isinstance(p, VideoProvider)
    assert p.name == "dummy"

    with pytest.raises(ConfigError) as exc_info:
        get("nonexistent", cfg)

    assert "Unknown provider: 'nonexistent'" in str(exc_info.value)
    assert "dummy, second" in str(exc_info.value)

    clear()
    assert names() == []
