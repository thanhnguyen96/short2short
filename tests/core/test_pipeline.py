from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fakes import FakeScript, FakeTTS, MemoryProvider
from short2short.config import AppConfig
from short2short.core.models import Aspect, Budget, SceneState, Stage, TaskStatus
from short2short.core.pipeline import regen_scene, resume_task, run_task
from short2short.errors import BudgetExceededError, PipelineError
from short2short.storage.cache import ClipCache
from short2short.storage.tasks import TaskStore


def fake_runner(argv: list[str]) -> None:
    out = Path(argv[-1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.touch()


@pytest.fixture(autouse=True)
def mock_compose_task(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_compose(**kwargs: Any) -> Path:
        td: Path = kwargs["task_dir"]
        final_mp4 = td / "final.mp4"
        final_mp4.touch()
        return final_mp4

    monkeypatch.setattr("short2short.core.compose.compose_task", fake_compose)


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks")


@pytest.fixture
def cache(tmp_path: Path) -> ClipCache:
    return ClipCache(tmp_path / "cache")


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        tasks_dir=tmp_path / "tasks",
        cache_dir=tmp_path / "cache",
        poll_interval_seconds=0.0,
        poll_timeout_seconds=5.0,
    )


def test_happy_path_three_scenes(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    provider = MemoryProvider()
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="test topic",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=3,
        seed=42,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest.status == TaskStatus.COMPLETED
    assert manifest.stage == Stage.DONE
    assert manifest.jobs_submitted == 3
    assert len(manifest.scenes) == 3
    assert all(s.state == SceneState.DOWNLOADED for s in manifest.scenes)
    assert manifest.final_relpath == "final.mp4"
    assert manifest.narration_relpath == "audio/narration.mp3"
    assert manifest.subtitle_relpath == "subtitle.srt"


def test_resume_completed_no_submit(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    provider = MemoryProvider()
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="test resume completed",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=3,
        seed=42,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert len(provider.submits) == 3

    resumed = resume_task(
        manifest.task_id,
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert resumed.status == TaskStatus.COMPLETED
    assert len(provider.submits) == 3


def test_cache_shared_across_two_tasks(
    config: AppConfig, store: TaskStore, cache: ClipCache
) -> None:
    provider = MemoryProvider()
    manifest1 = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="shared topic",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=3,
        seed=100,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest1.jobs_submitted == 3
    assert len(provider.submits) == 3

    manifest2 = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="shared topic",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=3,
        seed=100,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest2.status == TaskStatus.COMPLETED
    assert manifest2.jobs_submitted == 0
    assert len(provider.submits) == 3
    assert all(s.state == SceneState.DOWNLOADED for s in manifest2.scenes)


def test_max_clips_one(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    provider = MemoryProvider()
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="budget max clips",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=3,
        budget=Budget(max_clips=1, max_total_seconds=120.0),
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest.status == TaskStatus.COMPLETED
    assert manifest.jobs_submitted == 1
    assert manifest.stop_reason == "budget"
    assert manifest.scenes[0].state == SceneState.DOWNLOADED
    assert manifest.scenes[1].state == SceneState.SKIPPED_BUDGET
    assert manifest.scenes[2].state == SceneState.SKIPPED_BUDGET


def test_max_total_seconds_skips_second(
    config: AppConfig, store: TaskStore, cache: ClipCache
) -> None:
    provider = MemoryProvider()
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="budget max seconds",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=3,
        budget=Budget(max_clips=10, max_total_seconds=2.5),
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest.status == TaskStatus.COMPLETED
    assert manifest.jobs_submitted == 1
    assert manifest.stop_reason == "budget"
    assert manifest.scenes[0].state == SceneState.DOWNLOADED
    assert manifest.scenes[1].state == SceneState.SKIPPED_BUDGET
    assert manifest.scenes[2].state == SceneState.SKIPPED_BUDGET


def test_policy_failure_at_zero(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    provider = MemoryProvider(fail_policy_at={0})
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="policy test",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=2,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest.status == TaskStatus.COMPLETED
    assert manifest.scenes[0].state == SceneState.FAILED_POLICY
    assert manifest.scenes[0].visual_prompt == "visual 0"
    assert manifest.scenes[1].state == SceneState.DOWNLOADED
    assert "scene 0 failed policy" in manifest.warnings
    assert len(provider.submits) == 2


def test_unconfirmed_at_zero_paid(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    provider = MemoryProvider(paid=True, unconfirmed_at={0})
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="unconfirmed test",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=2,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest.status == TaskStatus.PAUSED
    assert manifest.stop_reason == "unconfirmed"
    assert manifest.scenes[0].job_id == "job-0"
    assert manifest.scenes[0].state == SceneState.SUBMITTED
    assert len(provider.submits) == 1


def test_resume_after_unconfirmed(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    initial_provider = MemoryProvider(paid=True, unconfirmed_at={0})
    paused = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=initial_provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="resume after unconfirmed test",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=2,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert paused.status == TaskStatus.PAUSED
    assert len(initial_provider.submits) == 1

    resume_provider = MemoryProvider(paid=True)
    resumed = resume_task(
        paused.task_id,
        config=config,
        store=store,
        cache=cache,
        provider=resume_provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert resumed.status == TaskStatus.COMPLETED
    assert len(resume_provider.submits) == 1
    assert resumed.scenes[0].state == SceneState.DOWNLOADED
    assert resumed.scenes[1].state == SceneState.DOWNLOADED


def test_regen_scene_with_prompt(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    provider = MemoryProvider()
    initial = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="regen test",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=2,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert len(provider.submits) == 2

    regened = regen_scene(
        initial.task_id,
        0,
        prompt="brand new visual prompt",
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        tts=FakeTTS(),
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert regened.status == TaskStatus.COMPLETED
    assert regened.scenes[0].visual_prompt == "brand new visual prompt"
    assert len(provider.submits) == 3
    assert regened.scenes[0].state == SceneState.DOWNLOADED


def test_regen_scene_unknown_index_raises(
    config: AppConfig, store: TaskStore, cache: ClipCache
) -> None:
    provider = MemoryProvider()
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="regen unknown index",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=2,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    with pytest.raises(PipelineError):
        regen_scene(
            manifest.task_id,
            99,
            config=config,
            store=store,
            cache=cache,
            provider=provider,
            tts=FakeTTS(),
            probe=lambda p: 2.5,
            runner=fake_runner,
        )


def test_resume_unknown_task_raises(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    with pytest.raises(PipelineError):
        resume_task(
            "unknown-task-id",
            config=config,
            store=store,
            cache=cache,
            provider=MemoryProvider(),
            script_generator=FakeScript(),
            tts=FakeTTS(),
            probe=lambda p: 2.5,
            runner=fake_runner,
        )


def test_regen_scene_budget_exceeded_if_never_had_job(
    config: AppConfig, store: TaskStore, cache: ClipCache
) -> None:
    provider = MemoryProvider()
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="budget cap test",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=3,
        budget=Budget(max_clips=1, max_total_seconds=120.0),
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest.scenes[1].state == SceneState.SKIPPED_BUDGET
    with pytest.raises(BudgetExceededError):
        regen_scene(
            manifest.task_id,
            1,
            config=config,
            store=store,
            cache=cache,
            provider=provider,
            tts=FakeTTS(),
            probe=lambda p: 2.5,
            runner=fake_runner,
        )


def test_quota_error_pauses_task(config: AppConfig, store: TaskStore, cache: ClipCache) -> None:
    provider = MemoryProvider(quota_at={0})
    manifest = run_task(
        config=config,
        store=store,
        cache=cache,
        provider=provider,
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="quota test",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=2,
        voice="vi-VN-HoaiMyNeural",
        probe=lambda p: 2.5,
        runner=fake_runner,
    )
    assert manifest.status == TaskStatus.PAUSED
    assert manifest.stop_reason == "quota"


def test_unhandled_exception_records_failure(
    config: AppConfig, store: TaskStore, cache: ClipCache
) -> None:
    class FailingScript:
        def generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Script boom")

    with pytest.raises(RuntimeError):
        run_task(
            config=config,
            store=store,
            cache=cache,
            provider=MemoryProvider(),
            script_generator=FailingScript(),  # type: ignore[arg-type]
            tts=FakeTTS(),
            topic="fail test",
            aspect=Aspect.PORTRAIT,
            lang="vi",
            scene_count=2,
            voice="vi-VN-HoaiMyNeural",
            task_id="test-fail-task",
            probe=lambda p: 2.5,
            runner=fake_runner,
        )
    failed_m = store.load("test-fail-task")
    assert failed_m.status == TaskStatus.FAILED
    assert "Script boom" in (failed_m.error or "")


def test_paid_provider_downloads_each_clip_before_next_submit(
    tmp_path: Path, mock_compose_task: None
) -> None:
    events: list[str] = []

    class OrderedProvider(MemoryProvider):
        def submit(self, prompt: str, aspect: Aspect, seconds: int, seed: int | None) -> str:
            events.append(f"submit {prompt}")
            return super().submit(prompt, aspect, seconds, seed)

        def download(self, job_id: str, dest: Path) -> Path:
            events.append(f"download {job_id}")
            return super().download(job_id, dest)

    config = AppConfig(tasks_dir=tmp_path / "tasks", cache_dir=tmp_path / "cache")
    run_task(
        config=config,
        store=TaskStore(config.tasks_dir),
        cache=ClipCache(config.cache_dir),
        provider=OrderedProvider(paid=True),
        script_generator=FakeScript(),
        tts=FakeTTS(),
        topic="t",
        aspect=Aspect.PORTRAIT,
        lang="vi",
        scene_count=2,
        seed=1,
        voice="v",
        bgm_path=None,
        budget=Budget(max_clips=5, max_total_seconds=100),
        probe=lambda p: 2.5,
        runner=lambda argv: Path(argv[-1]).touch(),
    )
    assert events == ["submit visual 0", "download mem-0", "submit visual 1", "download mem-1"]
