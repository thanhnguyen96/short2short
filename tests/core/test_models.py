from __future__ import annotations

from datetime import UTC, datetime

import pytest

from short2short.core.models import (
    TASK_ID_RE,
    Aspect,
    Budget,
    JobState,
    JobStatus,
    Scene,
    SceneState,
    Script,
    Stage,
    TaskManifest,
    TaskStatus,
    WordCue,
    aspect_size,
    new_task_id,
    validate_task_id,
)
from short2short.errors import PathSecurityError


def test_aspect_size() -> None:
    assert aspect_size(Aspect.PORTRAIT) == (1080, 1920)
    assert aspect_size(Aspect.LANDSCAPE) == (1920, 1080)
    assert aspect_size(Aspect.SQUARE) == (1080, 1080)


def test_new_task_id_and_validation() -> None:
    fixed_time = datetime(2026, 10, 17, 14, 30, 0, tzinfo=UTC)
    tid = new_task_id(fixed_time)
    assert tid.startswith("20261017-143000-")
    assert TASK_ID_RE.fullmatch(tid) is not None
    assert validate_task_id(tid) == tid

    # Dynamic task id
    rand_tid = new_task_id()
    assert TASK_ID_RE.fullmatch(rand_tid) is not None

    # Invalid task id raises PathSecurityError
    with pytest.raises(PathSecurityError):
        validate_task_id("../escape")

    with pytest.raises(PathSecurityError):
        validate_task_id("invalid/char")

    with pytest.raises(PathSecurityError):
        validate_task_id("")


def test_models_manifest_round_trip() -> None:
    cue = WordCue(text="hello", start=0.0, end=0.5)
    scene = Scene(
        index=0,
        narration="Hello world",
        visual_prompt="A sunny day",
        state=SceneState.PENDING,
        word_cues=[cue],
    )
    script = Script(topic="Greetings", lang="en", scenes=[scene])

    now = datetime.now(UTC)
    manifest = TaskManifest(
        task_id="20261017-120000-abcdef",
        topic="Greetings",
        provider="fake",
        aspect=Aspect.PORTRAIT,
        lang="en",
        seed=42,
        voice="en-US-AnaNeural",
        status=TaskStatus.RUNNING,
        stage=Stage.SCRIPT,
        created_at=now,
        updated_at=now,
        script=script,
        scenes=[scene],
        budget=Budget(max_clips=5, max_total_seconds=60.0),
        jobs_submitted=0,
    )

    json_str = manifest.model_dump_json()
    loaded = TaskManifest.model_validate_json(json_str)
    assert loaded.task_id == manifest.task_id
    assert loaded.jobs_submitted == 0
    assert loaded.aspect == Aspect.PORTRAIT
    assert len(loaded.scenes) == 1
    assert loaded.scenes[0].narration == "Hello world"


def test_job_status() -> None:
    js = JobStatus(state=JobState.SUCCEEDED, job_id="job-123")
    assert js.state == JobState.SUCCEEDED
    assert js.error is None
