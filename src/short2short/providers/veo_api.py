from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from short2short.core.models import Aspect, JobState, JobStatus
from short2short.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderPolicyError,
    ProviderQuotaError,
    ProviderUnconfirmedJobError,
    Short2ShortError,
)
from short2short.storage.cache import ClipCache

logger = logging.getLogger(__name__)


def _is_blocked(msg: str) -> bool:
    """Return True if error message indicates safety or policy blocking."""
    upper = msg.upper()
    return any(hint in upper for hint in ("SAFETY", "POLICY", "BLOCKED", "PROHIBITED", "RAI"))


def _is_quota(msg: str) -> bool:
    """Return True if error message indicates quota exhaustion or rate limits."""
    upper = msg.upper()
    return any(hint in upper for hint in ("RESOURCE_EXHAUSTED", "QUOTA", "429"))


def _is_auth(msg: str) -> bool:
    """Return True if error message indicates authentication or authorization issues."""
    upper = msg.upper()
    return any(
        hint in upper
        for hint in ("API_KEY_INVALID", "UNAUTHENTICATED", "INVALID API KEY", "PERMISSION_DENIED")
    )


def _is_transport(exc: Exception) -> bool:
    """Return True if exception indicates network or transport timeout."""
    name = type(exc).__name__.lower()
    return any(hint in name for hint in ("timeout", "connect", "network", "transport"))


class VeoApiProvider:
    """Official Google Gemini Veo video generation provider adapter."""

    name: str = "veo_api"
    max_clip_seconds: int = 8
    paid: bool = True
    _types: Any = None

    def __init__(
        self,
        api_key: str,
        model: str,
        cache: ClipCache,
        client: Any = None,
        poll_interval: float = 5.0,
        poll_timeout: float = 600.0,
        types_module: Any = None,
    ) -> None:
        """Initialize Veo API provider with credentials, model, and cache."""
        self.api_key: str = api_key
        self.model: str = model
        self.cache: ClipCache = cache
        self.poll_interval_seconds: float = float(poll_interval)
        self.poll_timeout_seconds: float = float(poll_timeout)
        self._operations: dict[str, Any] = {}

        if types_module is not None:
            self._types = types_module
        elif self._types is None:
            try:
                from google.genai import types

                self._types = types
            except ImportError as exc:
                if client is None:
                    raise ProviderError(
                        f"google-genai is not installed: {exc}", provider=self.name
                    ) from exc
                self._types = None

        if client is None:
            if not api_key or not api_key.strip():
                raise ProviderAuthError("Veo API key is empty", provider=self.name)
            try:
                from google import genai

                self.client: Any = genai.Client(api_key=api_key)
            except ImportError as exc:
                raise ProviderError(
                    f"google-genai is not installed: {exc}", provider=self.name
                ) from exc
        else:
            self.client = client

    def _ensure_types(self) -> Any:
        """Ensure google.genai.types is loaded or raise ProviderError."""
        if self._types is not None:
            return self._types
        try:
            from google.genai import types

            self._types = types
            return self._types
        except ImportError as exc:
            raise ProviderError(
                f"google-genai is not installed: {exc}", provider=self.name
            ) from exc

    def _handle_submit_exception(self, exc: Exception) -> None:
        """Categorize and raise appropriate provider error."""
        if isinstance(exc, Short2ShortError):
            raise exc
        msg = str(exc)
        if _is_blocked(msg):
            raise ProviderPolicyError(msg, provider=self.name) from exc
        if _is_quota(msg):
            raise ProviderQuotaError(msg, provider=self.name) from exc
        if _is_auth(msg):
            raise ProviderAuthError(msg, provider=self.name) from exc
        if _is_transport(exc):
            raise ProviderUnconfirmedJobError(
                f"Ambiguous transport failure during submit: {exc}",
                job_id=None,
                provider=self.name,
            ) from exc
        raise ProviderError(
            f"Failed to submit video generation job: {exc}", provider=self.name
        ) from exc

    def submit(self, prompt: str, aspect: Aspect, seconds: int, seed: int | None) -> str:
        """Submit a video generation job to Gemini Veo."""
        if not prompt or not prompt.strip():
            raise ProviderError("Prompt must not be empty", provider=self.name)

        types_mod = self._ensure_types()
        clamped = max(1, min(seconds, self.max_clip_seconds))

        config_kwargs: dict[str, Any] = {
            "aspect_ratio": aspect.value,
            "duration_seconds": clamped,
            "number_of_videos": 1,
        }
        if seed is not None:
            config_kwargs["seed"] = seed

        try:
            config = types_mod.GenerateVideosConfig(**config_kwargs)
            op = self.client.models.generate_videos(
                model=self.model,
                prompt=prompt,
                config=config,
            )
        except TypeError as exc:
            if "seed" in str(exc).lower() and "seed" in config_kwargs:
                logger.warning(
                    "Veo API does not support seed parameter, retrying without seed: %s",
                    exc,
                )
                config_kwargs.pop("seed", None)
                config = types_mod.GenerateVideosConfig(**config_kwargs)
                try:
                    op = self.client.models.generate_videos(
                        model=self.model,
                        prompt=prompt,
                        config=config,
                    )
                except Exception as retry_exc:
                    self._handle_submit_exception(retry_exc)
            else:
                self._handle_submit_exception(exc)
        except Exception as exc:
            self._handle_submit_exception(exc)

        job_id = getattr(op, "name", None)
        if not job_id:
            raise ProviderError("Veo API did not return an operation name", provider=self.name)

        job_id_str = str(job_id)
        self._operations[job_id_str] = op
        self.cache.write_job_record(
            self.name,
            job_id_str,
            {
                "job_id": job_id_str,
                "model": self.model,
                "prompt": prompt,
                "aspect": aspect.value,
                "seconds": clamped,
                "seed": seed,
            },
        )
        return job_id_str

    def poll(self, job_id: str) -> JobStatus:
        """Poll the generation status of an existing job."""
        types_mod = self._ensure_types()

        op = self._operations.get(job_id)
        if op is None:
            op = types_mod.GenerateVideosOperation(name=job_id)

        try:
            op = self.client.operations.get(op)
            self._operations[job_id] = op
        except Exception as exc:
            msg = str(exc)
            if _is_blocked(msg):
                raise ProviderPolicyError(msg, provider=self.name) from exc
            if _is_quota(msg):
                raise ProviderQuotaError(msg, provider=self.name) from exc
            if _is_auth(msg):
                raise ProviderAuthError(msg, provider=self.name) from exc
            logger.warning("Error polling Veo operation %s: %s", job_id, exc)
            return JobStatus(state=JobState.FAILED, job_id=job_id, error=msg)

        if getattr(op, "error", None):
            error_val = op.error
            if isinstance(error_val, dict):
                err_msg = str(error_val.get("message") or error_val)
            elif hasattr(error_val, "message"):
                err_msg = str(error_val.message)
            else:
                err_msg = str(error_val)

            if _is_blocked(err_msg):
                raise ProviderPolicyError(err_msg, provider=self.name)
            if _is_quota(err_msg):
                raise ProviderQuotaError(err_msg, provider=self.name)
            if _is_auth(err_msg):
                raise ProviderAuthError(err_msg, provider=self.name)
            return JobStatus(state=JobState.FAILED, job_id=job_id, error=err_msg)

        if not getattr(op, "done", False):
            return JobStatus(state=JobState.RUNNING, job_id=job_id)

        response = getattr(op, "response", None) or getattr(op, "result", None)
        generated_videos = getattr(response, "generated_videos", None) if response else None
        rai_count = getattr(response, "rai_media_filtered_count", 0) if response else 0
        rai_reasons = getattr(response, "rai_media_filtered_reasons", None) if response else None

        if not generated_videos:
            if (rai_count and rai_count > 0) or rai_reasons:
                reasons_str = ", ".join(rai_reasons) if rai_reasons else f"count={rai_count}"
                raise ProviderPolicyError(
                    f"Video generation blocked by safety policy ({reasons_str})",
                    provider=self.name,
                )
            return JobStatus(
                state=JobState.FAILED,
                job_id=job_id,
                error="Video generation completed without generated videos",
            )

        return JobStatus(state=JobState.SUCCEEDED, job_id=job_id)

    def download(self, job_id: str, dest: Path) -> Path:
        """Download the completed video file to dest."""
        types_mod = self._ensure_types()

        op = self._operations.get(job_id)
        if op is None:
            record = self.cache.read_job_record(self.name, job_id)
            if record is None:
                raise ProviderError(f"Unknown job_id: {job_id}", provider=self.name)
            op = types_mod.GenerateVideosOperation(name=job_id)
            try:
                op = self.client.operations.get(op)
                self._operations[job_id] = op
            except Exception as exc:
                msg = str(exc)
                if _is_blocked(msg):
                    raise ProviderPolicyError(msg, provider=self.name) from exc
                if _is_quota(msg):
                    raise ProviderQuotaError(msg, provider=self.name) from exc
                raise ProviderError(
                    f"Failed to fetch operation for {job_id}: {exc}", provider=self.name
                ) from exc

        response = getattr(op, "response", None) or getattr(op, "result", None)
        if not getattr(op, "done", False) or response is None:
            try:
                op = self.client.operations.get(op)
                self._operations[job_id] = op
            except Exception as exc:
                raise ProviderError(
                    f"Failed to refresh operation for {job_id}: {exc}", provider=self.name
                ) from exc
            response = getattr(op, "response", None) or getattr(op, "result", None)

        if not getattr(op, "done", False):
            raise ProviderError(f"Job {job_id} is not complete yet", provider=self.name)

        if response is None:
            raise ProviderError(f"Job {job_id} has no response", provider=self.name)

        videos = getattr(response, "generated_videos", None)
        if not videos:
            raise ProviderError(f"Job {job_id} has no generated videos", provider=self.name)

        v = videos[0]
        video_obj = getattr(v, "video", None)
        if video_obj is None:
            raise ProviderError(
                f"Generated video for job {job_id} has no video object", provider=self.name
            )

        try:
            self.client.files.download(file=video_obj)
            dest.parent.mkdir(parents=True, exist_ok=True)
            video_obj.save(str(dest))
        except Exception as exc:
            msg = str(exc)
            if _is_blocked(msg):
                raise ProviderPolicyError(msg, provider=self.name) from exc
            if _is_quota(msg):
                raise ProviderQuotaError(msg, provider=self.name) from exc
            raise ProviderError(
                f"Failed to download video for job {job_id}: {exc}", provider=self.name
            ) from exc

        return dest
