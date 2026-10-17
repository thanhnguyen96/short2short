from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from short2short.errors import PathSecurityError


class Aspect(StrEnum):
    """Video aspect ratios."""

    PORTRAIT = "9:16"
    LANDSCAPE = "16:9"
    SQUARE = "1:1"


class SceneState(StrEnum):
    """Lifecycle state of an individual scene."""

    PENDING = "pending"
    VOICED = "voiced"
    SUBMITTED = "submitted"
    DOWNLOADED = "downloaded"
    FAILED_POLICY = "failed_policy"
    FAILED = "failed"
    SKIPPED_BUDGET = "skipped_budget"


class Stage(StrEnum):
    """Pipeline progression stages."""

    SCRIPT = "script"
    VOICE = "voice"
    SUBTITLE = "subtitle"
    CLIPS = "clips"
    COMPOSE = "compose"
    DONE = "done"


class TaskStatus(StrEnum):
    """Overall task execution status."""

    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"


class JobState(StrEnum):
    """Status of asynchronous provider generation jobs."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def aspect_size(aspect: Aspect) -> tuple[int, int]:
    """Return (width, height) pixel dimensions for given aspect ratio."""
    if aspect == Aspect.PORTRAIT:
        return (1080, 1920)
    elif aspect == Aspect.LANDSCAPE:
        return (1920, 1080)
    elif aspect == Aspect.SQUARE:
        return (1080, 1080)
    raise ValueError(f"Unknown aspect ratio: {aspect!r}")


class WordCue(BaseModel):
    """Individual word timing cue from TTS."""

    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)


class VoiceResult(BaseModel):
    """Result of TTS voice synthesis."""

    path: Path
    duration_seconds: float = Field(gt=0)
    cues: list[WordCue] = Field(default_factory=list)


class Scene(BaseModel):
    """Scene definition including script, visual prompt, and execution state."""

    index: int = Field(ge=0)
    narration: str = Field(min_length=1)
    visual_prompt: str = Field(min_length=1)
    state: SceneState = SceneState.PENDING
    duration_seconds: float | None = None
    audio_relpath: str | None = None
    word_cues: list[WordCue] = Field(default_factory=list)
    job_id: str | None = None
    clip_relpath: str | None = None
    cache_key: str | None = None
    seed: int | None = None
    requested_seconds: int | None = None
    provider_meta: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = Field(default=0, ge=0)


class Script(BaseModel):
    """Full script comprising topic, language, and ordered scenes."""

    topic: str = Field(min_length=1)
    lang: str = Field(min_length=1)
    scenes: list[Scene] = Field(min_length=1)


class JobStatus(BaseModel):
    """Status update for an asynchronous generation job."""

    state: JobState
    job_id: str
    error: str | None = None


class Budget(BaseModel):
    """Budget limits for clip count and total duration."""

    max_clips: int = Field(default=12, ge=1)
    max_total_seconds: float = Field(default=120.0, gt=0)


class TaskManifest(BaseModel):
    """Persistent state manifest for a video generation task."""

    version: int = 1
    task_id: str
    topic: str
    provider: str
    aspect: Aspect
    lang: str
    seed: int
    voice: str
    status: TaskStatus
    stage: Stage
    created_at: datetime
    updated_at: datetime
    script: Script | None = None
    scenes: list[Scene] = Field(default_factory=list)
    subtitle_relpath: str | None = None
    narration_relpath: str | None = None
    final_relpath: str | None = None
    error: str | None = None
    stop_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    bgm_path: str | None = None
    fit_mode: str = "cover"
    jobs_submitted: int = Field(default=0, ge=0)


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def new_task_id(now: datetime | None = None) -> str:
    """Generate a unique task ID using UTC timestamp and random hex suffix."""
    if now is None:
        now = datetime.now(UTC)
    return f"{now.strftime('%Y%m%d-%H%M%S-')}{uuid.uuid4().hex[:6]}"


def validate_task_id(task_id: str) -> str:
    """Validate task_id against regex or raise PathSecurityError."""
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise PathSecurityError(f"Invalid task_id: {task_id!r}")
    return task_id
