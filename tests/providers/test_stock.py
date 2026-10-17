from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from short2short.core.models import Aspect, JobState
from short2short.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderTimeoutError,
)
from short2short.providers.base import VideoProvider
from short2short.providers.stock import PEXELS_SEARCH_URL, StockProvider
from short2short.storage.cache import ClipCache


@pytest.fixture
def cache(tmp_path: Path) -> ClipCache:
    """Fixture providing an isolated ClipCache instance."""
    return ClipCache(tmp_path / "cache")


def _make_pexels_response(videos: list[dict[str, Any]]) -> dict[str, Any]:
    """Helper to build Pexels search response payload."""
    return {
        "page": 1,
        "per_page": 10,
        "total_results": len(videos),
        "url": "https://www.pexels.com/search/videos/test/",
        "videos": videos,
    }


def test_stock_provider_protocol(cache: ClipCache) -> None:
    """Verify StockProvider implements VideoProvider protocol with expected attributes."""
    provider = StockProvider(api_key="test-api-key", cache=cache)
    assert isinstance(provider, VideoProvider)
    assert provider.name == "stock"
    assert provider.max_clip_seconds == 60
    assert provider.paid is False
    assert provider.poll_interval_seconds == 1.0
    assert provider.poll_timeout_seconds == 60.0
    provider.close()


def test_stock_provider_empty_key_raises(cache: ClipCache) -> None:
    """Verify empty or blank API key raises ProviderAuthError."""
    with pytest.raises(ProviderAuthError, match="must not be empty"):
        StockProvider(api_key="", cache=cache)
    with pytest.raises(ProviderAuthError, match="must not be empty"):
        StockProvider(api_key="   ", cache=cache)


@respx.mock
def test_submit_success_and_orientation_params(cache: ClipCache) -> None:
    """Verify submit sends Authorization header, query params, and returns stock-hex16 ID."""
    api_key = "test-pexels-key-12345"
    provider = StockProvider(api_key=api_key, cache=cache)

    payload = _make_pexels_response(
        [
            {
                "id": 101,
                "video_files": [
                    {
                        "file_type": "video/mp4",
                        "width": 1080,
                        "height": 1920,
                        "link": "https://videos.pexels.com/101-portrait.mp4",
                    }
                ],
            }
        ]
    )

    route = respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))

    job_id = provider.submit("futuristic city", Aspect.PORTRAIT, seconds=15, seed=42)

    assert route.called
    req = route.calls.last.request
    assert req.headers.get("authorization") == api_key
    assert req.url.params["query"] == "futuristic city"
    assert req.url.params["per_page"] == "10"
    assert req.url.params["orientation"] == "portrait"

    # Job ID must be stock-{hex16}
    assert job_id.startswith("stock-")
    assert len(job_id) == 22  # "stock-" (6) + 16 hex chars
    hex_part = job_id[6:]
    assert all(c in "0123456789abcdef" for c in hex_part)

    # Job record must be written to cache
    record = cache.read_job_record("stock", job_id)
    assert record is not None
    assert record["url"] == "https://videos.pexels.com/101-portrait.mp4"
    assert record["width"] == 1080
    assert record["height"] == 1920

    provider.close()


@respx.mock
def test_submit_nearest_height_and_larger_width_tiebreaker(cache: ClipCache) -> None:
    """Verify candidate selection picks nearest target height and breaks ties with larger width."""
    provider = StockProvider(api_key="test-key", cache=cache)
    # Aspect.LANDSCAPE: 1920 x 1080 -> target height = 1080

    payload = _make_pexels_response(
        [
            {
                "id": 201,
                "video_files": [
                    # diff |720 - 1080| = 360
                    {
                        "file_type": "video/mp4",
                        "width": 1280,
                        "height": 720,
                        "link": "https://videos.pexels.com/720p.mp4",
                    },
                    # diff |1080 - 1080| = 0, width = 1080
                    {
                        "file_type": "video/mp4",
                        "width": 1080,
                        "height": 1080,
                        "link": "https://videos.pexels.com/1080-square.mp4",
                    },
                    # diff |1080 - 1080| = 0, width = 1920 -> wins tie over 1080 width!
                    {
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "link": "https://videos.pexels.com/1080-wide.mp4",
                    },
                    # diff |2160 - 1080| = 1080
                    {
                        "file_type": "video/mp4",
                        "width": 3840,
                        "height": 2160,
                        "link": "https://videos.pexels.com/4k.mp4",
                    },
                ],
            }
        ]
    )

    respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))

    job_id = provider.submit("nature", Aspect.LANDSCAPE, seconds=8)
    record = cache.read_job_record("stock", job_id)
    assert record is not None
    assert record["url"] == "https://videos.pexels.com/1080-wide.mp4"
    assert record["width"] == 1920
    assert record["height"] == 1080

    provider.close()


@respx.mock
def test_submit_filters_invalid_and_non_mp4_files(cache: ClipCache) -> None:
    """Verify non-mp4 files and invalid/zero dimension files are excluded."""
    provider = StockProvider(api_key="test-key", cache=cache)

    payload = _make_pexels_response(
        [
            {
                "id": 301,
                "video_files": [
                    # WebM format -> skip
                    {
                        "file_type": "video/webm",
                        "width": 1920,
                        "height": 1080,
                        "link": "https://videos.pexels.com/clip.webm",
                    },
                    # Zero dimensions -> skip
                    {
                        "file_type": "video/mp4",
                        "width": 0,
                        "height": 0,
                        "link": "https://videos.pexels.com/zero.mp4",
                    },
                    # Missing link -> skip
                    {
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "link": "",
                    },
                    # Valid MP4
                    {
                        "file_type": "video/mp4",
                        "width": 1280,
                        "height": 720,
                        "link": "https://videos.pexels.com/valid.mp4",
                    },
                ],
            }
        ]
    )

    respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))

    job_id = provider.submit("test", Aspect.LANDSCAPE, seconds=5)
    record = cache.read_job_record("stock", job_id)
    assert record is not None
    assert record["url"] == "https://videos.pexels.com/valid.mp4"
    provider.close()


@respx.mock
def test_submit_empty_results_raises_provider_error(cache: ClipCache) -> None:
    """Verify empty videos list or no mp4 files raises ProviderError."""
    provider = StockProvider(api_key="test-key", cache=cache)

    respx.get(PEXELS_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_make_pexels_response([]))
    )

    with pytest.raises(ProviderError, match="No stock videos found"):
        provider.submit("rare search query", Aspect.LANDSCAPE, seconds=10)

    # Videos exist but have no mp4 files
    respx.get(PEXELS_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_make_pexels_response([{"id": 1, "video_files": []}]),
        )
    )

    with pytest.raises(ProviderError, match="No suitable MP4 video files found"):
        provider.submit("rare search query", Aspect.LANDSCAPE, seconds=10)

    provider.close()


def test_submit_empty_prompt_raises(cache: ClipCache) -> None:
    """Verify empty prompt raises ProviderError."""
    provider = StockProvider(api_key="test-key", cache=cache)
    with pytest.raises(ProviderError, match="Prompt must not be empty"):
        provider.submit("", Aspect.LANDSCAPE, seconds=10)
    with pytest.raises(ProviderError, match="Prompt must not be empty"):
        provider.submit("   ", Aspect.LANDSCAPE, seconds=10)
    provider.close()


@respx.mock
def test_submit_http_error_mappings(cache: ClipCache) -> None:
    """Verify HTTP status codes 401, 429, 500, and timeout map to appropriate exceptions."""
    provider = StockProvider(api_key="test-key", cache=cache)

    # 401 Unauthorized -> ProviderAuthError
    respx.get(PEXELS_SEARCH_URL).mock(
        return_value=httpx.Response(401, text="Unauthorized: Invalid key")
    )
    with pytest.raises(ProviderAuthError, match="Pexels auth failed"):
        provider.submit("test", Aspect.PORTRAIT, seconds=5)

    # 429 Rate Limit -> ProviderQuotaError
    respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(429, text="Rate limit exceeded"))
    with pytest.raises(ProviderQuotaError, match="Pexels rate limit exceeded"):
        provider.submit("test", Aspect.PORTRAIT, seconds=5)

    # 500 Server Error -> ProviderError
    respx.get(PEXELS_SEARCH_URL).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(ProviderError, match="Pexels API error"):
        provider.submit("test", Aspect.PORTRAIT, seconds=5)

    # Timeout -> ProviderTimeoutError
    respx.get(PEXELS_SEARCH_URL).mock(side_effect=httpx.TimeoutException("Read timed out"))
    with pytest.raises(ProviderTimeoutError, match="timed out"):
        provider.submit("test", Aspect.PORTRAIT, seconds=5)

    provider.close()


def test_poll_states(cache: ClipCache) -> None:
    """Verify poll behavior for succeeded, missing, and invalid job IDs."""
    provider = StockProvider(api_key="test-key", cache=cache)

    # Write a known record directly to cache
    job_id = "stock-0123456789abcdef"
    cache.write_job_record(
        "stock",
        job_id,
        {"url": "https://videos.pexels.com/clip.mp4", "width": 1920, "height": 1080},
    )

    status = provider.poll(job_id)
    assert status.state == JobState.SUCCEEDED
    assert status.job_id == job_id
    assert status.error is None

    # Missing record in cache returns FAILED state
    missing_id = "stock-ffffffffffffffff"
    status_missing = provider.poll(missing_id)
    assert status_missing.state == JobState.FAILED
    assert status_missing.job_id == missing_id
    assert "not found" in (status_missing.error or "")

    # Invalid job ID format raises ProviderError
    with pytest.raises(ProviderError, match="Invalid job ID"):
        provider.poll("fake-p-0-8-12345678")

    provider.close()


@respx.mock
def test_download_success_and_process_restart(cache: ClipCache, tmp_path: Path) -> None:
    """Verify download succeeds via net.download_url and works across provider instances."""
    api_key = "test-key"
    provider1 = StockProvider(api_key=api_key, cache=cache)

    # Submit job using provider1
    video_url = "https://videos.pexels.com/video-files/test.mp4"
    payload = _make_pexels_response(
        [
            {
                "id": 501,
                "video_files": [
                    {
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "link": video_url,
                    }
                ],
            }
        ]
    )
    respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    job_id = provider1.submit("test query", Aspect.LANDSCAPE, seconds=10)
    provider1.close()

    # Create fresh provider2 instance (simulating process restart)
    provider2 = StockProvider(api_key=api_key, cache=cache)

    # Mock download URL
    respx.get(video_url).mock(return_value=httpx.Response(200, content=b"stock-video-bytes-123"))

    dest = tmp_path / "downloads" / "scene_001.mp4"
    result_path = provider2.download(job_id, dest)

    assert result_path == dest
    assert dest.is_file()
    assert dest.read_bytes() == b"stock-video-bytes-123"

    provider2.close()


def test_download_missing_record_or_invalid_id(cache: ClipCache, tmp_path: Path) -> None:
    """Verify download raises ProviderError when job record is missing or ID is invalid."""
    provider = StockProvider(api_key="test-key", cache=cache)
    dest = tmp_path / "clip.mp4"

    with pytest.raises(ProviderError, match="Job record not found"):
        provider.download("stock-0000000000000000", dest)

    with pytest.raises(ProviderError, match="Invalid job ID"):
        provider.download("invalid-id", dest)

    provider.close()


def test_download_forbidden_host_raises(cache: ClipCache, tmp_path: Path) -> None:
    """Verify downloading from an unauthorized domain raises ProviderError."""
    provider = StockProvider(api_key="test-key", cache=cache)
    dest = tmp_path / "clip.mp4"

    job_id = "stock-1111222233334444"
    cache.write_job_record(
        "stock",
        job_id,
        {"url": "https://unauthorized-evil.com/video.mp4", "width": 1920, "height": 1080},
    )

    with pytest.raises(ProviderError, match="Host not allowed"):
        provider.download(job_id, dest)

    provider.close()


@respx.mock
def test_api_key_never_in_caplog(
    cache: ClipCache,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Assert Pexels API key never appears in caplog during submit, errors, or download."""
    secret_key = "PEXELS_SUPER_SECRET_KEY_9876543210"
    provider = StockProvider(api_key=secret_key, cache=cache)

    video_url = "https://videos.pexels.com/clip.mp4"
    payload = _make_pexels_response(
        [
            {
                "id": 701,
                "video_files": [
                    {
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "link": video_url,
                    }
                ],
            }
        ]
    )

    # 1. Normal submit and download
    respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    respx.get(video_url).mock(return_value=httpx.Response(200, content=b"clip-bytes"))

    with caplog.at_level(logging.DEBUG):
        job_id = provider.submit("ocean waves", Aspect.LANDSCAPE, seconds=10)
        provider.download(job_id, tmp_path / "ocean.mp4")

    assert secret_key not in caplog.text

    # 2. Error response echoing the secret key in response body
    caplog.clear()
    error_body = f"Error: Invalid credentials for key {secret_key}"
    respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(401, text=error_body))

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderAuthError):
        provider.submit("ocean waves", Aspect.LANDSCAPE, seconds=10)

    assert secret_key not in caplog.text

    provider.close()


@respx.mock
def test_error_body_tail_truncated_and_redacted(cache: ClipCache) -> None:
    """Verify error body tail is truncated to <= 200 chars and secrets are redacted."""
    secret_key = "PEXELS_SECRET_TOKEN_XYZ"
    provider = StockProvider(api_key=secret_key, cache=cache)

    # Construct response > 300 characters containing the secret key near the end
    long_prefix = "A" * 300
    long_body = f"{long_prefix} Details: api_key={secret_key} token={secret_key} end."

    respx.get(PEXELS_SEARCH_URL).mock(return_value=httpx.Response(500, text=long_body))

    with pytest.raises(ProviderError) as exc_info:
        provider.submit("test", Aspect.LANDSCAPE, seconds=5)

    msg = str(exc_info.value)
    assert secret_key not in msg
    assert "***" in msg

    # Body tail in exception message should not exceed 200 characters
    prefix_text = "Pexels API error (500): "
    assert prefix_text in msg
    body_part = msg.split(prefix_text)[1]
    assert len(body_part) <= 200

    provider.close()
