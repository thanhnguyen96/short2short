from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from short2short.core.models import Aspect, JobState
from short2short.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderPolicyError,
    ProviderQuotaError,
    ProviderUnconfirmedJobError,
)
from short2short.providers.base import VideoProvider
from short2short.providers.veo_api import VeoApiProvider
from short2short.storage.cache import ClipCache


class FakeVideo:
    """Mock GenAI Video object with save capability."""

    def __init__(self, data: bytes = b"fake-veo-mp4-data") -> None:
        self.data: bytes = data
        self.video_bytes: bytes | None = None

    def save(self, path: str) -> None:
        Path(path).write_bytes(self.data)


class FakeGeneratedVideo:
    """Mock GenAI GeneratedVideo container."""

    def __init__(self, video: FakeVideo | None = None) -> None:
        self.video: FakeVideo = video or FakeVideo()


class FakeGenerateVideosResponse:
    """Mock GenAI GenerateVideosResponse."""

    def __init__(
        self,
        generated_videos: list[FakeGeneratedVideo] | None = None,
        rai_media_filtered_count: int | None = None,
        rai_media_filtered_reasons: list[str] | None = None,
    ) -> None:
        self.generated_videos: list[FakeGeneratedVideo] | None = generated_videos
        self.rai_media_filtered_count: int | None = rai_media_filtered_count
        self.rai_media_filtered_reasons: list[str] | None = rai_media_filtered_reasons


class FakeOperation:
    """Mock GenAI GenerateVideosOperation."""

    def __init__(
        self,
        name: str = "operations/fake-op-123",
        done: bool = False,
        error: dict[str, Any] | None = None,
        response: FakeGenerateVideosResponse | None = None,
    ) -> None:
        self.name: str = name
        self.done: bool = done
        self.error: dict[str, Any] | None = error
        self.response: FakeGenerateVideosResponse | None = response
        self.result: FakeGenerateVideosResponse | None = response


class FakeGenerateVideosConfig:
    """Mock GenAI GenerateVideosConfig."""

    def __init__(
        self,
        *,
        aspect_ratio: str | None = None,
        duration_seconds: int | None = None,
        number_of_videos: int | None = None,
        seed: int | None = None,
    ) -> None:
        self.aspect_ratio: str | None = aspect_ratio
        self.duration_seconds: int | None = duration_seconds
        self.number_of_videos: int | None = number_of_videos
        self.seed: int | None = seed


class FakeTypes:
    """Mock google.genai.types namespace."""

    GenerateVideosConfig = FakeGenerateVideosConfig
    GenerateVideosOperation = FakeOperation


class FakeModels:
    """Mock client.models module."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_op: FakeOperation = FakeOperation()
        self.error_to_raise: Exception | None = None
        self.type_error_on_first_seed: bool = False

    def generate_videos(
        self,
        *,
        model: str,
        prompt: str,
        config: Any = None,
    ) -> FakeOperation:
        self.calls.append({"model": model, "prompt": prompt, "config": config})
        if self.type_error_on_first_seed and config and getattr(config, "seed", None) is not None:
            self.type_error_on_first_seed = False
            raise TypeError("generate_videos got an unexpected keyword argument 'seed'")
        if self.error_to_raise:
            raise self.error_to_raise
        return self.next_op


class FakeOperations:
    """Mock client.operations module."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.ops: dict[str, FakeOperation] = {}
        self.error_to_raise: Exception | None = None

    def get(self, operation: Any) -> FakeOperation:
        self.calls.append(operation)
        if self.error_to_raise:
            raise self.error_to_raise
        name = getattr(operation, "name", str(operation))
        if name in self.ops:
            return self.ops[name]
        return operation  # type: ignore[no-any-return]


class FakeFiles:
    """Mock client.files module."""

    def __init__(self) -> None:
        self.download_calls: list[Any] = []
        self.error_to_raise: Exception | None = None

    def download(self, *, file: Any) -> None:
        self.download_calls.append(file)
        if self.error_to_raise:
            raise self.error_to_raise


class FakeGenAIClient:
    """Mock google.genai.Client."""

    def __init__(self) -> None:
        self.models: FakeModels = FakeModels()
        self.operations: FakeOperations = FakeOperations()
        self.files: FakeFiles = FakeFiles()


@pytest.fixture
def fake_client() -> FakeGenAIClient:
    return FakeGenAIClient()


@pytest.fixture
def clip_cache(tmp_path: Path) -> ClipCache:
    return ClipCache(tmp_path / "cache")


@pytest.fixture
def provider(fake_client: FakeGenAIClient, clip_cache: ClipCache) -> VeoApiProvider:
    p = VeoApiProvider(
        api_key="test-api-key",
        model="veo-3.1-generate-preview",
        cache=clip_cache,
        client=fake_client,
        poll_interval=5.0,
        poll_timeout=600.0,
        types_module=FakeTypes,
    )
    p._types = FakeTypes
    return p


def test_veo_provider_implements_video_provider(provider: VeoApiProvider) -> None:
    assert isinstance(provider, VideoProvider)
    assert provider.name == "veo_api"
    assert provider.paid is True
    assert provider.max_clip_seconds == 8
    assert provider.poll_interval_seconds == 5.0
    assert provider.poll_timeout_seconds == 600.0


def test_empty_api_key_without_client_raises(clip_cache: ClipCache) -> None:
    with pytest.raises(ProviderAuthError, match="Veo API key is empty"):
        VeoApiProvider(api_key="", model="veo-3.1-generate-preview", cache=clip_cache)


def test_submit_success(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    fake_client.models.next_op = FakeOperation(name="operations/veo-123")

    job_id = provider.submit(
        prompt="A futuristic flying car",
        aspect=Aspect.PORTRAIT,
        seconds=8,
        seed=1234,
    )

    assert job_id == "operations/veo-123"
    assert len(fake_client.models.calls) == 1
    call = fake_client.models.calls[0]
    assert call["model"] == "veo-3.1-generate-preview"
    assert call["prompt"] == "A futuristic flying car"
    assert call["config"].aspect_ratio == "9:16"
    assert call["config"].duration_seconds == 8
    assert call["config"].seed == 1234
    assert call["config"].number_of_videos == 1

    record = provider.cache.read_job_record("veo_api", job_id)
    assert record is not None
    assert record["job_id"] == "operations/veo-123"
    assert record["aspect"] == "9:16"
    assert record["seconds"] == 8
    assert record["seed"] == 1234


def test_submit_empty_prompt_raises(provider: VeoApiProvider) -> None:
    with pytest.raises(ProviderError, match="Prompt must not be empty"):
        provider.submit(prompt="   ", aspect=Aspect.PORTRAIT, seconds=8, seed=None)


def test_submit_clamps_duration(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    fake_client.models.next_op = FakeOperation(name="operations/clamp-test")

    provider.submit(prompt="drone shot", aspect=Aspect.LANDSCAPE, seconds=20, seed=None)
    assert fake_client.models.calls[-1]["config"].duration_seconds == 8

    provider.submit(prompt="drone shot", aspect=Aspect.LANDSCAPE, seconds=0, seed=None)
    assert fake_client.models.calls[-1]["config"].duration_seconds == 1


def test_submit_seed_type_error_retries_without_seed(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    fake_client.models.next_op = FakeOperation(name="operations/retry-test")
    fake_client.models.type_error_on_first_seed = True

    job_id = provider.submit(
        prompt="retro diner neon",
        aspect=Aspect.SQUARE,
        seconds=6,
        seed=777,
    )

    assert job_id == "operations/retry-test"
    assert len(fake_client.models.calls) == 2
    assert fake_client.models.calls[0]["config"].seed == 777
    assert fake_client.models.calls[1]["config"].seed is None


def test_submit_policy_violation(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    fake_client.models.error_to_raise = Exception("SAFETY_VIOLATION: Prompt violates policy")
    with pytest.raises(ProviderPolicyError, match="SAFETY_VIOLATION"):
        provider.submit("unsafe prompt", Aspect.PORTRAIT, 8, None)


def test_submit_quota_exceeded(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    fake_client.models.error_to_raise = Exception("RESOURCE_EXHAUSTED: 429 Rate limit exceeded")
    with pytest.raises(ProviderQuotaError, match="RESOURCE_EXHAUSTED"):
        provider.submit("high volume prompt", Aspect.PORTRAIT, 8, None)


def test_submit_auth_error(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    fake_client.models.error_to_raise = Exception("API_KEY_INVALID: The provided key is invalid")
    with pytest.raises(ProviderAuthError, match="API_KEY_INVALID"):
        provider.submit("prompt", Aspect.PORTRAIT, 8, None)


def test_submit_transport_failure(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    fake_client.models.error_to_raise = TimeoutError("Connection timed out after 30s")
    with pytest.raises(ProviderUnconfirmedJobError) as exc_info:
        provider.submit("prompt", Aspect.PORTRAIT, 8, None)
    assert exc_info.value.job_id is None


def test_poll_running(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    fake_client.operations.ops["operations/op-run"] = FakeOperation(
        name="operations/op-run", done=False
    )

    status = provider.poll("operations/op-run")
    assert status.state == JobState.RUNNING
    assert status.job_id == "operations/op-run"


def test_poll_succeeded(provider: VeoApiProvider, fake_client: FakeGenAIClient) -> None:
    response = FakeGenerateVideosResponse(generated_videos=[FakeGeneratedVideo()])
    fake_client.operations.ops["operations/op-done"] = FakeOperation(
        name="operations/op-done",
        done=True,
        response=response,
    )

    status = provider.poll("operations/op-done")
    assert status.state == JobState.SUCCEEDED
    assert status.job_id == "operations/op-done"


def test_poll_done_empty_videos_with_rai_count(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    response = FakeGenerateVideosResponse(generated_videos=[], rai_media_filtered_count=2)
    fake_client.operations.ops["operations/op-rai-count"] = FakeOperation(
        name="operations/op-rai-count",
        done=True,
        response=response,
    )

    with pytest.raises(ProviderPolicyError, match="safety policy"):
        provider.poll("operations/op-rai-count")


def test_poll_done_empty_videos_with_rai_reasons(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    response = FakeGenerateVideosResponse(
        generated_videos=[], rai_media_filtered_reasons=["PROHIBITED_VIOLENCE"]
    )
    fake_client.operations.ops["operations/op-rai-reasons"] = FakeOperation(
        name="operations/op-rai-reasons",
        done=True,
        response=response,
    )

    with pytest.raises(ProviderPolicyError, match="PROHIBITED_VIOLENCE"):
        provider.poll("operations/op-rai-reasons")


def test_poll_done_empty_videos_without_rai(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    response = FakeGenerateVideosResponse(
        generated_videos=[], rai_media_filtered_count=0, rai_media_filtered_reasons=[]
    )
    fake_client.operations.ops["operations/op-empty"] = FakeOperation(
        name="operations/op-empty",
        done=True,
        response=response,
    )

    status = provider.poll("operations/op-empty")
    assert status.state == JobState.FAILED
    assert "without generated videos" in (status.error or "")


def test_poll_error_in_operation_policy(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    fake_client.operations.ops["operations/op-err-policy"] = FakeOperation(
        name="operations/op-err-policy",
        done=True,
        error={"message": "Content blocked by POLICY review"},
    )

    with pytest.raises(ProviderPolicyError, match="POLICY"):
        provider.poll("operations/op-err-policy")


def test_poll_error_in_operation_quota(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    fake_client.operations.ops["operations/op-err-quota"] = FakeOperation(
        name="operations/op-err-quota",
        done=True,
        error={"message": "RESOURCE_EXHAUSTED: daily limit reached"},
    )

    with pytest.raises(ProviderQuotaError, match="RESOURCE_EXHAUSTED"):
        provider.poll("operations/op-err-quota")


def test_poll_error_in_operation_generic(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    fake_client.operations.ops["operations/op-err-generic"] = FakeOperation(
        name="operations/op-err-generic",
        done=True,
        error={"message": "internal processing failure"},
    )

    status = provider.poll("operations/op-err-generic")
    assert status.state == JobState.FAILED
    assert "internal processing failure" in (status.error or "")


def test_poll_client_exception_quota(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    fake_client.operations.error_to_raise = Exception("429 Too Many Requests: QUOTA limit")
    with pytest.raises(ProviderQuotaError, match="QUOTA"):
        provider.poll("operations/any")


def test_poll_client_exception_policy(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    fake_client.operations.error_to_raise = Exception("SAFETY check rejected request")
    with pytest.raises(ProviderPolicyError, match="SAFETY"):
        provider.poll("operations/any")


def test_poll_client_exception_generic(
    provider: VeoApiProvider, fake_client: FakeGenAIClient
) -> None:
    fake_client.operations.error_to_raise = Exception("Unexpected connection reset")
    status = provider.poll("operations/any")
    assert status.state == JobState.FAILED
    assert "Unexpected connection reset" in (status.error or "")


def test_download_success(
    provider: VeoApiProvider, fake_client: FakeGenAIClient, tmp_path: Path
) -> None:
    video_bytes = b"mp4-content-veo"
    v = FakeVideo(data=video_bytes)
    resp = FakeGenerateVideosResponse(generated_videos=[FakeGeneratedVideo(video=v)])
    fake_client.operations.ops["operations/dl-op"] = FakeOperation(
        name="operations/dl-op",
        done=True,
        response=resp,
    )

    provider.poll("operations/dl-op")
    dest = tmp_path / "output" / "clip.mp4"
    result = provider.download("operations/dl-op", dest)

    assert result == dest
    assert dest.is_file()
    assert dest.read_bytes() == video_bytes
    assert len(fake_client.files.download_calls) == 1


def test_download_after_restart(
    provider: VeoApiProvider,
    fake_client: FakeGenAIClient,
    clip_cache: ClipCache,
    tmp_path: Path,
) -> None:
    fake_client.models.next_op = FakeOperation(name="operations/restart-job")
    job_id = provider.submit("cool sunset", Aspect.LANDSCAPE, 8, None)

    v = FakeVideo(data=b"restart-clip-data")
    resp = FakeGenerateVideosResponse(generated_videos=[FakeGeneratedVideo(video=v)])
    fake_client.operations.ops[job_id] = FakeOperation(
        name=job_id,
        done=True,
        response=resp,
    )

    new_provider = VeoApiProvider(
        api_key="test-api-key",
        model="veo-3.1-generate-preview",
        cache=clip_cache,
        client=fake_client,
        types_module=FakeTypes,
    )
    new_provider._types = FakeTypes

    dest = tmp_path / "restarted" / "out.mp4"
    result = new_provider.download(job_id, dest)

    assert result == dest
    assert dest.is_file()
    assert dest.read_bytes() == b"restart-clip-data"


def test_download_unknown_job_raises(provider: VeoApiProvider, tmp_path: Path) -> None:
    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError, match="Unknown job_id"):
        provider.download("operations/nonexistent", dest)


def test_download_not_done_raises(
    provider: VeoApiProvider, fake_client: FakeGenAIClient, tmp_path: Path
) -> None:
    fake_client.models.next_op = FakeOperation(name="operations/pending-job")
    job_id = provider.submit("waiting", Aspect.PORTRAIT, 8, None)

    fake_client.operations.ops[job_id] = FakeOperation(name=job_id, done=False)

    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError, match="not complete yet"):
        provider.download(job_id, dest)


def test_download_failure_raises(
    provider: VeoApiProvider, fake_client: FakeGenAIClient, tmp_path: Path
) -> None:
    v = FakeVideo()
    resp = FakeGenerateVideosResponse(generated_videos=[FakeGeneratedVideo(video=v)])
    fake_client.operations.ops["operations/dl-fail"] = FakeOperation(
        name="operations/dl-fail",
        done=True,
        response=resp,
    )
    provider.poll("operations/dl-fail")
    fake_client.files.error_to_raise = Exception("Download stream disconnected")

    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError, match="Failed to download video"):
        provider.download("operations/dl-fail", dest)
