"""Tests for FlowProvider."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from short2short.config import AppConfig
from short2short.core.models import Aspect, JobState
from short2short.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderPolicyError,
    ProviderQuotaError,
    ProviderTimeoutError,
    ProviderUnconfirmedJobError,
)
from short2short.providers.base import VideoProvider
from short2short.providers.flow.provider import (
    FlowProvider,
    build_flow_provider,
    media_has_video_stream,
)
from short2short.providers.flow.ui import MediaSnapshot, PageFacts, ProjectLink
from short2short.storage.cache import ClipCache


class FakeUi:
    """Mock FlowUi recording calls and providing controlled responses."""

    def __init__(self) -> None:
        self.current_href: str = "https://flow.google.com/"
        self.stale_card: str = ""
        self.awaiting_answer: bool = False
        self.projects: list[ProjectLink] = [
            ProjectLink(
                "short2short",
                "https://flow.google.com/project/11111111-1111-1111-1111-111111111111",
            )
        ]
        self.new_project_visible: bool = True
        self.opened_urls: list[str] = []
        self.new_project_called: int = 0
        self.baseline_uuids: frozenset[str] = frozenset()
        self.sent_messages: list[str] = []
        self.decision_text: str = "Approve\n10 credits\nVeo 3.1 - Lite"
        self.approval_text_val: str | None = None
        self.approved: bool = False
        self.approve_exc: Exception | None = None
        self.open_project_exc: Exception | None = None
        self.signed_video_url: str = (
            "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=secret"
        )
        self.error_text: str = ""
        self.progress_lines_list: list[str] = []
        self.media_bytes: bytes = b"fake video bytes"
        self.find_media_sequence: list[str | None] = []
        self.tiles: int = 0
        self.tile_video_url: str = ""
        self.calls: list[str] = []
        self.depth: int = 0
        self.max_depth: int = 0

    def _enter(self, name: str) -> None:
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.calls.append(name)

    def _exit(self) -> None:
        self.depth -= 1

    def page_facts(self) -> PageFacts:
        self._enter("page_facts")
        try:
            return PageFacts(
                href=self.current_href,
                host="flow.google.com",
                logged_in=True,
                composer=True,
            )
        finally:
            self._exit()

    def list_projects(self) -> tuple[list[ProjectLink], bool]:
        self._enter("list_projects")
        try:
            return self.projects, self.new_project_visible
        finally:
            self._exit()

    def open_project(self, url: str) -> str:
        self._enter("open_project")
        try:
            self.opened_urls.append(url)
            if self.open_project_exc:
                raise self.open_project_exc
            self.current_href = url
            return url
        finally:
            self._exit()

    def new_project(self) -> str:
        self._enter("new_project")
        try:
            self.new_project_called += 1
            self.current_href = (
                "https://flow.google.com/project/33333333-3333-3333-3333-333333333333"
            )
            return self.current_href
        finally:
            self._exit()

    def snapshot_media(self) -> MediaSnapshot:
        self._enter("snapshot_media")
        try:
            return MediaSnapshot(uuids=self.baseline_uuids, latest_video_url="")
        finally:
            self._exit()

    def send_prompt(self, message: str) -> None:
        self._enter("send_prompt")
        try:
            self.sent_messages.append(message)
            self.awaiting_answer = True
        finally:
            self._exit()

    def read_decision_text(self, message: str) -> str:
        self._enter("read_decision_text")
        try:
            return self.decision_text
        finally:
            self._exit()

    def approval_text(self) -> str:
        self._enter("approval_text")
        try:
            if self.stale_card:
                return self.stale_card
            if not self.awaiting_answer:
                return ""
            if self.approval_text_val is not None:
                return self.approval_text_val
            if "Approve" in self.decision_text:
                return self.decision_text
            return ""
        finally:
            self._exit()

    def approve(self) -> None:
        self._enter("approve")
        try:
            if self.approve_exc:
                raise self.approve_exc
            self.approved = True
            self.awaiting_answer = False
        finally:
            self._exit()

    def wait_until(
        self,
        step: str,
        predicate: Callable[[], Any],
        *,
        timeout: float,
    ) -> Any:
        val = predicate()
        if val is not None:
            return val
        raise ProviderTimeoutError(f"Flow UI changed: {step}: timed out", provider="flow")

    def find_new_media(self, baseline: frozenset[str]) -> str | None:
        self._enter("find_new_media")
        try:
            if self.find_media_sequence:
                return self.find_media_sequence.pop(0)
            return None
        finally:
            self._exit()

    def ensure_confirm_always(self) -> bool:
        self._enter("ensure_confirm_always")
        try:
            return False
        finally:
            self._exit()

    def tile_count(self) -> int:
        self._enter("tile_count")
        try:
            return self.tiles
        finally:
            self._exit()

    def newest_tile_video_url(self, project_url: str, baseline: frozenset[str]) -> str:
        self._enter("newest_tile_video_url")
        try:
            return self.tile_video_url
        finally:
            self._exit()

    def latest_signed_video_url(self, uuid: str = "") -> str:
        self._enter("latest_signed_video_url")
        try:
            return self.signed_video_url
        finally:
            self._exit()

    def agent_error_text(self, message: str) -> str:
        self._enter("agent_error_text")
        try:
            return self.error_text
        finally:
            self._exit()

    def progress_lines(self) -> list[str]:
        self._enter("progress_lines")
        try:
            return self.progress_lines_list
        finally:
            self._exit()

    def fetch_media_base64(self, url: str) -> bytes:
        self._enter("fetch_media_base64")
        try:
            return self.media_bytes
        finally:
            self._exit()


def test_flow_provider_protocol(tmp_path: Path) -> None:
    """FlowProvider complies with VideoProvider protocol and attributes."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    assert isinstance(provider, VideoProvider)
    assert provider.name == "flow"
    assert provider.paid is True
    assert provider.max_clip_seconds == 8
    assert provider.poll_interval_seconds == 5.0
    assert provider.poll_timeout_seconds == 600.0


def test_submit_writes_record(tmp_path: Path) -> None:
    """submit writes job record, preserves quotes in prompt, and omits seed."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    prompt = 'a "futuristic" city at night'
    job_id = provider.submit(prompt, Aspect.PORTRAIT, 8, 42)

    assert job_id.startswith("flow-")
    assert len(job_id) == 37

    assert len(ui.sent_messages) == 1
    sent = ui.sent_messages[0]
    assert prompt in sent
    assert "Aspect: vertical 9:16. One video, no other changes." in sent
    assert "42" not in sent

    assert ui.approved is True
    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert (
        record["project_url"]
        == "https://flow.google.com/project/11111111-1111-1111-1111-111111111111"
    )
    assert record["credits"] == 10
    assert record["model"] == "Veo 3.1 - Lite"
    assert record["phase"] == "approved"
    assert record["approved_at"] != ""
    assert record["prompt_chars"] == len(prompt)
    assert record["message_head"] == sent[:80]
    assert record["baseline"] == []


def test_submit_credits_exceed_cap(tmp_path: Path) -> None:
    """submit raises ProviderQuotaError when card credits exceed cap."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.decision_text = "Approve\n11 credits\nVeo 3.1 - Lite"
    provider = FlowProvider(ui, cache, max_credits_per_clip=10)

    with pytest.raises(ProviderQuotaError, match=r"needs 11 credits, limit 10"):
        provider.submit("sunset", Aspect.LANDSCAPE, 8, None)

    assert ui.approved is False
    jobs_dir = tmp_path / "jobs" / "flow"
    if jobs_dir.is_dir():
        records = [p for p in jobs_dir.iterdir() if p.name != "_project.json"]
        assert len(records) == 0


def test_submit_auto_approve_false(tmp_path: Path) -> None:
    """submit raises ProviderQuotaError when auto_approve is False."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache, auto_approve=False)

    with pytest.raises(ProviderQuotaError, match=r"needs 10 credits, limit 10"):
        provider.submit("sunset", Aspect.LANDSCAPE, 8, None)

    assert ui.approved is False


def test_submit_no_credit_cost(tmp_path: Path) -> None:
    """submit raises ProviderError when approval card has no credit cost."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.decision_text = "Approve please"
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderError, match="approval card has no credit cost"):
        provider.submit("sunset", Aspect.LANDSCAPE, 8, None)

    assert ui.approved is False


def test_submit_zero_credits_rejected(tmp_path: Path) -> None:
    """submit raises ProviderError when card states 0 credits."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.decision_text = "Approve 0 credits"
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderError, match="approval card has no credit cost"):
        provider.submit("sunset", Aspect.LANDSCAPE, 8, None)

    assert ui.approved is False


def test_submit_agent_refusal(tmp_path: Path) -> None:
    """submit raises ProviderPolicyError on agent refusal."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.decision_text = "I can't generate that request"
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderPolicyError, match="Flow refused the prompt"):
        provider.submit("violent scene", Aspect.PORTRAIT, 8, None)

    assert ui.approved is False


def test_submit_agent_error(tmp_path: Path) -> None:
    """submit raises ProviderError on agent error message."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.decision_text = "Something went wrong. Try again."
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderError, match="agent_error"):
        provider.submit("storm", Aspect.PORTRAIT, 8, None)

    assert ui.approved is False


def test_submit_unknown_reply_times_out(tmp_path: Path) -> None:
    """submit times out on unexpected reply without approval card."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.decision_text = "hello"
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderTimeoutError, match="timed out"):
        provider.submit("ocean", Aspect.PORTRAIT, 8, None)

    assert ui.approved is False


def test_submit_empty_prompt_rejected(tmp_path: Path) -> None:
    """submit rejects empty prompt before CDP calls."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderError, match="Prompt cannot be empty"):
        provider.submit("   ", Aspect.PORTRAIT, 8, None)

    assert len(ui.sent_messages) == 0


def test_submit_progress_without_card_unconfirmed(tmp_path: Path) -> None:
    """submit writes record and raises ProviderUnconfirmedJobError on PROGRESS reply."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.decision_text = "generating media."
    provider = FlowProvider(ui, cache)

    with pytest.raises(
        ProviderUnconfirmedJobError, match="Flow started without approval card"
    ) as exc_info:
        provider.submit("ocean waves", Aspect.PORTRAIT, 8, None)

    job_id = exc_info.value.job_id
    assert job_id is not None
    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert record["phase"] == "approved"
    assert record["credits"] == 0
    assert record["model"] is None


def test_submit_approve_still_present_unconfirmed(tmp_path: Path) -> None:
    """approve raising still present writes record phase approving and raises unconfirmed."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.approve_exc = ProviderError("Flow UI changed: approve: still present", provider="flow")
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderUnconfirmedJobError, match="approve not confirmed") as exc_info:
        provider.submit("sunset", Aspect.PORTRAIT, 8, None)

    job_id = exc_info.value.job_id
    assert job_id is not None
    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert record["phase"] == "approving"


def test_submit_approve_no_approve_record_removed(tmp_path: Path) -> None:
    """approve raising no_approve removes job record and re-raises ProviderError."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.approve_exc = ProviderError("Flow UI changed: approve: no_approve", provider="flow")
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderError, match="no_approve"):
        provider.submit("sunset", Aspect.PORTRAIT, 8, None)

    jobs_dir = tmp_path / "jobs" / "flow"
    if jobs_dir.is_dir():
        records = [p for p in jobs_dir.iterdir() if p.name != "_project.json"]
        assert len(records) == 0


def test_submit_approve_timeout_unconfirmed(tmp_path: Path) -> None:
    """approve timing out keeps record and raises ProviderUnconfirmedJobError."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.approve_exc = ProviderTimeoutError("Flow UI changed: approve: timed out", provider="flow")
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderUnconfirmedJobError, match="approve not confirmed") as exc_info:
        provider.submit("mountain", Aspect.PORTRAIT, 8, None)

    job_id = exc_info.value.job_id
    assert job_id is not None
    assert cache.read_job_record("flow", job_id) is not None


def test_poll_running_then_succeeded(tmp_path: Path) -> None:
    """poll returns RUNNING until media uuid appears, then SUCCEEDED."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    job_id = provider.submit("beach", Aspect.LANDSCAPE, 8, None)
    uuid_val = "11111111-2222-3333-4444-555555555555"
    ui.find_media_sequence = [None, uuid_val]

    st1 = provider.poll(job_id)
    assert st1.state == JobState.RUNNING

    st2 = provider.poll(job_id)
    assert st2.state == JobState.SUCCEEDED
    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert record["phase"] == "succeeded"
    assert record["media_uuid"] == uuid_val
    assert "sig=secret" in str(record["media_url"])

    # Subsequent poll returns SUCCEEDED without touching find_new_media
    ui.find_media_sequence = [None]
    st3 = provider.poll(job_id)
    assert st3.state == JobState.SUCCEEDED
    assert len(ui.find_media_sequence) == 1


def test_poll_reads_uuid_from_new_tile(tmp_path: Path) -> None:
    """Opaque thumbnails: a new tile is opened to read the signed video URL."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.tiles = 2
    provider = FlowProvider(ui, cache)

    job_id = provider.submit("beach", Aspect.LANDSCAPE, 8, None)
    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert record["baseline_tiles"] == 2

    st1 = provider.poll(job_id)
    assert st1.state == JobState.RUNNING
    assert "newest_tile_video_url" not in ui.calls

    ui.tiles = 3
    ui.tile_video_url = (
        "https://flow-content.google/video/22222222-3333-4444-5555-666666666666?sig=x"
    )
    st2 = provider.poll(job_id)
    assert st2.state == JobState.SUCCEEDED
    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert record["media_uuid"] == "22222222-3333-4444-5555-666666666666"


def test_poll_poster_uuid_without_video_url_opens_tile(tmp_path: Path) -> None:
    """uuid seen only via the poster image: the tile player supplies the signed URL."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.tiles = 1
    provider = FlowProvider(ui, cache)

    job_id = provider.submit("beach", Aspect.LANDSCAPE, 8, None)
    new = "22222222-3333-4444-5555-666666666666"
    ui.tiles = 2
    ui.find_media_sequence = [new]
    ui.signed_video_url = ""
    ui.tile_video_url = f"https://flow-content.google/video/{new}?sig=x"
    st = provider.poll(job_id)
    assert st.state == JobState.SUCCEEDED


def test_poll_signed_url_mismatch_returns_running(tmp_path: Path) -> None:
    """poll returns RUNNING when performance video url uuid does not match found uuid."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    job_id = provider.submit("beach", Aspect.LANDSCAPE, 8, None)
    ui.find_media_sequence = ["11111111-2222-3333-4444-555555555555"]
    ui.signed_video_url = (
        "https://flow-content.google/video/22222222-3333-4444-5555-666666666666?sig=x"
    )

    st = provider.poll(job_id)
    assert st.state == JobState.RUNNING


def test_poll_agent_refusal(tmp_path: Path) -> None:
    """poll propagates policy error when agent reports refusal."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    job_id = provider.submit("forest", Aspect.PORTRAIT, 8, None)
    ui.error_text = "I can't generate that request"

    with pytest.raises(ProviderPolicyError, match="Flow refused the prompt"):
        provider.poll(job_id)

    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert record["phase"] == "failed"


def test_poll_agent_error(tmp_path: Path) -> None:
    """poll returns FAILED JobStatus when agent reports error."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    job_id = provider.submit("forest", Aspect.PORTRAIT, 8, None)
    ui.error_text = "Something went wrong. Try again."

    st = provider.poll(job_id)
    assert st.state == JobState.FAILED
    assert "Something went wrong" in str(st.error)

    record = cache.read_job_record("flow", job_id)
    assert record is not None
    assert record["phase"] == "failed"


def test_poll_timeout_unconfirmed_job(tmp_path: Path) -> None:
    """CDP timeout during poll after phase approved raises ProviderUnconfirmedJobError."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    job_id = provider.submit("city", Aspect.PORTRAIT, 8, None)
    ui.current_href = "https://flow.google.com/"
    ui.open_project_exc = ProviderTimeoutError(
        "Flow UI changed: open_project: timed out", provider="flow"
    )

    with pytest.raises(
        ProviderUnconfirmedJobError, match="poll could not confirm the job"
    ) as exc_info:
        provider.poll(job_id)

    assert exc_info.value.job_id == job_id
    assert cache.read_job_record("flow", job_id) is not None


def test_poll_missing_record(tmp_path: Path) -> None:
    """poll raises ProviderError on unknown job ID."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    with pytest.raises(ProviderError, match="Missing job record"):
        provider.poll("flow-nonexistent")


def test_download_success(tmp_path: Path) -> None:
    """download writes media, validates duration and video stream, returns dest."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()

    def fake_download(url: str, dest: Path, *, allow_hosts: set[str], timeout: float) -> None:
        dest.write_bytes(b"01234567")

    provider = FlowProvider(
        ui,
        cache,
        download_fn=fake_download,
        probe_duration_fn=lambda bin_ff, p: 2.5,
        probe_has_video_fn=lambda bin_ff, p: True,
    )

    job_id = "flow-testdownload0001"
    cache.write_job_record(
        "flow",
        job_id,
        {
            "media_url": "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=1"
        },
    )
    dest = tmp_path / "out.mp4"
    res = provider.download(job_id, dest)

    assert res == dest
    assert dest.read_bytes() == b"01234567"


def test_download_fallback_http_403(tmp_path: Path) -> None:
    """download falls back to fetch_media_base64 on HTTP 403."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.media_bytes = b"fallback video content"

    def fake_download(url: str, dest: Path, *, allow_hosts: set[str], timeout: float) -> None:
        raise ProviderError("HTTP 403 downloading https://flow-content.google/video/x")

    provider = FlowProvider(
        ui,
        cache,
        download_fn=fake_download,
        probe_duration_fn=lambda bin_ff, p: 1.0,
        probe_has_video_fn=lambda bin_ff, p: True,
    )

    job_id = "flow-testdownload0002"
    cache.write_job_record(
        "flow",
        job_id,
        {
            "media_url": "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=1"
        },
    )
    dest = tmp_path / "out.mp4"
    res = provider.download(job_id, dest)

    assert res == dest
    assert dest.read_bytes() == b"fallback video content"


def test_download_fallback_http_401(tmp_path: Path) -> None:
    """download falls back to fetch_media_base64 on HTTP 401."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.media_bytes = b"fallback 401 content"

    def fake_download(url: str, dest: Path, *, allow_hosts: set[str], timeout: float) -> None:
        raise ProviderError("HTTP 401 downloading https://flow-content.google/video/x")

    provider = FlowProvider(
        ui,
        cache,
        download_fn=fake_download,
        probe_duration_fn=lambda bin_ff, p: 1.0,
        probe_has_video_fn=lambda bin_ff, p: True,
    )

    job_id = "flow-testdownload0003"
    cache.write_job_record(
        "flow",
        job_id,
        {
            "media_url": "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=1"
        },
    )
    dest = tmp_path / "out.mp4"
    res = provider.download(job_id, dest)

    assert res == dest
    assert dest.read_bytes() == b"fallback 401 content"


def test_download_other_http_error(tmp_path: Path) -> None:
    """download does not fall back on HTTP 404 and propagates ProviderError."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()

    def fake_download(url: str, dest: Path, *, allow_hosts: set[str], timeout: float) -> None:
        raise ProviderError("HTTP 404 downloading https://flow-content.google/video/x")

    provider = FlowProvider(ui, cache, download_fn=fake_download)

    job_id = "flow-testdownload0004"
    cache.write_job_record(
        "flow",
        job_id,
        {
            "media_url": "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=1"
        },
    )
    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError, match="HTTP 404"):
        provider.download(job_id, dest)


def test_download_validation_duration_zero(tmp_path: Path) -> None:
    """download unlinks dest and raises ProviderError when duration probe is zero."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()

    def fake_download(url: str, dest: Path, *, allow_hosts: set[str], timeout: float) -> None:
        dest.write_bytes(b"empty")

    provider = FlowProvider(
        ui,
        cache,
        download_fn=fake_download,
        probe_duration_fn=lambda bin_ff, p: 0.0,
        probe_has_video_fn=lambda bin_ff, p: True,
    )

    job_id = "flow-testdownload0005"
    cache.write_job_record(
        "flow",
        job_id,
        {
            "media_url": "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=1"
        },
    )
    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError, match="downloaded media failed validation"):
        provider.download(job_id, dest)

    assert not dest.exists()


def test_download_validation_has_video_false(tmp_path: Path) -> None:
    """download unlinks dest and raises ProviderError when video stream probe is False."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()

    def fake_download(url: str, dest: Path, *, allow_hosts: set[str], timeout: float) -> None:
        dest.write_bytes(b"audio only")

    provider = FlowProvider(
        ui,
        cache,
        download_fn=fake_download,
        probe_duration_fn=lambda bin_ff, p: 3.0,
        probe_has_video_fn=lambda bin_ff, p: False,
    )

    job_id = "flow-testdownload0006"
    cache.write_job_record(
        "flow",
        job_id,
        {
            "media_url": "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=1"
        },
    )
    dest = tmp_path / "out.mp4"
    with pytest.raises(ProviderError, match="downloaded media failed validation"):
        provider.download(job_id, dest)

    assert not dest.exists()


def test_download_missing_media_url(tmp_path: Path) -> None:
    """download raises ProviderError when job record has no media_url."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    job_id = "flow-testdownload0007"
    cache.write_job_record("flow", job_id, {"media_url": None})
    dest = tmp_path / "out.mp4"

    with pytest.raises(ProviderError, match="has no media url"):
        provider.download(job_id, dest)


def test_build_flow_provider_missing_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """build_flow_provider raises ProviderAuthError when CDP port file is missing."""
    cache = ClipCache(tmp_path)
    config = AppConfig()
    monkeypatch.setattr("short2short.providers.flow.provider.read_port", lambda cfg: None)

    with pytest.raises(ProviderAuthError, match="Chrome CDP port not found"):
        build_flow_provider(config, cache)


def test_build_flow_provider_connect_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """build_flow_provider raises ProviderAuthError on socket connection failure."""
    cache = ClipCache(tmp_path)
    config = AppConfig()
    monkeypatch.setattr("short2short.providers.flow.provider.read_port", lambda cfg: 9222)

    def _fail_connect(port: int) -> Any:
        raise OSError("Connection refused")

    monkeypatch.setattr("short2short.providers.flow.provider.CdpPage.connect", _fail_connect)

    with pytest.raises(ProviderAuthError, match="Failed to connect to Chrome CDP"):
        build_flow_provider(config, cache)


def test_build_flow_provider_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """build_flow_provider initializes FlowProvider when connect succeeds."""
    cache = ClipCache(tmp_path)
    config = AppConfig()
    monkeypatch.setattr("short2short.providers.flow.provider.read_port", lambda cfg: 9222)
    monkeypatch.setattr(
        "short2short.providers.flow.provider.CdpPage.connect", lambda port: object()
    )
    monkeypatch.setattr("short2short.providers.flow.provider.FlowUi", lambda page: FakeUi())

    p = build_flow_provider(config, cache)
    assert isinstance(p, FlowProvider)
    assert p.max_credits_per_clip == config.flow_max_credits_per_clip


def test_lock_serialization(tmp_path: Path) -> None:
    """Calls to submit are serialized by provider lock with max depth 1."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    provider.submit("first prompt", Aspect.PORTRAIT, 8, None)
    provider.submit("second prompt", Aspect.PORTRAIT, 8, None)

    assert ui.max_depth == 1


def test_project_reuse(tmp_path: Path) -> None:
    """Second submit reuses _project.json and does not call new_project."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    provider.submit("prompt 1", Aspect.PORTRAIT, 8, None)
    assert ui.new_project_called == 0
    assert "https://flow.google.com/project/11111111-1111-1111-1111-111111111111" in ui.opened_urls

    ui.opened_urls.clear()
    provider.submit("prompt 2", Aspect.PORTRAIT, 8, None)
    assert ui.new_project_called == 0
    assert ui.opened_urls == [
        "https://flow.google.com/project/11111111-1111-1111-1111-111111111111"
    ]


def test_ensure_project_matching_title(tmp_path: Path) -> None:
    """ensure_project selects existing link titled short2short."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.projects = [
        ProjectLink(
            "short2short", "https://flow.google.com/project/22222222-2222-2222-2222-222222222222"
        )
    ]
    provider = FlowProvider(ui, cache)

    provider.submit("prompt", Aspect.PORTRAIT, 8, None)
    assert ui.new_project_called == 0
    assert "https://flow.google.com/project/22222222-2222-2222-2222-222222222222" in ui.opened_urls


def test_ensure_project_new_project_when_no_match(tmp_path: Path) -> None:
    """ensure_project creates new project when no project matches."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    ui.projects = [
        ProjectLink(
            "other_project", "https://flow.google.com/project/44444444-4444-4444-4444-444444444444"
        )
    ]
    provider = FlowProvider(ui, cache)

    provider.submit("prompt", Aspect.PORTRAIT, 8, None)
    assert ui.new_project_called == 1
    record = cache.read_job_record("flow", "_project")
    assert record is not None
    assert record["created"] is True
    assert record["url"] == "https://flow.google.com/project/33333333-3333-3333-3333-333333333333"


def test_stored_project_url_host_rejected(tmp_path: Path) -> None:
    """_load_project rejects stored project URL from unauthorized host."""
    cache = ClipCache(tmp_path)
    ui = FakeUi()
    provider = FlowProvider(ui, cache)

    cache.write_job_record(
        "flow",
        "_project",
        {
            "title": "short2short",
            "url": "https://evil.com/project/11111111-1111-1111-1111-111111111111",
            "created": False,
        },
    )

    with pytest.raises(ProviderError, match="stored project url rejected"):
        provider.submit("prompt", Aspect.PORTRAIT, 8, None)


def test_media_has_video_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """media_has_video_stream detects video streams via ffprobe JSON."""
    test_file = tmp_path / "test.mp4"
    test_file.write_bytes(b"data")

    # Has video stream
    payload_video = json.dumps({"streams": [{"codec_type": "video"}]}).encode("utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=payload_video, stderr=b""
        ),
    )
    assert media_has_video_stream("ffmpeg", test_file) is True

    # No video stream (e.g. audio only)
    payload_audio = json.dumps({"streams": [{"codec_type": "audio"}]}).encode("utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=payload_audio, stderr=b""
        ),
    )
    assert media_has_video_stream("ffmpeg", test_file) is False

    # Failure: non-zero exit code
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=b"", stderr=b"error"
        ),
    )
    with pytest.raises(ProviderError, match="ffprobe rejected the download"):
        media_has_video_stream("ffmpeg", test_file)

    # Failure: timeout
    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=30)

    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(ProviderError, match="ffprobe rejected the download"):
        media_has_video_stream("ffmpeg", test_file)


def test_submit_refuses_when_stale_card_pending(tmp_path: Path) -> None:
    ui = FakeUi()
    ui.stale_card = "Approve\n10 credits\nVeo 3.1 - Lite"
    provider = FlowProvider(ui, ClipCache(tmp_path))
    with pytest.raises(ProviderError, match="pending approval card"):
        provider.submit("a cat", Aspect.PORTRAIT, 8, None)
    assert ui.sent_messages == []
    assert ui.approved is False


def test_stored_subpage_url_is_canonicalized(tmp_path: Path) -> None:
    """A stored /project/<id>/tools URL is reduced to the project page before opening."""
    cache = ClipCache(tmp_path)
    pid = "55555555-5555-5555-5555-555555555555"
    cache.write_job_record(
        "flow",
        "_project",
        {
            "title": "short2short",
            "url": f"https://flow.google.com/project/{pid}/tools",
            "created": True,
        },
    )
    ui = FakeUi()
    provider = FlowProvider(ui, cache)
    assert provider._ensure_project() == f"https://flow.google.com/project/{pid}"
    assert ui.opened_urls == [f"https://flow.google.com/project/{pid}"]
    rec = cache.read_job_record("flow", "_project")
    assert rec is not None and rec["url"] == f"https://flow.google.com/project/{pid}"
