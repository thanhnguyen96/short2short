from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from short2short.core.models import Aspect, JobStatus


@runtime_checkable
class VideoProvider(Protocol):
    """Protocol for video generation providers."""

    name: str
    max_clip_seconds: int
    paid: bool

    def submit(self, prompt: str, aspect: Aspect, seconds: int, seed: int | None) -> str: ...

    def poll(self, job_id: str) -> JobStatus: ...

    def download(self, job_id: str, dest: Path) -> Path: ...
