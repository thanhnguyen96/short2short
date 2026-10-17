from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx

from short2short.errors import ProviderError
from short2short.net import download_url, host_allowed


def test_host_allowed() -> None:
    allow = {"videos.pexels.com", "storage.googleapis.com"}
    assert host_allowed("https://videos.pexels.com/v1/test.mp4", allow) is True
    assert host_allowed("https://STORAGE.GOOGLEAPIS.COM/clip.mp4", allow) is True

    # Reject http (non-https)
    assert host_allowed("http://videos.pexels.com/v1/test.mp4", allow) is False

    # Reject userinfo
    assert host_allowed("https://user:pass@videos.pexels.com/v1/test.mp4", allow) is False

    # Reject unknown host
    assert host_allowed("https://evil.com/clip.mp4", allow) is False
    assert host_allowed("not a url", allow) is False


@respx.mock
def test_download_url_allowed_redirect_writes(tmp_path: Path) -> None:
    allow = {"videos.pexels.com", "storage.googleapis.com"}
    dest = tmp_path / "video.mp4"

    respx.get("https://videos.pexels.com/initial").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://storage.googleapis.com/final.mp4"}
        )
    )
    respx.get("https://storage.googleapis.com/final.mp4").mock(
        return_value=httpx.Response(200, content=b"downloaded-mp4-content")
    )

    download_url("https://videos.pexels.com/initial", dest, allow_hosts=allow, timeout=10.0)

    assert dest.is_file()
    assert dest.read_bytes() == b"downloaded-mp4-content"


@respx.mock
def test_download_url_redirect_to_foreign_host_raises(tmp_path: Path) -> None:
    allow = {"videos.pexels.com"}
    dest = tmp_path / "video.mp4"

    respx.get("https://videos.pexels.com/redirect").mock(
        return_value=httpx.Response(302, headers={"Location": "https://malicious.com/bad.mp4"})
    )

    with pytest.raises(ProviderError, match="Redirect host not allowed"):
        download_url("https://videos.pexels.com/redirect", dest, allow_hosts=allow, timeout=10.0)


@respx.mock
def test_download_url_query_string_not_in_caplog(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    allow = {"videos.pexels.com"}
    dest = tmp_path / "video.mp4"
    url = "https://videos.pexels.com/clip.mp4?secret_token=SUPER_SECRET_123&user=42"

    respx.get("https://videos.pexels.com/clip.mp4").mock(
        return_value=httpx.Response(200, content=b"test-clip")
    )

    with caplog.at_level(logging.INFO):
        download_url(url, dest, allow_hosts=allow, timeout=10.0)

    assert "SUPER_SECRET_123" not in caplog.text
    assert "secret_token" not in caplog.text
    assert "https://videos.pexels.com/clip.mp4" in caplog.text
