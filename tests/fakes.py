from __future__ import annotations

from pathlib import Path

from short2short.core.models import Aspect, JobState, JobStatus, Scene, Script, VoiceResult, WordCue
from short2short.errors import ProviderPolicyError, ProviderQuotaError, ProviderUnconfirmedJobError


class MemoryProvider:
    """In-memory video provider for unit and pipeline tests."""

    name = "memory"
    max_clip_seconds = 8

    def __init__(
        self,
        *,
        paid: bool = False,
        fail_policy_at: set[int] | None = None,
        unconfirmed_at: set[int] | None = None,
        quota_at: set[int] | None = None,
    ) -> None:
        self.paid: bool = paid
        self.fail_policy_at: set[int] = set(fail_policy_at or ())
        self.unconfirmed_at: set[int] = set(unconfirmed_at or ())
        self.quota_at: set[int] = set(quota_at or ())
        self.submits: list[tuple[str, Aspect, int, int | None]] = []
        self.poll_interval_seconds: float = 0.0
        self.poll_timeout_seconds: float = 5.0

    def submit(self, prompt: str, aspect: Aspect, seconds: int, seed: int | None) -> str:
        """Record submission or raise configured error at specific submit index."""
        idx = len(self.submits)
        self.submits.append((prompt, aspect, seconds, seed))
        if idx in self.fail_policy_at:
            raise ProviderPolicyError(f"Policy failure at submit {idx}", provider=self.name)
        if idx in self.unconfirmed_at:
            raise ProviderUnconfirmedJobError(
                f"Unconfirmed job at submit {idx}",
                job_id=f"job-{idx}",
                provider=self.name,
            )
        if idx in self.quota_at:
            raise ProviderQuotaError(f"Quota exceeded at submit {idx}", provider=self.name)
        return f"mem-{idx}"

    def poll(self, job_id: str) -> JobStatus:
        """Poll job status, always succeeding for in-memory provider."""
        return JobStatus(state=JobState.SUCCEEDED, job_id=job_id)

    def download(self, job_id: str, dest: Path) -> Path:
        """Write fake mp4 payload to destination path."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-mp4")
        return dest


class FakeScript:
    """Fake script generator producing predictable scenes."""

    def generate(
        self, topic: str, *, lang: str, scene_count: int, seconds_per_scene: int
    ) -> Script:
        """Generate test script with formatted narration and visual prompts."""
        scenes = [
            Scene(
                index=i,
                narration=f"line {i}",
                visual_prompt=f"visual {i}",
            )
            for i in range(scene_count)
        ]
        return Script(topic=topic, lang=lang, scenes=scenes)


class FakeTTS:
    """Fake TTS synthesizer producing fixed duration audio and cue."""

    def synthesize(self, text: str, dest: Path, *, voice: str) -> VoiceResult:
        """Synthesize mock audio file with fixed duration."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ID3")
        cue = WordCue(text=text, start=0.0, end=2.5)
        return VoiceResult(path=dest, duration_seconds=2.5, cues=[cue])
