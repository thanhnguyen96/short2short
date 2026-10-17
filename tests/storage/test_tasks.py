from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime
from pathlib import Path

import pytest

from short2short.core.models import Aspect, Stage, TaskManifest, TaskStatus
from short2short.errors import PathSecurityError, PipelineError
from short2short.storage.tasks import TaskStore


def _make_sample_manifest(task_id: str) -> TaskManifest:
    now = datetime.now(UTC)
    return TaskManifest(
        task_id=task_id,
        topic="Test Topic",
        provider="fake",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        seed=123,
        voice="vi-VN-HoaiMyNeural",
        status=TaskStatus.RUNNING,
        stage=Stage.SCRIPT,
        created_at=now,
        updated_at=now,
    )


def test_task_store_crud(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    manifest = _make_sample_manifest("task-001")

    # Create
    tdir = store.create(manifest)
    assert tdir.is_dir()
    assert (tdir / "manifest.json").is_file()

    # Re-create raises PipelineError
    with pytest.raises(PipelineError, match="already exists"):
        store.create(manifest)

    # Load
    loaded = store.load("task-001")
    assert loaded.task_id == "task-001"
    assert loaded.topic == "Test Topic"

    # Save
    loaded.status = TaskStatus.COMPLETED
    store.save(loaded)
    reloaded = store.load("task-001")
    assert reloaded.status == TaskStatus.COMPLETED

    # Missing load raises PipelineError
    with pytest.raises(PipelineError, match="not found"):
        store.load("task-999")


def test_task_store_list_ids(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    store.create(_make_sample_manifest("task-b"))
    store.create(_make_sample_manifest("task-a"))

    # Folder without manifest.json is ignored
    (tmp_path / "task-c").mkdir()

    ids = store.list_ids()
    assert ids == ["task-a", "task-b"]


def test_task_store_path_traversal_raises(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    with pytest.raises(PathSecurityError):
        store.task_dir("..")

    with pytest.raises(PathSecurityError):
        store.load("..")


def test_task_store_concurrent_updates(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    manifest = _make_sample_manifest("task-concurrent")
    store.create(manifest)

    def worker(val: str) -> None:
        store.update("task-concurrent", lambda m: m.warnings.append(val))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker, "warning-1")
        f2 = executor.submit(worker, "warning-2")
        f1.result()
        f2.result()

    final_manifest = store.load("task-concurrent")
    assert "warning-1" in final_manifest.warnings
    assert "warning-2" in final_manifest.warnings
    assert len(final_manifest.warnings) == 2
