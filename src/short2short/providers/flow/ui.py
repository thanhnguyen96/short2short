"""Drive the Flow page through a CDP session."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol, TypeVar, runtime_checkable
from uuid import uuid4

from short2short.errors import ProviderError, ProviderTimeoutError
from short2short.providers.flow.constants import (
    APPROVE_TIMEOUT_SECONDS,
    DOWNLOAD_FETCH_TIMEOUT_SECONDS,
    EVAL_TIMEOUT_SECONDS,
    NAV_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    SEND_CONFIRM_SECONDS,
)
from short2short.providers.flow.parse import (
    ReplyKind,
    classify_reply,
    new_media_uuids,
    uuid_from_media_url,
)
from short2short.providers.flow.ui_js import (
    JS_APPROVAL_TEXT,
    JS_APPROVE_STILL_THERE,
    JS_CLICK_APPROVE,
    JS_CLICK_SEND,
    JS_CLOSE_AGENT_SETTINGS,
    JS_COMPOSER_TEXT,
    JS_CONFIRM_SETTING,
    JS_FOCUS_COMPOSER,
    JS_LATEST_REPLY,
    JS_LOGGED_IN,
    JS_MEDIA_SNAPSHOT,
    JS_OPEN_AGENT_SETTINGS,
    JS_PROGRESS,
    JS_PROJECTS,
    JS_SET_CONFIRM_ALWAYS,
    JS_TILE_COUNT,
    JS_VISIBILITY,
    js_fetch_media_b64,
    js_media_url_from_performance,
    js_new_project,
    js_open_project,
    js_open_tile,
)
from short2short.security import redact

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _norm_url(url: str) -> str:
    """URL without query, fragment, or trailing slash, for page identity checks."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _text_after(reply: str, message: str) -> str:
    """Return reply text after the last echo of message (or its first 80 chars)."""
    if not reply:
        return ""
    for marker in (message, message[:80]):
        idx = reply.rfind(marker) if marker else -1
        if idx != -1:
            return reply[idx + len(marker) :].strip()
    return reply.strip()


@runtime_checkable
class CdpSession(Protocol):
    """Chrome DevTools session. evaluate returns the JSON string from the page."""

    def evaluate(self, expression: str, *, timeout: float = 60.0) -> str: ...

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PageFacts:
    """Basic state facts read from the page."""

    href: str
    host: str
    logged_in: bool
    composer: bool


@dataclass(frozen=True)
class ProjectLink:
    """Link to an existing Flow project."""

    title: str
    href: str


@dataclass(frozen=True)
class MediaSnapshot:
    """Uuids already on the project page plus the latest performance video URL."""

    uuids: frozenset[str]
    latest_video_url: str


class FlowUi:
    """User-visible Flow actions. One instance is not thread-safe; caller holds the lock."""

    def __init__(
        self,
        session: CdpSession,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.monotonic,
        screenshot_dir: Path | None = None,
    ) -> None:
        self.session = session
        self.sleep_fn = sleep_fn
        self.time_fn = time_fn
        self.screenshot_dir = screenshot_dir

    def _screenshot(self, step: str) -> None:
        """Best-effort Page.captureScreenshot into screenshot_dir. Never raises."""
        if self.screenshot_dir is None:
            return
        try:
            res = self.session.send("Page.captureScreenshot", {"format": "png"})
            data_str = res["result"]["data"]
            raw = base64.b64decode(data_str)
            clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", step)[:40] or "screenshot"
            filename = f"flow-{clean_step}-{uuid4().hex[:8]}.png"
            out_path = self.screenshot_dir / filename
            out_path.write_bytes(raw)
        except (OSError, KeyError, ValueError, binascii.Error) as exc:
            logger.warning("flow screenshot failed for %s: %s", step, exc)

    def _fail(self, step: str, reason: str) -> NoReturn:
        """Screenshot, then raise ProviderError('Flow UI changed: {step}: {reason}')."""
        self._screenshot(step)
        raise ProviderError(f"Flow UI changed: {step}: {reason}", provider="flow")

    def _eval_json(self, expression: str, *, timeout: float, step: str) -> dict[str, Any]:
        """evaluate + json.loads. Failures become ProviderError prefixed with the step."""
        try:
            raw = self.session.evaluate(expression, timeout=timeout)
        except ProviderTimeoutError:
            self._screenshot(step)
            raise
        except TimeoutError as exc:
            self._screenshot(step)
            raise ProviderTimeoutError(
                f"Flow UI changed: {step}: timed out", provider="flow"
            ) from exc
        except (ProviderError, OSError) as exc:
            self._screenshot(step)
            raise ProviderError(
                f"Flow UI changed: {step}: {redact(str(exc))}", provider="flow"
            ) from exc

        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raw_len = len(raw) if isinstance(raw, str) else 0
            logger.warning("flow ui %s returned non-json (%d chars)", step, raw_len)
            self._screenshot(step)
            raise ProviderError(f"Flow UI changed: {step}: bad json", provider="flow") from exc

        if not isinstance(parsed, dict):
            raw_len = len(raw) if isinstance(raw, str) else 0
            logger.warning("flow ui %s returned non-json (%d chars)", step, raw_len)
            self._screenshot(step)
            raise ProviderError(f"Flow UI changed: {step}: bad json", provider="flow")

        return parsed

    def focus_composer(self) -> None:
        """Focus the composer, waiting for the agent panel to render. Raises if it never does."""

        def _try() -> dict[str, Any] | None:
            res = self._eval_json(
                JS_FOCUS_COMPOSER, timeout=EVAL_TIMEOUT_SECONDS, step="focus_composer"
            )
            return res if res.get("ok") else None

        try:
            self.wait_until("focus_composer", _try, timeout=NAV_TIMEOUT_SECONDS)
        except ProviderTimeoutError as exc:
            raise ProviderError(
                "Flow UI changed: focus_composer: no_composer", provider="flow"
            ) from exc

    def insert_text(self, text: str) -> None:
        """Input.insertText. Does not focus. Empty text raises ProviderError."""
        if not text:
            self._fail("insert_text", "empty text")
        res = self.session.send("Input.insertText", {"text": text})
        if not isinstance(res, dict) or "error" in res:
            self._fail("insert_text", "cdp rejected insertText")

    def press_enter(self) -> None:
        """Enter keyDown then keyUp. Does not click the send button."""
        self.session.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "text": "\r",
            },
        )
        self.session.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
            },
        )

    def page_facts(self) -> PageFacts:
        """Read host, href, composer, logged_in. Does not navigate."""
        res = self._eval_json(JS_LOGGED_IN, timeout=EVAL_TIMEOUT_SECONDS, step="page_facts")
        for k in ("href", "host", "logged_in", "composer"):
            if k not in res:
                self._fail("page_facts", "missing keys")
        return PageFacts(
            href=str(res["href"]),
            host=str(res["host"]),
            logged_in=bool(res["logged_in"]),
            composer=bool(res["composer"]),
        )

    def list_projects(self) -> tuple[list[ProjectLink], bool]:
        """Return (links, new_project_visible). hrefs that fail the flow host check are dropped."""
        res = self._eval_json(JS_PROJECTS, timeout=EVAL_TIMEOUT_SECONDS, step="list_projects")
        raw_projects = res.get("projects", [])
        links: list[ProjectLink] = []
        if isinstance(raw_projects, list):
            for item in raw_projects:
                if not isinstance(item, dict):
                    continue
                href = str(item.get("href", ""))
                parsed = urllib.parse.urlsplit(href)
                if parsed.hostname != "flow.google.com":
                    continue
                title = str(item.get("text", "")).strip()
                links.append(ProjectLink(title=title, href=href))
        new_project_visible = bool(res.get("new_project", False))
        return links, new_project_visible

    def open_project(self, url: str) -> str:
        """Click or assign the project URL. Return final location.href."""
        expr = js_open_project(url)
        deadline = self.time_fn() + NAV_TIMEOUT_SECONDS
        try:
            res = self._eval_json(expr, timeout=NAV_TIMEOUT_SECONDS, step="open_project")
        except ProviderTimeoutError:
            raise
        except ProviderError:
            # location.assign can destroy the execution context before the snippet returns.
            res = {"ok": True}
        if not res.get("ok"):
            self._fail("open_project", str(res.get("reason", "failed")))
        target = _norm_url(url)
        current_href = self.page_facts().href
        if _norm_url(current_href) == target:
            return current_href
        while self.time_fn() < deadline:
            self.sleep_fn(POLL_INTERVAL_SECONDS)
            current_href = self.page_facts().href
            if _norm_url(current_href) == target:
                return current_href
        self._screenshot("open_project")
        raise ProviderTimeoutError("Flow UI changed: open_project: timed out", provider="flow")

    def new_project(self) -> str:
        """Click New project. Return final location.href. Raises if the control is missing."""
        # Home page renders the control after navigation; wait for it instead of failing.
        self.wait_until(
            "new_project_visible",
            lambda: True if self.list_projects()[1] else None,
            timeout=NAV_TIMEOUT_SECONDS,
        )
        before_href = self.page_facts().href
        deadline = self.time_fn() + NAV_TIMEOUT_SECONDS
        res = self._eval_json(js_new_project(), timeout=NAV_TIMEOUT_SECONDS, step="new_project")
        if not res.get("ok"):
            self._fail("new_project", str(res.get("reason", "failed")))
        current_href = self.page_facts().href
        if current_href != before_href:
            return current_href
        while self.time_fn() < deadline:
            self.sleep_fn(POLL_INTERVAL_SECONDS)
            current_href = self.page_facts().href
            if current_href != before_href:
                return current_href
        self._screenshot("new_project")
        raise ProviderTimeoutError("Flow UI changed: new_project: timed out", provider="flow")

    def snapshot_media(self) -> MediaSnapshot:
        """Uuids already on the project page plus the latest performance video URL."""
        res = self._eval_json(
            JS_MEDIA_SNAPSHOT, timeout=EVAL_TIMEOUT_SECONDS, step="snapshot_media"
        )
        raw_uuids = res.get("uuids", [])
        uuids_set: frozenset[str] = (
            frozenset(str(x) for x in raw_uuids) if isinstance(raw_uuids, list) else frozenset()
        )
        latest_url = str(res.get("latest_video_url", ""))
        return MediaSnapshot(uuids=uuids_set, latest_video_url=latest_url)

    def latest_reply(self) -> str:
        """Tail of the agent panel text. May be empty."""
        res = self._eval_json(JS_LATEST_REPLY, timeout=EVAL_TIMEOUT_SECONDS, step="latest_reply")
        return str(res.get("text", ""))

    def approval_text(self) -> str:
        """Approval card text, or empty string when no card is visible."""
        res = self._eval_json(JS_APPROVAL_TEXT, timeout=EVAL_TIMEOUT_SECONDS, step="approval_text")
        return str(res.get("text", ""))

    def progress_lines(self) -> list[str]:
        """Short progress fragments. Empty when none."""
        res = self._eval_json(JS_PROGRESS, timeout=EVAL_TIMEOUT_SECONDS, step="progress_lines")
        lines = res.get("lines", [])
        if isinstance(lines, list):
            return [str(x) for x in lines]
        return []

    def approve(self) -> None:
        """Click Approve, then require the control to disappear."""
        res = self._eval_json(JS_CLICK_APPROVE, timeout=EVAL_TIMEOUT_SECONDS, step="approve")
        if not res.get("ok"):
            self._fail("approve", str(res.get("reason", "no_approve")))
        deadline = self.time_fn() + APPROVE_TIMEOUT_SECONDS
        check = self._eval_json(
            JS_APPROVE_STILL_THERE, timeout=EVAL_TIMEOUT_SECONDS, step="approve_present"
        )
        if not check.get("present"):
            return
        while self.time_fn() < deadline:
            self.sleep_fn(POLL_INTERVAL_SECONDS)
            check = self._eval_json(
                JS_APPROVE_STILL_THERE, timeout=EVAL_TIMEOUT_SECONDS, step="approve_present"
            )
            if not check.get("present"):
                return
        self._screenshot("approve")
        raise ProviderError("Flow UI changed: approve: still present", provider="flow")

    def agent_error_text(self, message: str) -> str:
        """Agent text after message when it classifies as REFUSAL or ERROR, else empty."""
        text = _text_after(self.latest_reply(), message)
        if classify_reply(text) in (ReplyKind.REFUSAL, ReplyKind.ERROR):
            return text
        return ""

    def find_new_media(self, baseline: frozenset[str]) -> str | None:
        """A new uuid, or None. Does not wait."""
        snap = self.snapshot_media()
        new_uuids = new_media_uuids(set(baseline), set(snap.uuids))
        if not new_uuids:
            return None
        if len(new_uuids) == 1:
            return new_uuids[0]
        perf_uuid = uuid_from_media_url(snap.latest_video_url)
        if perf_uuid is not None and perf_uuid in new_uuids:
            return perf_uuid
        return max(new_uuids)

    def tile_count(self) -> int:
        """Number of visible video tiles on the project page."""
        res = self._eval_json(JS_TILE_COUNT, timeout=EVAL_TIMEOUT_SECONDS, step="tile_count")
        count = res.get("count", 0)
        return count if isinstance(count, int) else 0

    def newest_tile_video_url(self, project_url: str, baseline: frozenset[str]) -> str:
        """Open the newest tile, read its signed video URL, return to project_url.

        Tile thumbnails use opaque URLs, so the uuid is only visible in the player.
        Returns empty while the tile has no playable video yet or its uuid is in baseline.
        Caller redacts logs.
        """
        # A hidden tab never loads the player, so the URL would never appear.
        self.ensure_visible()
        res = self._eval_json(js_open_tile(0), timeout=EVAL_TIMEOUT_SECONDS, step="open_tile")
        if not res.get("ok"):
            return ""
        try:
            url = self.wait_until(
                "tile_video",
                lambda: self.latest_signed_video_url() or None,
                timeout=EVAL_TIMEOUT_SECONDS,
            )
        except ProviderTimeoutError:
            url = ""
        finally:
            self.open_project(project_url)
        found = uuid_from_media_url(url)
        if found is None or found in baseline:
            return ""
        return url

    def latest_signed_video_url(self, uuid: str = "") -> str:
        """Signed video URL (optionally for one uuid) including query, or empty.

        Caller redacts logs.
        """
        res = self._eval_json(
            js_media_url_from_performance(uuid),
            timeout=EVAL_TIMEOUT_SECONDS,
            step="latest_signed_video_url",
        )
        url = str(res.get("url", ""))
        if url and uuid_from_media_url(url) is None:
            self._fail("media_url", "unexpected host")
        return url

    def wait_until(
        self,
        step: str,
        predicate: Callable[[], T | None],
        *,
        timeout: float,
    ) -> T:
        """Poll predicate until it returns non-None. Timeout raises ProviderTimeoutError."""
        deadline = self.time_fn() + timeout
        val = predicate()
        if val is not None:
            return val
        while self.time_fn() < deadline:
            self.sleep_fn(POLL_INTERVAL_SECONDS)
            val = predicate()
            if val is not None:
                return val
        self._screenshot(step)
        raise ProviderTimeoutError(f"Flow UI changed: {step}: timed out", provider="flow")

    def composer_text(self) -> str:
        """Current text in the composer (whitespace-collapsed), or empty."""
        res = self._eval_json(JS_COMPOSER_TEXT, timeout=EVAL_TIMEOUT_SECONDS, step="composer_text")
        return str(res.get("text", ""))

    def _visibility(self) -> str:
        res = self._eval_json(JS_VISIBILITY, timeout=EVAL_TIMEOUT_SECONDS, step="visibility")
        return str(res.get("state", ""))

    def ensure_visible(self) -> None:
        """Flow ignores sends while the tab is hidden; restore the window and front the tab."""
        if self._visibility() == "visible":
            return
        win = self.session.send("Browser.getWindowForTarget")
        window_id = win.get("result", {}).get("windowId") if isinstance(win, dict) else None
        if window_id is not None:
            # A minimize/restore cycle makes Chrome mark the page visible again.
            for state in ("minimized", "normal"):
                self.session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": state}},
                )
                self.sleep_fn(1.0)
        self.session.send("Page.bringToFront")
        try:
            self.wait_until(
                "ensure_visible",
                lambda: True if self._visibility() == "visible" else None,
                timeout=EVAL_TIMEOUT_SECONDS,
            )
        except ProviderTimeoutError as exc:
            raise ProviderError(
                "Flow UI changed: ensure_visible: tab stays hidden; bring the Flow window "
                "to the front",
                provider="flow",
            ) from exc

    def ensure_confirm_always(self) -> bool:
        """Make Flow's 'Confirm before generating' setting Always. Return True if it changed.

        With Never, Flow spends credits without an approval card, so the credit cap
        cannot be enforced.
        """
        # An open settings view hides the composer; close it, then expand the panel.
        self._eval_json(
            JS_CLOSE_AGENT_SETTINGS, timeout=EVAL_TIMEOUT_SECONDS, step="close_settings"
        )
        self.focus_composer()
        res = self._eval_json(
            JS_OPEN_AGENT_SETTINGS, timeout=EVAL_TIMEOUT_SECONDS, step="open_settings"
        )
        if not res.get("ok"):
            self._fail("confirm_setting", str(res.get("reason", "no_settings")))

        def _read() -> str | None:
            r = self._eval_json(
                JS_CONFIRM_SETTING, timeout=EVAL_TIMEOUT_SECONDS, step="confirm_setting"
            )
            value = str(r.get("value", ""))
            return value or None

        try:
            value = self.wait_until("confirm_setting", _read, timeout=EVAL_TIMEOUT_SECONDS)
            if value == "Always":
                return False
            res = self._eval_json(
                JS_SET_CONFIRM_ALWAYS, timeout=EVAL_TIMEOUT_SECONDS, step="set_confirm"
            )
            if not res.get("ok"):
                self._fail("confirm_setting", str(res.get("reason", "set_failed")))
            self.sleep_fn(1.0)
            self.wait_until(
                "confirm_setting_saved",
                lambda: True if _read() == "Always" else None,
                timeout=EVAL_TIMEOUT_SECONDS,
            )
            logger.info("flow confirm setting changed to Always")
            return True
        finally:
            self._eval_json(
                JS_CLOSE_AGENT_SETTINGS, timeout=EVAL_TIMEOUT_SECONDS, step="close_settings"
            )

    def send_prompt(self, message: str) -> None:
        """Focus composer, insert text, press Enter, and confirm the composer emptied.

        Enter pressed before the editor registers the inserted text is ignored, so wait
        for the text to land first. If the composer still holds the text afterwards the
        message was not sent; the send button is clicked once, then it fails.
        """
        flat = " ".join(message.split())
        self.ensure_visible()
        existing = self.composer_text()
        self.focus_composer()
        if existing and existing != flat:
            self._fail("send_prompt", "composer not empty; clear the draft in Flow first")
        if not existing:
            self.insert_text(message)
        # else: an earlier unsent attempt left exactly this message; just send it.
        head = flat[:40]
        self.wait_until(
            "send_prompt_text",
            lambda: True if head in self.composer_text() else None,
            timeout=EVAL_TIMEOUT_SECONDS,
        )
        for attempt in range(2):
            if attempt == 0:
                self.press_enter()
            else:
                # Enter is sometimes ignored by the editor; use the visible send button.
                res = self._eval_json(
                    JS_CLICK_SEND, timeout=EVAL_TIMEOUT_SECONDS, step="click_send"
                )
                if not res.get("ok"):
                    self._fail("send_prompt", str(res.get("reason", "no_send_button")))
            try:
                self.wait_until(
                    "send_prompt",
                    lambda: True if not self.composer_text() else None,
                    timeout=SEND_CONFIRM_SECONDS,
                )
                return
            except ProviderTimeoutError:
                continue
        self._fail("send_prompt", "message stayed in composer")

    def read_decision_text(self, message: str) -> str:
        """Return approval_text if present, else latest_reply after message."""
        app = self.approval_text()
        if app:
            return app
        return _text_after(self.latest_reply(), message)

    def fetch_media_base64(self, url: str) -> bytes:
        """In-page fetch of one flow-content.google URL. Raises ProviderError on failure."""
        expr = js_fetch_media_b64(url)
        res = self._eval_json(expr, timeout=DOWNLOAD_FETCH_TIMEOUT_SECONDS, step="fetch_media")
        if not res.get("ok"):
            status = res.get("status", 0)
            self._fail("fetch_media", f"HTTP {status}")
        b64_val = res.get("b64")
        if not isinstance(b64_val, str) or not b64_val:
            self._fail("fetch_media", "empty")
        try:
            data = base64.b64decode(b64_val, validate=True)
        except binascii.Error:
            self._fail("fetch_media", "bad base64")
        if not data:
            self._fail("fetch_media", "empty")
        return data
