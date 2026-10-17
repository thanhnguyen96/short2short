"""EXPERIMENTAL. Unofficial Google Flow (flow.google.com) adapter.

Private UI changes without notice and may violate Google Terms of Service.
Drives the visible Flow UI through actions a user would take (focus the composer,
insert text, press Enter, click Approve). Uses a dedicated Chrome profile on
loopback CDP. Does not bypass safety filters, rewrite prompts, add humanizer
delays or mouse jitter, rotate proxies, or solve captchas.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import websocket

from short2short.config import AppConfig
from short2short.core.models import Aspect, JobState, JobStatus
from short2short.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderPolicyError,
    ProviderQuotaError,
    ProviderUnconfirmedJobError,
    Short2ShortError,
)
from short2short.ffmpeg import ffprobe_duration
from short2short.net import download_url
from short2short.providers.flow.auth import CdpPage, read_port
from short2short.providers.flow.constants import (
    DOWNLOAD_FETCH_TIMEOUT_SECONDS,
    FLOW_PAGE_URL,
    MAX_ERROR_TAIL,
    PROJECT_TITLE,
    REPLY_TIMEOUT_SECONDS,
)
from short2short.providers.flow.parse import (
    ReplyKind,
    classify_reply,
    compose_user_message,
    parse_approval,
    redact_media_url,
    uuid_from_media_url,
)
from short2short.providers.flow.ui import FlowUi
from short2short.security import redact
from short2short.storage.cache import ClipCache

logger = logging.getLogger(__name__)

__all__ = [
    "FlowProvider",
    "build_flow_provider",
    "media_has_video_stream",
]

_PROGRESS_SENTINEL = "__FLOW_PROGRESS_SENTINEL__"
_PROJECT_PATH_RE = re.compile(r"^/project/([0-9a-fA-F-]{36})(?:/|$)")


def media_has_video_stream(bin_ffmpeg: str, path: Path, *, timeout: float = 30) -> bool:
    """True when ffprobe -show_streams reports a stream with codec_type video.

    Uses the same ffprobe path rule as ffprobe_duration (sibling of ffmpeg.exe, else 'ffprobe').
    shell=False, argv list. Short2ShortError from a failed probe becomes ProviderError.
    """
    p = Path(bin_ffmpeg)
    if p.name.lower() in ("ffmpeg", "ffmpeg.exe"):
        probe_bin = str(p.with_name(f"ffprobe{p.suffix}"))
    else:
        probe_bin = "ffprobe"

    cmd = [
        probe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "v",
        str(path),
    ]

    try:
        res = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("ffprobe rejected the download", provider="flow") from exc
    except OSError as exc:
        raise ProviderError("ffprobe rejected the download", provider="flow") from exc
    except Short2ShortError as exc:
        raise ProviderError("ffprobe rejected the download", provider="flow") from exc

    if res.returncode != 0:
        raise ProviderError("ffprobe rejected the download", provider="flow")

    try:
        stdout_str = res.stdout.decode("utf-8", errors="replace")
        data: dict[str, Any] = json.loads(stdout_str)
        streams = data.get("streams")
        if isinstance(streams, list) and len(streams) > 0:
            return any(isinstance(s, dict) and s.get("codec_type") == "video" for s in streams)
        return False
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderError("ffprobe rejected the download", provider="flow") from exc


class FlowProvider:
    """Video provider that drives the Flow agent panel."""

    def __init__(
        self,
        ui: FlowUi,
        cache: ClipCache,
        *,
        max_credits_per_clip: int = 10,
        auto_approve: bool = True,
        ffmpeg_bin: str = "ffmpeg",
        allow_hosts: set[str] | None = None,
        poll_interval: float = 5.0,
        poll_timeout: float = 600.0,
        download_fn: Callable[..., None] = download_url,
        probe_duration_fn: Callable[..., float] = ffprobe_duration,
        probe_has_video_fn: Callable[..., bool] | None = None,
    ) -> None:
        self.ui = ui
        self.cache = cache
        self.name: str = "flow"
        self.paid: bool = True
        self.max_clip_seconds: int = 8
        self.poll_interval_seconds: float = poll_interval
        self.poll_timeout_seconds: float = poll_timeout
        self.max_credits_per_clip = max_credits_per_clip
        self.auto_approve = auto_approve
        self.ffmpeg_bin = ffmpeg_bin

        base_allow = {"flow-content.google"}
        if allow_hosts is not None:
            self.allow_hosts: set[str] = base_allow | set(allow_hosts)
        else:
            self.allow_hosts = base_allow

        self.download_fn = download_fn
        self.probe_duration_fn = probe_duration_fn
        self.probe_has_video_fn: Callable[..., bool] = (
            probe_has_video_fn if probe_has_video_fn is not None else media_has_video_stream
        )
        self._lock = threading.Lock()

    @staticmethod
    def _canonical_project_url(url: str) -> str:
        """Reduce any project sub-page URL to https://flow.google.com/project/<id>."""
        m = _PROJECT_PATH_RE.match(urllib.parse.urlsplit(url).path)
        if m is None:
            raise ProviderError("stored project url rejected", provider="flow")
        return f"https://flow.google.com/project/{m.group(1)}"

    def _load_project(self) -> dict[str, Any] | None:
        """Load stored project record from cache if present and valid."""
        rec = self.cache.read_job_record(self.name, "_project")
        if not isinstance(rec, dict):
            return None
        url = rec.get("url")
        if not isinstance(url, str):
            return None
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise ProviderError("stored project url rejected", provider=self.name) from exc
        if parsed.scheme != "https" or parsed.hostname != "flow.google.com":
            raise ProviderError("stored project url rejected", provider=self.name)
        return rec

    def _save_project(self, url: str, *, created: bool) -> None:
        """Persist project URL to cache atomically."""
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise ProviderError("stored project url rejected", provider=self.name) from exc
        if parsed.scheme != "https" or parsed.hostname != "flow.google.com":
            raise ProviderError("stored project url rejected", provider=self.name)
        self.cache.write_job_record(
            self.name,
            "_project",
            {"title": PROJECT_TITLE, "url": url, "created": created},
        )

    def _ensure_project(self) -> str:
        """Open stored project, or existing link titled short2short, or click New project.

        Returns the canonical project page URL (https://flow.google.com/project/<id>).
        Sub-pages such as /tools have no agent panel, so they are never returned.
        """
        project = self._load_project()
        if project and isinstance(project.get("url"), str):
            canonical = self._canonical_project_url(str(project["url"]))
            self.ui.open_project(canonical)
            if canonical != project["url"]:
                self._save_project(canonical, created=bool(project.get("created", False)))
            return canonical

        # Project list and New project only exist on the home page.
        if urllib.parse.urlsplit(self.ui.page_facts().href).path not in ("", "/"):
            self.ui.open_project(FLOW_PAGE_URL)
        links, _ = self.ui.list_projects()
        for link in links:
            if link.title.strip() == PROJECT_TITLE:
                canonical = self._canonical_project_url(link.href)
                self.ui.open_project(canonical)
                self._save_project(canonical, created=False)
                return canonical

        canonical = self._canonical_project_url(self.ui.new_project())
        self._save_project(canonical, created=True)
        self.ui.open_project(canonical)
        return canonical

    def _load_job(self, job_id: str) -> dict[str, Any]:
        """Raise ProviderError('Missing job record for {job_id}') when absent or not a dict."""
        record = self.cache.read_job_record(self.name, job_id)
        if not isinstance(record, dict):
            raise ProviderError(f"Missing job record for {job_id}", provider=self.name)
        return record

    def submit(self, prompt: str, aspect: Aspect, seconds: int, seed: int | None) -> str:
        """Send the prompt, approve within the credit cap, return job_id.

        seconds and seed are accepted for the VideoProvider signature and are not sent.
        The Flow UI clip length is fixed at 8s. seed is not representable in the agent panel.
        """
        with self._lock:
            message = compose_user_message(prompt, aspect.value)
            project_url = self._ensure_project()
            # Flow has reverted this to Never on its own; without it no card, no cap.
            self.ui.ensure_confirm_always()
            baseline = self.ui.snapshot_media().uuids
            baseline_tiles = self.ui.tile_count()
            if self.ui.approval_text():
                # A stale card (e.g. left by a quota stop) would be approved instead of ours.
                raise ProviderError(
                    "Flow project has a pending approval card; answer it in Flow first",
                    provider=self.name,
                )
            self.ui.send_prompt(message)

            def _pred() -> str | None:
                card = self.ui.approval_text()
                if card:
                    return card
                decision_text = self.ui.read_decision_text(message)
                kind = classify_reply(decision_text)
                if kind in (ReplyKind.APPROVAL, ReplyKind.REFUSAL, ReplyKind.ERROR):
                    return decision_text
                if kind == ReplyKind.PROGRESS:
                    return _PROGRESS_SENTINEL
                return None

            reply_text = self.ui.wait_until("agent_reply", _pred, timeout=REPLY_TIMEOUT_SECONDS)

            if reply_text == _PROGRESS_SENTINEL:
                job_id = "flow-" + uuid.uuid4().hex
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                rec = {
                    "project_url": project_url,
                    "baseline": sorted(baseline),
                    "baseline_tiles": baseline_tiles,
                    "credits": 0,
                    "model": None,
                    "approved_at": now_iso,
                    "prompt_chars": len(prompt),
                    "media_uuid": None,
                    "media_url": None,
                    "phase": "approved",
                    "message_head": message[:80],
                }
                self.cache.write_job_record(self.name, job_id, rec)
                raise ProviderUnconfirmedJobError(
                    "Flow started without approval card; set Confirm before generating to Always",
                    job_id=job_id,
                    provider=self.name,
                )

            card_present = bool(self.ui.approval_text())
            kind = classify_reply(reply_text)
            if card_present or kind == ReplyKind.APPROVAL:
                info = parse_approval(reply_text)
                if info is None or info.credits <= 0:
                    raise ProviderError("approval card has no credit cost", provider=self.name)
                if info.credits > self.max_credits_per_clip or not self.auto_approve:
                    raise ProviderQuotaError(
                        f"needs {info.credits} credits, limit {self.max_credits_per_clip}",
                        provider=self.name,
                    )

                job_id = "flow-" + uuid.uuid4().hex
                record = {
                    "project_url": project_url,
                    "baseline": sorted(baseline),
                    "baseline_tiles": baseline_tiles,
                    "credits": info.credits,
                    "model": info.model,
                    "approved_at": "",
                    "prompt_chars": len(prompt),
                    "media_uuid": None,
                    "media_url": None,
                    "phase": "approving",
                    "message_head": message[:80],
                }
                self.cache.write_job_record(self.name, job_id, record)

                try:
                    self.ui.approve()
                except ProviderError as exc:
                    if "no_approve" in str(exc):
                        self.cache.job_record_path(self.name, job_id).unlink(missing_ok=True)
                        raise
                    # Covers ProviderTimeoutError too: click may have landed.
                    raise ProviderUnconfirmedJobError(
                        "approve not confirmed", job_id=job_id, provider=self.name
                    ) from exc

                record["phase"] = "approved"
                record["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self.cache.write_job_record(self.name, job_id, record)
                logger.info(
                    "flow submit approved credits=%s model=%s prompt_chars=%s",
                    info.credits,
                    info.model,
                    len(prompt),
                )
                return job_id

            if kind == ReplyKind.REFUSAL:
                raise ProviderPolicyError(
                    f"Flow refused the prompt: {redact(reply_text[-MAX_ERROR_TAIL:])}",
                    provider=self.name,
                )

            if kind == ReplyKind.ERROR:
                raise ProviderError(
                    f"Flow UI changed: agent_error: {redact(reply_text[-MAX_ERROR_TAIL:])}",
                    provider=self.name,
                )

            raise ProviderError(
                f"Flow UI changed: unexpected_reply: {redact(reply_text[-MAX_ERROR_TAIL:])}",
                provider=self.name,
            )

    def poll(self, job_id: str) -> JobStatus:
        """Look for a new video uuid. RUNNING until one appears or the agent reports failure."""
        with self._lock:
            record = self._load_job(job_id)
            if record.get("phase") == "succeeded" and record.get("media_uuid"):
                return JobStatus(state=JobState.SUCCEEDED, job_id=job_id)
            if record.get("phase") == "failed":
                return JobStatus(
                    state=JobState.FAILED,
                    job_id=job_id,
                    error=record.get("error"),
                )

            try:
                facts = self.ui.page_facts()
                proj_url = str(record["project_url"])
                proj_no_q = urllib.parse.urlsplit(proj_url)._replace(query="", fragment="").geturl()
                curr_no_q = (
                    urllib.parse.urlsplit(facts.href)._replace(query="", fragment="").geturl()
                )
                if not curr_no_q.startswith(proj_no_q):
                    self.ui.open_project(proj_url)

                baseline_uuids = frozenset(record.get("baseline", []))
                found = self.ui.find_new_media(baseline_uuids)
                signed_url = self.ui.latest_signed_video_url(found) if found else ""
                baseline_tiles = record.get("baseline_tiles")
                if (
                    not signed_url
                    and isinstance(baseline_tiles, int)
                    and self.ui.tile_count() > baseline_tiles
                ):
                    # Thumbnails hide the video URL (the uuid may show only via the poster
                    # image); the newest tile's player loads the signed video URL.
                    tile_url = self.ui.newest_tile_video_url(proj_url, baseline_uuids)
                    tile_uuid = uuid_from_media_url(tile_url) if tile_url else None
                    if tile_uuid is not None and (found is None or tile_uuid == found):
                        found, signed_url = tile_uuid, tile_url

                if found is not None:
                    if signed_url and uuid_from_media_url(signed_url) == found:
                        record["media_uuid"] = found
                        record["media_url"] = signed_url
                        record["phase"] = "succeeded"
                        self.cache.write_job_record(self.name, job_id, record)
                        logger.info(
                            "flow poll succeeded media_url=%s", redact_media_url(signed_url)
                        )
                        return JobStatus(state=JobState.SUCCEEDED, job_id=job_id)

                    record["media_uuid"] = found
                    record["media_url"] = f"https://flow-content.google/video/{found}"
                    self.cache.write_job_record(self.name, job_id, record)
                    return JobStatus(state=JobState.RUNNING, job_id=job_id)

                message_head = str(record.get("message_head", ""))
                err_text = self.ui.agent_error_text(message_head)
                if err_text:
                    kind = classify_reply(err_text)
                    tail = redact(err_text[-MAX_ERROR_TAIL:])
                    if kind == ReplyKind.REFUSAL:
                        record["phase"] = "failed"
                        record["error"] = tail
                        self.cache.write_job_record(self.name, job_id, record)
                        raise ProviderPolicyError(
                            f"Flow refused the prompt: {tail}", provider=self.name
                        )
                    if kind == ReplyKind.ERROR:
                        record["phase"] = "failed"
                        record["error"] = tail
                        self.cache.write_job_record(self.name, job_id, record)
                        return JobStatus(state=JobState.FAILED, job_id=job_id, error=tail)

                lines = self.ui.progress_lines()
                if lines:
                    logger.info("flow poll progress %s", lines[:3])
                return JobStatus(state=JobState.RUNNING, job_id=job_id)

            except ProviderPolicyError:
                raise
            except ProviderError as exc:
                # Credits are already spent; never let the pipeline resubmit.
                if record.get("phase") in ("approved", "approving"):
                    raise ProviderUnconfirmedJobError(
                        "poll could not confirm the job", job_id=job_id, provider=self.name
                    ) from exc
                raise

    def download(self, job_id: str, dest: Path) -> Path:
        """Download the signed URL, fall back to in-page fetch, validate with ffprobe."""
        with self._lock:
            record = self._load_job(job_id)
            media_url = record.get("media_url")
            if not media_url or not isinstance(media_url, str):
                raise ProviderError(f"Job {job_id} has no media url", provider=self.name)

            try:
                self.download_fn(
                    media_url,
                    dest,
                    allow_hosts=self.allow_hosts,
                    timeout=DOWNLOAD_FETCH_TIMEOUT_SECONDS,
                )
            except ProviderError as exc:
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    data = self.ui.fetch_media_base64(media_url)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    part = dest.with_suffix(dest.suffix + ".part")
                    try:
                        part.write_bytes(data)
                        os.replace(part, dest)
                    finally:
                        if part.exists():
                            with contextlib.suppress(OSError):
                                part.unlink()
                else:
                    raise

            valid = False
            try:
                dur = self.probe_duration_fn(self.ffmpeg_bin, dest)
                has_video = self.probe_has_video_fn(self.ffmpeg_bin, dest)
                valid = (dur > 0) and (has_video is True)
            except (ProviderError, Short2ShortError):
                valid = False

            if not valid:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
                raise ProviderError("downloaded media failed validation", provider=self.name)

            logger.info("flow download validated dest=%s url=%s", dest, redact_media_url(media_url))
            return dest


def build_flow_provider(config: AppConfig, cache: ClipCache) -> FlowProvider:
    """Connect to the logged-in Chrome and return a provider. Signature is stable."""
    port = read_port(config)
    if port is None:
        raise ProviderAuthError(
            "Chrome CDP port not found. Run `short2short flow login --i-understand`",
            provider="flow",
        )

    try:
        page = CdpPage.connect(port)
    except ProviderAuthError:
        raise
    except (OSError, httpx.HTTPError, TimeoutError, websocket.WebSocketException) as exc:
        raise ProviderAuthError(
            "Failed to connect to Chrome CDP. Run `short2short flow login --i-understand`",
            provider="flow",
        ) from exc

    ui = FlowUi(page)
    return FlowProvider(
        ui,
        cache,
        max_credits_per_clip=config.flow_max_credits_per_clip,
        auto_approve=config.flow_auto_approve,
        ffmpeg_bin=config.ffmpeg_path,
        allow_hosts=set(config.download_allow_hosts),
        poll_interval=config.poll_interval_seconds,
        poll_timeout=config.poll_timeout_seconds,
    )
