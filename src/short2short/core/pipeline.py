from __future__ import annotations

import logging
import math
import secrets
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from short2short.config import AppConfig
from short2short.core import compose
from short2short.core.interfaces import TTS, ScriptGenerator
from short2short.core.models import (
    Aspect,
    Budget,
    JobState,
    JobStatus,
    Scene,
    SceneState,
    Stage,
    TaskManifest,
    TaskStatus,
    new_task_id,
    validate_task_id,
)
from short2short.core.subtitle import write_srt
from short2short.errors import (
    BudgetExceededError,
    PipelineError,
    ProviderError,
    ProviderPolicyError,
    ProviderQuotaError,
    ProviderUnconfirmedJobError,
    Short2ShortError,
)
from short2short.ffmpeg import Runner, default_runner, ffprobe_duration
from short2short.providers.base import VideoProvider
from short2short.security import redact, safe_join
from short2short.storage.cache import ClipCache
from short2short.storage.tasks import TaskStore

logger = logging.getLogger(__name__)


def _poll_job(provider: VideoProvider, job_id: str, config: AppConfig) -> JobStatus:
    """Poll provider job until completion or timeout."""
    deadline = time.monotonic() + getattr(
        provider, "poll_timeout_seconds", config.poll_timeout_seconds
    )
    interval = getattr(provider, "poll_interval_seconds", config.poll_interval_seconds)
    while True:
        status = provider.poll(job_id)
        if status.state == JobState.SUCCEEDED:
            return status
        if status.state == JobState.FAILED:
            raise ProviderError(status.error or "job failed", provider=provider.name)
        if time.monotonic() > deadline:
            raise ProviderUnconfirmedJobError(
                "poll timed out", job_id=job_id, provider=provider.name
            )
        if interval > 0:
            time.sleep(interval)


def _handle_provider_error(
    task_id: str, scene_idx: int, exc: Exception, paid: bool, store: TaskStore
) -> None:
    """Mutate task manifest state appropriately for specific provider errors."""

    def mut(m: TaskManifest) -> None:
        s = m.scenes[scene_idx]
        s.error = redact(str(exc))
        if isinstance(exc, ProviderPolicyError):
            s.state = SceneState.FAILED_POLICY
            m.warnings.append(f"scene {s.index} failed policy")
        elif isinstance(exc, ProviderUnconfirmedJobError):
            s.attempts += 1
            if exc.job_id:
                s.job_id = exc.job_id
            s.state = SceneState.SUBMITTED if s.job_id else SceneState.FAILED
            if not s.job_id:
                s.error = "unconfirmed"
            m.status, m.stop_reason = TaskStatus.PAUSED, "unconfirmed"
        elif isinstance(exc, ProviderQuotaError):
            s.attempts += 1
            s.state = SceneState.FAILED
            m.status, m.stop_reason = TaskStatus.PAUSED, "quota"
        elif isinstance(exc, ProviderError):
            s.attempts += 1
            s.state = SceneState.FAILED
            if paid:
                m.status, m.stop_reason = TaskStatus.PAUSED, "provider"
        else:
            raise exc

    store.update(task_id, mut)


def _skip_remaining_budget(manifest: TaskManifest, from_index: int, store: TaskStore) -> None:
    """Mark current and remaining unsubmitted/undownloaded scenes as SKIPPED_BUDGET."""
    found = False
    for s in manifest.scenes:
        if s.index == from_index:
            found = True
        if found and s.state not in (
            SceneState.DOWNLOADED,
            SceneState.SUBMITTED,
            SceneState.FAILED_POLICY,
        ):
            s.state = SceneState.SKIPPED_BUDGET
    manifest.stop_reason = "budget"
    store.save(manifest)


def _voice_scene(
    task_dir: Path, scene: Scene, voice: str, tts: TTS, probe: Callable[[Path], float]
) -> None:
    """Synthesize audio and probe duration for a scene if not already on disk."""
    audio_path = safe_join(task_dir, f"audio/scene_{scene.index:03d}.mp3")
    if audio_path.is_file() and audio_path.stat().st_size > 0:
        scene.audio_relpath = f"audio/scene_{scene.index:03d}.mp3"
        if scene.duration_seconds is None:
            try:
                scene.duration_seconds = probe(audio_path)
            except Short2ShortError as err:
                logger.warning("Probe failed for %s: %s", audio_path, err)
    else:
        res = tts.synthesize(scene.narration, dest=audio_path, voice=voice)
        scene.audio_relpath = f"audio/scene_{scene.index:03d}.mp3"
        scene.word_cues = res.cues
        try:
            scene.duration_seconds = probe(audio_path)
        except Short2ShortError as err:
            logger.warning("Probe failed for %s: %s; using TTS duration", audio_path, err)
            scene.duration_seconds = res.duration_seconds
    if scene.state == SceneState.PENDING:
        scene.state = SceneState.VOICED


def _poll_and_download_scene(
    task_id: str,
    scene_index: int,
    config: AppConfig,
    store: TaskStore,
    cache: ClipCache,
    provider: VideoProvider,
) -> None:
    """Poll provider and download clip for a submitted scene."""
    manifest = store.load(task_id)
    scene = manifest.scenes[scene_index]
    task_dir = store.task_dir(task_id)
    dur = scene.duration_seconds if scene.duration_seconds is not None else 1.0
    req_sec = scene.requested_seconds or max(1, min(math.ceil(dur), provider.max_clip_seconds))
    cache_key = scene.cache_key or ClipCache.make_key(
        provider.name, scene.visual_prompt, manifest.aspect.value, manifest.seed, req_sec
    )
    dest_clip = safe_join(task_dir, f"clips/scene_{scene.index:03d}.mp4")
    dest_clip.parent.mkdir(parents=True, exist_ok=True)
    try:
        assert scene.job_id is not None
        _poll_job(provider, scene.job_id, config)
        provider.download(scene.job_id, dest_clip)
        cache.put(cache_key, dest_clip)

        def mut_done(m: TaskManifest) -> None:
            s = m.scenes[scene_index]
            s.clip_relpath = f"clips/scene_{s.index:03d}.mp4"
            s.state, s.error = SceneState.DOWNLOADED, None
            s.cache_key, s.requested_seconds, s.seed = cache_key, req_sec, m.seed

        store.update(task_id, mut_done)
    except Exception as exc:
        _handle_provider_error(task_id, scene_index, exc, provider.paid, store)


def _process_clips(
    manifest: TaskManifest,
    config: AppConfig,
    store: TaskStore,
    cache: ClipCache,
    provider: VideoProvider,
) -> TaskManifest | None:
    """Run clips stage with cache checks, budget checks, and provider jobs."""
    task_dir = store.task_dir(manifest.task_id)
    safe_join(task_dir, "clips").mkdir(parents=True, exist_ok=True)

    for scene in manifest.scenes:
        dur = scene.duration_seconds if scene.duration_seconds is not None else 1.0
        req_sec = max(1, min(math.ceil(dur), provider.max_clip_seconds))
        scene.requested_seconds, scene.seed = req_sec, manifest.seed
        ckey = ClipCache.make_key(
            provider.name, scene.visual_prompt, manifest.aspect.value, manifest.seed, req_sec
        )
        scene.cache_key = ckey
        if (
            scene.state == SceneState.DOWNLOADED
            and scene.clip_relpath
            and safe_join(task_dir, scene.clip_relpath).is_file()
        ):
            continue
        if scene.state in (SceneState.FAILED_POLICY, SceneState.SKIPPED_BUDGET) or (
            scene.state == SceneState.FAILED and scene.attempts >= 2
        ):
            continue
        cached = cache.get(ckey)
        if cached is not None:
            dest = safe_join(task_dir, f"clips/scene_{scene.index:03d}.mp4")
            shutil.copyfile(cached, dest)
            scene.clip_relpath = f"clips/scene_{scene.index:03d}.mp4"
            scene.state, scene.error = SceneState.DOWNLOADED, None
            store.save(manifest)

    covered = sum(
        s.duration_seconds or 0.0
        for s in manifest.scenes
        if s.state in (SceneState.DOWNLOADED, SceneState.FAILED_POLICY)
    )
    scheduled: list[int] = []
    for scene in manifest.scenes:
        if scene.state in (
            SceneState.DOWNLOADED,
            SceneState.FAILED_POLICY,
            SceneState.SKIPPED_BUDGET,
        ) or (scene.state == SceneState.FAILED and scene.attempts >= 2):
            continue
        if scene.state == SceneState.SUBMITTED and scene.job_id:
            scheduled.append(scene.index)
            continue
        dur = scene.duration_seconds or 0.0
        if manifest.jobs_submitted + len(scheduled) >= manifest.budget.max_clips or (
            covered > 0 and (covered + dur) > manifest.budget.max_total_seconds
        ):
            _skip_remaining_budget(manifest, scene.index, store)
            break
        scheduled.append(scene.index)
        covered += dur

    if not scheduled:
        return None

    for idx in scheduled:
        manifest = store.load(manifest.task_id)
        scene = manifest.scenes[idx]
        if scene.state == SceneState.SUBMITTED and scene.job_id:
            if provider.paid:
                # Paid jobs run strictly one at a time: finish this one before paying again.
                _poll_and_download_scene(manifest.task_id, idx, config, store, cache, provider)
                manifest = store.load(manifest.task_id)
                if manifest.status == TaskStatus.PAUSED:
                    return manifest
            continue
        req_sec = scene.requested_seconds or max(
            1, min(math.ceil(scene.duration_seconds or 1.0), provider.max_clip_seconds)
        )
        ckey = ClipCache.make_key(
            provider.name, scene.visual_prompt, manifest.aspect.value, manifest.seed, req_sec
        )
        try:
            job_id = provider.submit(scene.visual_prompt, manifest.aspect, req_sec, manifest.seed)

            def mut_sub(
                m: TaskManifest,
                _idx: int = idx,
                _jid: str = job_id,
                _ckey: str = ckey,
                _sec: int = req_sec,
            ) -> None:
                s = m.scenes[_idx]
                s.job_id, s.state = _jid, SceneState.SUBMITTED
                s.attempts += 1
                m.jobs_submitted += 1
                s.cache_key, s.requested_seconds, s.seed = _ckey, _sec, m.seed

            store.update(manifest.task_id, mut_sub)
        except Exception as exc:
            _handle_provider_error(manifest.task_id, idx, exc, provider.paid, store)
            manifest = store.load(manifest.task_id)
            if manifest.status == TaskStatus.PAUSED:
                return manifest
            continue
        if provider.paid:
            _poll_and_download_scene(manifest.task_id, idx, config, store, cache, provider)
            manifest = store.load(manifest.task_id)
            if manifest.status == TaskStatus.PAUSED:
                return manifest

    manifest = store.load(manifest.task_id)
    to_poll = [
        s.index for s in manifest.scenes if s.state == SceneState.SUBMITTED and s.job_id is not None
    ]
    if not to_poll:
        return None

    if provider.paid or min(config.max_parallel_jobs, 8) <= 1:
        for idx in to_poll:
            _poll_and_download_scene(manifest.task_id, idx, config, store, cache, provider)
            manifest = store.load(manifest.task_id)
            if manifest.status == TaskStatus.PAUSED:
                return manifest
    else:
        workers = min(config.max_parallel_jobs, 8)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(
                    _poll_and_download_scene,
                    manifest.task_id,
                    i,
                    config,
                    store,
                    cache,
                    provider,
                )
                for i in to_poll
            ]
            for f in futs:
                f.result()
        manifest = store.load(manifest.task_id)
        if manifest.status == TaskStatus.PAUSED:
            return manifest
    return None


def _run_compose_stages(
    manifest: TaskManifest,
    config: AppConfig,
    store: TaskStore,
    runner: Runner | None,
    ffmpeg_bin: str | None,
) -> TaskManifest:
    """Execute compose stages 5-7 to produce final video."""
    task_dir = store.task_dir(manifest.task_id)
    bin_path = ffmpeg_bin or config.ffmpeg_path
    effective_runner = runner or default_runner

    kept = [
        s for s in manifest.scenes if s.state in (SceneState.DOWNLOADED, SceneState.FAILED_POLICY)
    ]
    kept.sort(key=lambda s: s.index)
    if not kept:
        manifest.status, manifest.error = TaskStatus.FAILED, "no scenes to compose"
        store.save(manifest)
        return manifest

    manifest.stage = Stage.COMPOSE
    work_dir = safe_join(task_dir, "work")
    work_dir.mkdir(parents=True, exist_ok=True)
    concat_txt = safe_join(work_dir, "audio_concat.txt")
    kept_audio_paths = [
        safe_join(task_dir, s.audio_relpath or f"audio/scene_{s.index:03d}.mp3") for s in kept
    ]
    concat_txt.write_text(compose.concat_list_text(kept_audio_paths), encoding="utf-8")
    narration_mp3 = safe_join(task_dir, "audio", "narration.mp3")
    narration_mp3.parent.mkdir(parents=True, exist_ok=True)
    effective_runner(compose.audio_concat_argv(bin_path, concat_txt, narration_mp3))
    manifest.narration_relpath = "audio/narration.mp3"

    srt_path = safe_join(task_dir, "subtitle.srt")
    write_srt(kept, srt_path)
    manifest.subtitle_relpath = "subtitle.srt"
    store.save(manifest)

    bgm = Path(manifest.bgm_path) if manifest.bgm_path else None
    compose.compose_task(
        bin=bin_path,
        task_dir=task_dir,
        scenes=kept,
        aspect=manifest.aspect,
        narration=narration_mp3,
        srt=srt_path if srt_path.is_file() else None,
        bgm=bgm,
        bgm_volume=config.bgm_volume,
        font_size=config.subtitle_font_size,
        fit=compose.FitMode(manifest.fit_mode),
        runner=effective_runner,
    )
    manifest.stage, manifest.status = Stage.DONE, TaskStatus.COMPLETED
    manifest.final_relpath = "final.mp4"
    manifest.narration_relpath, manifest.subtitle_relpath = "audio/narration.mp3", "subtitle.srt"
    store.save(manifest)
    return manifest


def _run_stages(
    manifest: TaskManifest,
    config: AppConfig,
    store: TaskStore,
    cache: ClipCache,
    provider: VideoProvider,
    script_generator: ScriptGenerator,
    tts: TTS,
    scene_count: int,
    ffmpeg_bin: str | None,
    probe: Callable[[Path], float] | None,
    runner: Runner | None,
) -> TaskManifest:
    """Run all pipeline progression stages sequentially."""
    task_dir = store.task_dir(manifest.task_id)
    bin_path = ffmpeg_bin or config.ffmpeg_path
    effective_probe = probe or (lambda p: ffprobe_duration(bin_path, p))

    if manifest.script is None:
        clamped = max(1, min(scene_count, config.max_clips))
        script = script_generator.generate(
            manifest.topic,
            lang=manifest.lang,
            scene_count=clamped,
            seconds_per_scene=config.scene_seconds,
        )
        manifest.script = script
        manifest.scenes = [s.model_copy() for s in script.scenes]
        manifest.stage = Stage.SCRIPT
        store.save(manifest)

    safe_join(task_dir, "audio").mkdir(parents=True, exist_ok=True)
    for scene in manifest.scenes:
        _voice_scene(task_dir, scene, manifest.voice, tts, effective_probe)
    manifest.stage = Stage.VOICE
    store.save(manifest)

    srt_path = safe_join(task_dir, "subtitle.srt")
    voiced = [s for s in manifest.scenes if s.state == SceneState.VOICED or s.duration_seconds]
    write_srt(voiced, srt_path)
    manifest.subtitle_relpath = "subtitle.srt"
    manifest.stage = Stage.SUBTITLE
    store.save(manifest)

    manifest.stage = Stage.CLIPS
    store.save(manifest)

    paused = _process_clips(manifest, config, store, cache, provider)
    if paused is not None:
        return paused

    manifest = store.load(manifest.task_id)
    return _run_compose_stages(manifest, config, store, runner, ffmpeg_bin)


def _record_failure(store: TaskStore, task_id: str, exc: Exception) -> None:
    try:
        m = store.load(task_id)
        m.status, m.error = TaskStatus.FAILED, redact(str(exc))
        store.save(m)
    except Exception:
        pass


def run_task(
    *,
    config: AppConfig,
    store: TaskStore,
    cache: ClipCache,
    provider: VideoProvider,
    script_generator: ScriptGenerator,
    tts: TTS,
    topic: str,
    aspect: Aspect,
    lang: str,
    scene_count: int,
    seed: int | None = None,
    voice: str,
    bgm_path: str | Path | None = None,
    budget: Budget | None = None,
    fit_mode: str = "cover",
    ffmpeg_bin: str | None = None,
    task_id: str | None = None,
    probe: Callable[[Path], float] | None = None,
    runner: Runner | None = None,
) -> TaskManifest:
    """Create and execute a full video generation pipeline task."""
    tid = validate_task_id(task_id) if task_id is not None else new_task_id()
    task_budget = budget or Budget(
        max_clips=config.max_clips, max_total_seconds=config.max_total_seconds
    )
    task_seed = seed if seed is not None else secrets.randbits(31)
    now = datetime.now(UTC)
    manifest = TaskManifest(
        task_id=tid,
        topic=topic,
        provider=provider.name,
        aspect=aspect,
        lang=lang,
        seed=task_seed,
        voice=voice,
        status=TaskStatus.RUNNING,
        stage=Stage.SCRIPT,
        created_at=now,
        updated_at=now,
        budget=task_budget,
        bgm_path=str(bgm_path) if bgm_path is not None else None,
        fit_mode=fit_mode,
        jobs_submitted=0,
    )
    store.create(manifest)
    try:
        return _run_stages(
            manifest,
            config,
            store,
            cache,
            provider,
            script_generator,
            tts,
            scene_count,
            ffmpeg_bin,
            probe,
            runner,
        )
    except Exception as exc:
        _record_failure(store, tid, exc)
        raise


def resume_task(
    task_id: str,
    *,
    config: AppConfig,
    store: TaskStore,
    cache: ClipCache,
    provider: VideoProvider,
    script_generator: ScriptGenerator,
    tts: TTS,
    ffmpeg_bin: str | None = None,
    probe: Callable[[Path], float] | None = None,
    runner: Runner | None = None,
) -> TaskManifest:
    """Resume execution of an existing task from its earliest incomplete stage."""
    validate_task_id(task_id)
    manifest = store.load(task_id)

    if (
        manifest.status == TaskStatus.COMPLETED
        and safe_join(store.task_dir(task_id), manifest.final_relpath or "final.mp4").is_file()
    ):
        return manifest

    manifest.status, manifest.stop_reason, manifest.error = TaskStatus.RUNNING, None, None
    store.save(manifest)
    try:
        count = len(manifest.scenes) if manifest.scenes else config.scene_count
        return _run_stages(
            manifest,
            config,
            store,
            cache,
            provider,
            script_generator,
            tts,
            count,
            ffmpeg_bin,
            probe,
            runner,
        )
    except Exception as exc:
        _record_failure(store, task_id, exc)
        raise


def regen_scene(
    task_id: str,
    index: int,
    *,
    config: AppConfig,
    store: TaskStore,
    cache: ClipCache,
    provider: VideoProvider,
    tts: TTS,
    prompt: str | None = None,
    ffmpeg_bin: str | None = None,
    probe: Callable[[Path], float] | None = None,
    runner: Runner | None = None,
) -> TaskManifest:
    """Regenerate a single scene clip and recompose the task."""
    validate_task_id(task_id)
    manifest = store.load(task_id)
    task_dir = store.task_dir(task_id)

    target_scene = next((s for s in manifest.scenes if s.index == index), None)
    if target_scene is None:
        raise PipelineError(f"Scene index {index} not found in task {task_id}")

    had_job = (target_scene.job_id is not None) or (
        target_scene.state
        in (SceneState.SUBMITTED, SceneState.DOWNLOADED, SceneState.FAILED_POLICY)
    )
    if manifest.jobs_submitted >= manifest.budget.max_clips and not had_job:
        raise BudgetExceededError(
            f"Budget exceeded: max_clips {manifest.budget.max_clips} reached "
            f"and scene {index} never had a job"
        )

    if prompt is not None and prompt != "":
        target_scene.visual_prompt = prompt

    target_scene.job_id, target_scene.clip_relpath = None, None
    target_scene.cache_key, target_scene.error, target_scene.attempts = None, None, 0

    effective_probe = probe or (lambda p: ffprobe_duration(ffmpeg_bin or config.ffmpeg_path, p))
    _voice_scene(task_dir, target_scene, manifest.voice, tts, effective_probe)

    manifest.status, manifest.stop_reason, manifest.error = TaskStatus.RUNNING, None, None
    store.save(manifest)

    dur = target_scene.duration_seconds if target_scene.duration_seconds is not None else 1.0
    req_sec = max(1, min(math.ceil(dur), provider.max_clip_seconds))
    target_scene.requested_seconds, target_scene.seed = req_sec, manifest.seed
    ckey = ClipCache.make_key(
        provider.name, target_scene.visual_prompt, manifest.aspect.value, manifest.seed, req_sec
    )
    target_scene.cache_key = ckey
    store.save(manifest)

    cached = cache.get(ckey)
    if cached is not None:
        dest = safe_join(task_dir, f"clips/scene_{target_scene.index:03d}.mp4")
        shutil.copyfile(cached, dest)
        target_scene.clip_relpath = f"clips/scene_{target_scene.index:03d}.mp4"
        target_scene.state, target_scene.error = SceneState.DOWNLOADED, None
        store.save(manifest)
        return _run_compose_stages(manifest, config, store, runner, ffmpeg_bin)

    try:
        try:
            job_id = provider.submit(
                target_scene.visual_prompt, manifest.aspect, req_sec, manifest.seed
            )
            target_scene.job_id, target_scene.state = job_id, SceneState.SUBMITTED
            target_scene.attempts += 1
            manifest.jobs_submitted += 1
            store.save(manifest)
        except Exception as exc:
            _handle_provider_error(manifest.task_id, target_scene.index, exc, provider.paid, store)
            manifest = store.load(manifest.task_id)
            if manifest.status == TaskStatus.PAUSED:
                return manifest

        manifest = store.load(manifest.task_id)
        if target_scene.index in [
            s.index
            for s in manifest.scenes
            if s.state == SceneState.SUBMITTED and s.job_id is not None
        ]:
            _poll_and_download_scene(
                manifest.task_id, target_scene.index, config, store, cache, provider
            )
            manifest = store.load(manifest.task_id)
            if manifest.status == TaskStatus.PAUSED:
                return manifest

        return _run_compose_stages(manifest, config, store, runner, ffmpeg_bin)
    except Exception as exc:
        _record_failure(store, task_id, exc)
        raise
