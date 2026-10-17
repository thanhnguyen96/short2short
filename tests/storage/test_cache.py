from __future__ import annotations

from pathlib import Path

import pytest

from short2short.errors import PathSecurityError
from short2short.storage.cache import ClipCache


def test_make_key_stability_and_seed_none_vs_zero() -> None:
    k1 = ClipCache.make_key("fake", "test prompt", "9:16", None, 5)
    k2 = ClipCache.make_key("fake", "test prompt", "9:16", None, 5)
    assert k1 == k2

    k_zero = ClipCache.make_key("fake", "test prompt", "9:16", 0, 5)
    assert k1 != k_zero


def test_clip_cache_get_and_put(tmp_path: Path) -> None:
    cache = ClipCache(tmp_path)
    key = ClipCache.make_key("fake", "prompt", "9:16", 1, 5)

    # Empty cache returns None
    assert cache.get(key) is None
    assert cache.get("bad-key") is None

    # Put clip
    src = tmp_path / "source.mp4"
    src.write_bytes(b"dummy video data")
    cached_path = cache.put(key, src)

    assert cached_path.is_file()
    assert cached_path.read_bytes() == b"dummy video data"

    # Get cached clip
    retrieved = cache.get(key)
    assert retrieved == cached_path

    # Invalid key raises ValueError on put
    with pytest.raises(ValueError, match="Invalid cache key"):
        cache.put("short-key", src)


def test_job_record_round_trip_and_safe_filename(tmp_path: Path) -> None:
    cache = ClipCache(tmp_path)

    # Path mapping converts / and : to _
    job_path = cache.job_record_path("flow", "a/b:c")
    assert job_path.name == "a_b_c.json"
    assert ":" not in job_path.name
    assert "/" not in job_path.name

    # Write and read record
    payload = {"project_id": "proj-123", "status": "pending"}
    cache.write_job_record("flow", "a/b:c", payload)

    loaded = cache.read_job_record("flow", "a/b:c")
    assert loaded == payload

    # Nonexistent job record returns None
    assert cache.read_job_record("flow", "nonexistent") is None

    # Invalid job_id raises PathSecurityError
    with pytest.raises(PathSecurityError):
        cache.job_record_path("flow", "invalid?char*")
