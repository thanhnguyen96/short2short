from __future__ import annotations

import re
import uuid
from pathlib import Path

from short2short.core.models import Aspect, JobState, JobStatus, aspect_size
from short2short.errors import ProviderError
from short2short.ffmpeg import Runner, default_runner

_ASPECT_TO_CODE: dict[Aspect | str, str] = {
    Aspect.PORTRAIT: "p",
    Aspect.LANDSCAPE: "l",
    Aspect.SQUARE: "s",
    "9:16": "p",
    "16:9": "l",
    "1:1": "s",
}

_CODE_TO_ASPECT: dict[str, Aspect] = {
    "p": Aspect.PORTRAIT,
    "l": Aspect.LANDSCAPE,
    "s": Aspect.SQUARE,
}

_PALETTE: list[str] = ["red", "blue", "green", "yellow", "purple", "orange"]

_JOB_ID_RE = re.compile(r"^fake-([pls])-(-?\d+)-(\d+)-([0-9a-fA-F]{8})$")


class FakeProvider:
    """Fake video provider generating solid-color video via ffmpeg lavfi."""

    name: str = "fake"
    max_clip_seconds: int = 30
    paid: bool = False
    poll_interval_seconds: float = 0.0
    poll_timeout_seconds: float = 10.0

    def __init__(self, bin: str = "ffmpeg", runner: Runner | None = None) -> None:
        """Initialize FakeProvider with ffmpeg binary and optional command runner."""
        self.bin: str = bin
        self.runner: Runner = runner if runner is not None else default_runner

    def submit(self, prompt: str, aspect: Aspect, seconds: int, seed: int | None) -> str:
        """Submit a fake clip generation request returning a deterministic job_id."""
        aspect_code = _ASPECT_TO_CODE.get(aspect)
        if aspect_code is None:
            try:
                aspect_code = _ASPECT_TO_CODE[Aspect(aspect)]
            except (ValueError, KeyError) as exc:
                raise ProviderError(
                    f"Unsupported aspect ratio: {aspect!r}", provider=self.name
                ) from exc

        actual_seed = 0 if seed is None else int(seed)
        actual_seconds = max(1, min(int(seconds), self.max_clip_seconds))
        hex8 = uuid.uuid4().hex[:8]
        return f"fake-{aspect_code}-{actual_seed}-{actual_seconds}-{hex8}"

    def poll(self, job_id: str) -> JobStatus:
        """Poll status of a job_id by parsing its components."""
        self._parse_job_id(job_id)
        return JobStatus(state=JobState.SUCCEEDED, job_id=job_id)

    def download(self, job_id: str, dest: Path) -> Path:
        """Generate solid-color clip into dest using ffmpeg lavfi."""
        aspect, seed, seconds = self._parse_job_id(job_id)
        width, height = aspect_size(aspect)
        color = _PALETTE[seed % len(_PALETTE)]

        dest.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            self.bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}:d={seconds}:r=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ]
        self.runner(argv)
        return dest

    def _parse_job_id(self, job_id: str) -> tuple[Aspect, int, int]:
        match = _JOB_ID_RE.fullmatch(job_id)
        if match is None:
            raise ProviderError(f"Unknown job_id format: {job_id!r}", provider=self.name)
        code = match.group(1)
        seed = int(match.group(2))
        seconds = int(match.group(3))
        aspect = _CODE_TO_ASPECT[code]
        return aspect, seed, seconds
