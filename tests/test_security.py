from __future__ import annotations

from pathlib import Path

import pytest

from short2short.errors import PathSecurityError
from short2short.security import assert_loopback_host, redact, safe_join


def test_redact_secrets() -> None:
    assert redact("api_key=sk-abc") == "api_key=***"
    assert redact("Bearer ya29.zzz") == "Bearer ***"
    assert redact("Cookie: SID=qqq") == "Cookie: ***"
    assert (
        redact("https://example.com/v1?signature=deadbeef&user=1")
        == "https://example.com/v1?signature=***&user=1"
    )
    assert redact("signature=deadbeef in URL") == "signature=*** in URL"
    assert redact("Authorization: Bearer my-secret-token") == "Authorization: ***"
    assert redact("sk-1234567890abcdef") == "***"
    assert redact("ya29.abcdef12345") == "***"


def test_safe_join_ok(tmp_path: Path) -> None:
    res = safe_join(tmp_path, "sub", "file.txt")
    assert res == (tmp_path / "sub" / "file.txt").resolve()


def test_safe_join_traversal_escape(tmp_path: Path) -> None:
    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "..", "escape.txt")

    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "sub", "..", "..", "escape.txt")


def test_safe_join_absolute_or_drive_part(tmp_path: Path) -> None:
    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "/absolute/path")

    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "C:\\absolute\\path")


def test_safe_join_empty_part(tmp_path: Path) -> None:
    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "")

    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "   ")


def test_assert_loopback_host() -> None:
    assert_loopback_host("127.0.0.1")

    with pytest.raises(PathSecurityError):
        assert_loopback_host("localhost")

    with pytest.raises(PathSecurityError):
        assert_loopback_host("0.0.0.0")

    with pytest.raises(PathSecurityError):
        assert_loopback_host("192.168.1.1")
