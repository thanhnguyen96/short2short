from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from short2short.core.models import TASK_ID_RE, TaskManifest, validate_task_id
from short2short.errors import PipelineError
from short2short.security import safe_join

logger = logging.getLogger(__name__)


class TaskStore:
    """Storage manager for task workspaces and manifests."""

    def __init__(self, root: Path) -> None:
        """Initialize task store at the specified root directory."""
        self.root: Path = root.resolve()
        self._store_lock = threading.Lock()
        self._task_locks: dict[str, threading.RLock] = {}

    def _get_lock(self, task_id: str) -> threading.RLock:
        with self._store_lock:
            if task_id not in self._task_locks:
                self._task_locks[task_id] = threading.RLock()
            return self._task_locks[task_id]

    def task_dir(self, task_id: str) -> Path:
        """Return directory path for task_id, validating against path traversal."""
        validate_task_id(task_id)
        return safe_join(self.root, task_id)

    def create(self, manifest: TaskManifest) -> Path:
        """Create new task directory and manifest.json. Raise PipelineError if it already exists."""
        tdir = self.task_dir(manifest.task_id)
        manifest_file = safe_join(tdir, "manifest.json")
        lock = self._get_lock(manifest.task_id)
        with lock:
            if manifest_file.exists():
                raise PipelineError(f"Task already exists: {manifest.task_id}")
            tdir.mkdir(parents=True, exist_ok=True)
            self._write_manifest(manifest_file, manifest)
        return tdir

    def load(self, task_id: str) -> TaskManifest:
        """Load manifest for task_id. Raise PipelineError if not found."""
        tdir = self.task_dir(task_id)
        manifest_file = safe_join(tdir, "manifest.json")
        if not manifest_file.is_file():
            raise PipelineError(f"Task manifest not found: {task_id}")
        lock = self._get_lock(task_id)
        with lock:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            return TaskManifest.model_validate(data)

    def save(self, manifest: TaskManifest) -> None:
        """Save manifest with updated_at timestamp set to now UTC."""
        tdir = self.task_dir(manifest.task_id)
        manifest_file = safe_join(tdir, "manifest.json")
        lock = self._get_lock(manifest.task_id)
        with lock:
            manifest.updated_at = datetime.now(UTC)
            tdir.mkdir(parents=True, exist_ok=True)
            self._write_manifest(manifest_file, manifest)

    def _write_manifest(self, path: Path, manifest: TaskManifest) -> None:
        tmp_path = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            tmp_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def update(self, task_id: str, mutator: Callable[[TaskManifest], None]) -> TaskManifest:
        """Atomically load, mutate, and save task manifest under task lock."""
        lock = self._get_lock(task_id)
        with lock:
            manifest = self.load(task_id)
            mutator(manifest)
            self.save(manifest)
            return manifest

    def list_ids(self) -> list[str]:
        """Return sorted list of valid task IDs containing a manifest.json."""
        if not self.root.is_dir():
            return []
        ids: list[str] = []
        for entry in self.root.iterdir():
            if (
                entry.is_dir()
                and TASK_ID_RE.fullmatch(entry.name)
                and (entry / "manifest.json").is_file()
            ):
                ids.append(entry.name)
        return sorted(ids)
