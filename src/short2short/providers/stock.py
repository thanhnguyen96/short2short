from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from short2short.core.models import Aspect, JobState, JobStatus, aspect_size
from short2short.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderTimeoutError,
)
from short2short.net import download_url
from short2short.security import redact
from short2short.storage.cache import ClipCache

logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

PEXELS_SEARCH_URL: str = "https://api.pexels.com/videos/search"

DEFAULT_ALLOW_HOSTS: frozenset[str] = frozenset(
    {"videos.pexels.com", "images.pexels.com", "storage.googleapis.com"}
)

ORIENTATION_MAP: dict[Aspect, str] = {
    Aspect.PORTRAIT: "portrait",
    Aspect.LANDSCAPE: "landscape",
    Aspect.SQUARE: "square",
}


@dataclass(frozen=True)
class _CandidateFile:
    url: str
    width: int
    height: int
    video_index: int
    file_index: int


def _candidate_score(cand: _CandidateFile, target_height: int) -> tuple[int, int, int, int]:
    """Score candidate by distance to target height, breaking ties with larger width."""
    return (
        abs(cand.height - target_height),
        -cand.width,
        cand.video_index,
        cand.file_index,
    )


class StockProvider:
    """Pexels stock video provider implementing VideoProvider protocol."""

    name: str = "stock"
    max_clip_seconds: int = 60
    paid: bool = False
    poll_interval_seconds: float = 1.0
    poll_timeout_seconds: float = 60.0

    def __init__(
        self,
        api_key: str,
        cache: ClipCache,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        allow_hosts: set[str] | list[str] | None = None,
    ) -> None:
        """Initialize Pexels stock provider with API key, cache, and optional client."""
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderAuthError("Pexels API key must not be empty", provider="stock")
        self.api_key: str = api_key.strip()
        self.cache: ClipCache = cache
        self.timeout: float = timeout
        self.allow_hosts: set[str] = (
            set(allow_hosts) if allow_hosts is not None else set(DEFAULT_ALLOW_HOSTS)
        )
        self._client: httpx.Client | None = client
        self._own_client: bool = False

    @property
    def client(self) -> httpx.Client:
        """Return HTTP client, creating default instance if not injected."""
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
            )
            self._own_client = True
        return self._client

    def close(self) -> None:
        """Close owned HTTP client if open."""
        if self._own_client and self._client is not None:
            self._client.close()
            self._client = None
            self._own_client = False

    def __enter__(self) -> StockProvider:
        """Context management entry."""
        return self

    def __exit__(self, *args: object) -> None:
        """Context management exit, closing client."""
        self.close()

    def _redact(self, text: str) -> str:
        """Redact secrets and raw api_key from text."""
        if not text:
            return ""
        if self.api_key and self.api_key in text:
            text = text.replace(self.api_key, "***")
        return redact(text)

    def _format_error_body(self, resp: httpx.Response) -> str:
        """Extract and redact response body tail up to 200 characters."""
        try:
            text = resp.text
        except Exception:
            text = ""
        redacted_full = self._redact(text)
        return redacted_full[-200:]

    def submit(
        self,
        prompt: str,
        aspect: Aspect,
        seconds: int,
        seed: int | None = None,
    ) -> str:
        """Search Pexels for a stock video matching prompt and aspect ratio."""
        if not prompt or not prompt.strip():
            raise ProviderError("Prompt must not be empty", provider=self.name)

        if isinstance(aspect, str):
            aspect = Aspect(aspect)

        orientation = ORIENTATION_MAP.get(aspect)
        if orientation is None:
            raise ProviderError(f"Unsupported aspect ratio: {aspect!r}", provider=self.name)

        _target_w, target_height = aspect_size(aspect)

        logger.info("Searching Pexels (prompt_len=%d, orientation=%s)", len(prompt), orientation)

        headers = {"Authorization": self.api_key}
        params: dict[str, Any] = {
            "query": prompt,
            "per_page": 10,
            "orientation": orientation,
        }

        try:
            resp = self.client.get(
                PEXELS_SEARCH_URL,
                headers=headers,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Pexels search timed out after {self.timeout}s",
                provider=self.name,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                f"Pexels request failed: {self._redact(str(exc))}",
                provider=self.name,
            ) from exc

        if resp.status_code in (401, 403):
            raise ProviderAuthError(
                f"Pexels auth failed ({resp.status_code}): {self._format_error_body(resp)}",
                provider=self.name,
            )
        if resp.status_code == 429:
            raise ProviderQuotaError(
                f"Pexels rate limit exceeded (429): {self._format_error_body(resp)}",
                provider=self.name,
            )
        if resp.status_code != 200:
            raise ProviderError(
                f"Pexels API error ({resp.status_code}): {self._format_error_body(resp)}",
                provider=self.name,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError(
                f"Invalid JSON response from Pexels: {self._redact(str(exc))}",
                provider=self.name,
            ) from exc

        if not isinstance(data, dict):
            raise ProviderError("Pexels response is not a JSON object", provider=self.name)

        videos = data.get("videos")
        if not isinstance(videos, list) or len(videos) == 0:
            raise ProviderError(
                f"No stock videos found for query: {prompt!r}",
                provider=self.name,
            )

        candidates: list[_CandidateFile] = []
        for v_idx, video in enumerate(videos):
            if not isinstance(video, dict):
                continue
            video_files = video.get("video_files")
            if not isinstance(video_files, list):
                continue
            for f_idx, f in enumerate(video_files):
                if not isinstance(f, dict):
                    continue
                file_type = str(f.get("file_type") or "").lower()
                link = str(f.get("link") or "").strip()
                if not link:
                    continue
                is_mp4 = "mp4" in file_type or link.split("?")[0].lower().endswith(".mp4")
                if not is_mp4:
                    continue
                width = f.get("width")
                height = f.get("height")
                if not isinstance(width, int) or not isinstance(height, int):
                    try:
                        width = int(width)  # type: ignore[arg-type]
                        height = int(height)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
                if width <= 0 or height <= 0:
                    continue
                candidates.append(
                    _CandidateFile(
                        url=link,
                        width=width,
                        height=height,
                        video_index=v_idx,
                        file_index=f_idx,
                    )
                )

        if not candidates:
            raise ProviderError(
                f"No suitable MP4 video files found for query: {prompt!r}",
                provider=self.name,
            )

        best = min(
            candidates,
            key=lambda c: _candidate_score(c, target_height),
        )

        job_id = f"stock-{secrets.token_hex(8)}"
        record = {
            "url": best.url,
            "width": best.width,
            "height": best.height,
        }
        self.cache.write_job_record(self.name, job_id, record)
        logger.info(
            "Selected stock video (%dx%d) for job %s",
            best.width,
            best.height,
            job_id,
        )
        return job_id

    def poll(self, job_id: str) -> JobStatus:
        """Poll job status from cached job record."""
        if not isinstance(job_id, str) or not job_id.startswith("stock-"):
            raise ProviderError(f"Invalid job ID: {job_id!r}", provider=self.name)

        record = self.cache.read_job_record(self.name, job_id)
        if record is None or not isinstance(record, dict) or "url" not in record:
            return JobStatus(
                state=JobState.FAILED,
                job_id=job_id,
                error=f"Job record not found for {job_id}",
            )

        return JobStatus(
            state=JobState.SUCCEEDED,
            job_id=job_id,
        )

    def download(self, job_id: str, dest: Path) -> Path:
        """Download video for job_id to dest path via net.download_url."""
        if not isinstance(job_id, str) or not job_id.startswith("stock-"):
            raise ProviderError(f"Invalid job ID: {job_id!r}", provider=self.name)

        record = self.cache.read_job_record(self.name, job_id)
        if record is None or not isinstance(record, dict) or "url" not in record:
            raise ProviderError(
                f"Job record not found for {job_id}",
                provider=self.name,
            )

        url = str(record["url"])
        download_url(
            url,
            dest,
            allow_hosts=self.allow_hosts,
            timeout=self.timeout,
            client=self.client,
        )
        return dest
