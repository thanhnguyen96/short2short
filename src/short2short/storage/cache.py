from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from short2short.errors import PathSecurityError
from short2short.security import safe_join

logger = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class ClipCache:
    """Local cache for generated video clips and provider job records."""

    def __init__(self, root: Path) -> None:
        """Initialize clip cache at root directory."""
        self.root: Path = root.resolve()

    @staticmethod
    def make_key(provider: str, prompt: str, aspect: str, seed: int | None, seconds: int) -> str:
        """Generate SHA256 hex cache key from clip parameters."""
        seed_str = "" if seed is None else str(seed)
        raw = f"{provider}|{prompt}|{aspect}|{seed_str}|{seconds}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Path | None:
        """Retrieve cached clip path if key is valid and file exists non-empty."""
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            return None
        p = safe_join(self.root, key[:2], f"{key}.mp4")
        if p.is_file() and p.stat().st_size > 0:
            return p
        return None

    def put(self, key: str, src: Path) -> Path:
        """Copy clip file into cache atomically."""
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError(f"Invalid cache key: {key!r}")
        dest = safe_join(self.root, key[:2], f"{key}.mp4")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(f"{key}.mp4.part.{uuid.uuid4().hex[:8]}")
        try:
            shutil.copyfile(src, part)
            os.replace(part, dest)
            return dest
        finally:
            if part.exists():
                with contextlib.suppress(OSError):
                    part.unlink()

    def job_record_path(self, provider: str, job_id: str) -> Path:
        """Return path for job record json, replacing slashes/colons safely."""
        clean_id = job_id.replace("/", "_")
        if not JOB_ID_RE.fullmatch(clean_id):
            raise PathSecurityError(f"Invalid job_id format: {job_id!r}")
        safe_id = clean_id.replace(":", "_")
        return safe_join(self.root, "jobs", provider, f"{safe_id}.json")

    def write_job_record(self, provider: str, job_id: str, data: dict[str, Any]) -> None:
        """Write job record dictionary to JSON file atomically."""
        p = self.job_record_path(provider, job_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()

    def read_job_record(self, provider: str, job_id: str) -> dict[str, Any] | None:
        """Read job record dictionary from JSON file if it exists."""
        try:
            p = self.job_record_path(provider, job_id)
        except Exception:
            return None
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
