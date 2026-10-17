from __future__ import annotations

import contextlib
import logging
import os
import urllib.parse
from pathlib import Path

import httpx

from short2short.errors import ProviderError

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def host_allowed(url: str, allow_hosts: set[str]) -> bool:
    """Check if URL is https, contains no userinfo, and hostname is in allow_hosts."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False

    if parsed.scheme.lower() != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if not parsed.hostname:
        return False

    return parsed.hostname.lower() in {h.lower() for h in allow_hosts}


def _write_stream_to_dest(response: httpx.Response, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_file = dest.with_name(f"{dest.name}.part")
    try:
        with open(part_file, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=65536):
                f.write(chunk)
        os.replace(part_file, dest)
    finally:
        if part_file.exists():
            with contextlib.suppress(OSError):
                part_file.unlink()


def download_url(
    url: str,
    dest: Path,
    *,
    allow_hosts: set[str],
    timeout: float,
    client: httpx.Client | None = None,
) -> None:
    """Stream GET request, following at most one redirect if host is allowed.

    First URL and redirect target must pass host_allowed.
    Logs only scheme://host/path without query parameters.
    Writes to dest.part then atomic replaces to dest.
    """
    if not host_allowed(url, allow_hosts):
        raise ProviderError(f"Host not allowed for download: {url}")

    parsed = urllib.parse.urlsplit(url)
    sanitized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    logger.info("Downloading %s to %s", sanitized_url, dest)

    own_client = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=False)
        own_client = True

    try:
        with client.stream("GET", url) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise ProviderError(
                        f"Redirect response missing Location header from {sanitized_url}"
                    )
                redirect_url = urllib.parse.urljoin(url, location)
                if not host_allowed(redirect_url, allow_hosts):
                    raise ProviderError(f"Redirect host not allowed: {redirect_url}")

                parsed_redir = urllib.parse.urlsplit(redirect_url)
                sanitized_redir = (
                    f"{parsed_redir.scheme}://{parsed_redir.netloc}{parsed_redir.path}"
                )
                logger.info("Following redirect to %s", sanitized_redir)

                with client.stream("GET", redirect_url) as redir_resp:
                    if redir_resp.status_code != 200:
                        raise ProviderError(
                            f"HTTP {redir_resp.status_code} downloading {sanitized_redir}"
                        )
                    _write_stream_to_dest(redir_resp, dest)
                return

            if resp.status_code != 200:
                raise ProviderError(f"HTTP {resp.status_code} downloading {sanitized_url}")

            _write_stream_to_dest(resp, dest)
    finally:
        if own_client:
            client.close()
