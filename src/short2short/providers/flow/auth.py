"""Chrome launch, CDP connect, login/status/logout, and doctor for the flow.google.com adapter."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import websocket

from short2short.config import (
    AppConfig,
    default_config_dir,
    delete_secret,
    load_config,
)
from short2short.errors import ConfigError, ProviderAuthError, ProviderError, ProviderTimeoutError
from short2short.providers.flow.constants import (
    CDP_PORT_FILE_NAME,
    DOCTOR_TAIL_CHARS,
    EVAL_TIMEOUT_SECONDS,
    FLOW_PAGE_HOSTS,
    FLOW_PAGE_URL,
    LOGIN_TIMEOUT_SECONDS,
    PROJECT_TITLE,
)
from short2short.providers.flow.ui import CdpSession, FlowUi
from short2short.providers.flow.ui_js import JS_FOCUS_COMPOSER
from short2short.security import assert_loopback_host, redact
from short2short.storage.cache import ClipCache

logger = logging.getLogger(__name__)

__all__ = [
    "CdpPage",
    "DoctorReport",
    "chrome_argv",
    "doctor",
    "find_chrome",
    "free_port",
    "launch_chrome",
    "login",
    "logout",
    "port_file",
    "read_port",
    "status",
    "write_port",
]


def find_chrome(config: AppConfig | None = None) -> str:
    """Locate Google Chrome executable path."""
    if config and config.chrome_path:
        p = Path(config.chrome_path)
        if p.is_file():
            return str(p)

    if os.name == "nt":
        candidates = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        for base in candidates:
            if base:
                exe = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if exe.is_file():
                    return str(exe)

    for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found

    raise ProviderAuthError("Google Chrome executable not found", provider="flow")


def chrome_argv(chrome: str, port: int, profile: Path | str) -> list[str]:
    """Build Chrome launch arguments for remote debugging."""
    return [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # Flow ignores input and skips media loads in a hidden or occluded tab.
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        FLOW_PAGE_URL,
    ]


def free_port() -> int:
    """Find an available TCP port on loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def launch_chrome(argv: list[str], popen: Any = subprocess.Popen) -> Any:
    """Launch detached Chrome process."""
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True

    return popen(argv, **kwargs)


def port_file(config: AppConfig | None = None) -> Path:
    """Return path to CDP port file."""
    return default_config_dir() / CDP_PORT_FILE_NAME


def read_port(config: AppConfig | None = None) -> int | None:
    """Read saved CDP port from file if present."""
    p = port_file(config)
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def write_port(port: int, config: AppConfig | None = None) -> None:
    """Save CDP port to file."""
    p = port_file(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(port), encoding="utf-8")


class CdpPage(CdpSession):
    """CDP session connected to a Google Flow tab."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._req_id = 0

    @classmethod
    def connect(
        cls,
        port: int,
        *,
        http_get: Any = None,
        ws_factory: Any = None,
    ) -> CdpPage:
        """Connect to active Flow page tab via Chrome DevTools Protocol."""
        url = f"http://127.0.0.1:{port}/json/list"
        if http_get is None:
            resp = httpx.get(url, timeout=10.0)
            targets: Any = resp.json()
        else:
            resp = http_get(url)
            targets = resp.json() if hasattr(resp, "json") else json.loads(resp.text)

        matched: dict[str, Any] | None = None
        if isinstance(targets, list):
            for t in targets:
                if (
                    isinstance(t, dict)
                    and t.get("type") == "page"
                    and any(h in t.get("url", "") for h in FLOW_PAGE_HOSTS)
                ):
                    matched = t
                    break

        if matched is None:
            raise ProviderAuthError(
                "No Flow page tab (flow.google.com) found in Chrome", provider="flow"
            )

        ws_url = matched.get("webSocketDebuggerUrl")
        if not ws_url or not isinstance(ws_url, str):
            raise ProviderAuthError("Target missing webSocketDebuggerUrl", provider="flow")

        parsed = urllib.parse.urlsplit(ws_url)
        assert_loopback_host(parsed.hostname or "")

        if ws_factory is None:
            # Chrome rejects DevTools websockets that carry a browser Origin header.
            ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        else:
            try:
                ws = ws_factory(ws_url, timeout=30)
            except TypeError:
                ws = ws_factory(ws_url)

        return cls(ws)

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send CDP command and wait for corresponding response."""
        self._req_id += 1
        req_id = self._req_id
        req_msg = {"id": req_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(req_msg))

        while True:
            raw = self.ws.recv()
            if not raw:
                raise ProviderError("CDP connection closed unexpectedly", provider="flow")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if isinstance(msg, dict) and msg.get("id") == req_id:
                return msg

    def evaluate(self, expression: str, *, timeout: float = 60.0) -> str:
        """Evaluate JS expression in page using Runtime.evaluate."""
        old_timeout = None
        if hasattr(self.ws, "gettimeout"):
            with contextlib.suppress(AttributeError, TypeError):
                old_timeout = self.ws.gettimeout()
        if hasattr(self.ws, "settimeout"):
            with contextlib.suppress(AttributeError, TypeError):
                self.ws.settimeout(timeout)

        try:
            resp = self.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError("CDP evaluate timed out", provider="flow") from exc
        finally:
            if old_timeout is not None and hasattr(self.ws, "settimeout"):
                with contextlib.suppress(AttributeError, TypeError):
                    self.ws.settimeout(old_timeout)

        result_data = resp.get("result", {})
        if not isinstance(result_data, dict):
            result_data = {}

        if "exceptionDetails" in result_data:
            exc_details = result_data["exceptionDetails"]
            raise ProviderError(
                f"CDP evaluation error: {redact(str(exc_details))}", provider="flow"
            )

        val = result_data.get("result", {}).get("value")
        return "" if val is None else str(val)

    def close(self) -> None:
        """Close CDP websocket connection."""
        if hasattr(self.ws, "close"):
            with contextlib.suppress(OSError, websocket.WebSocketException):
                self.ws.close()


def login(
    config: AppConfig | None = None,
    *,
    wait_seconds: float = LOGIN_TIMEOUT_SECONDS,
    cdp_connector: Callable[[int], CdpPage] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> str:
    """Launch Chrome if needed and wait until the Flow composer is visible.

    Returns the final page URL (not a token, not an expiry).
    Does not read cookies and does not write keyring.
    """
    cfg = config or load_config()
    port = read_port(cfg)
    if port is None:
        chrome = find_chrome(cfg)
        port = free_port()
        argv = chrome_argv(chrome, port, cfg.chrome_profile_dir)
        launch_chrome(argv)
        write_port(port, cfg)

    connector = cdp_connector or CdpPage.connect
    session: CdpPage | None = None
    deadline = time_fn() + wait_seconds

    while time_fn() < deadline:
        if session is None:
            try:
                session = connector(port)
            except (
                ProviderAuthError,
                OSError,
                TimeoutError,
                httpx.HTTPError,
                websocket.WebSocketException,
            ):
                sleep_fn(2.0)
                continue

        try:
            facts = FlowUi(session).page_facts()
        except (ProviderError, OSError, TimeoutError, websocket.WebSocketException):
            session = None
            sleep_fn(2.0)
            continue

        if facts.logged_in:
            return facts.href

        sleep_fn(2.0)

    raise ProviderAuthError("Login timed out waiting for the Flow composer", provider="flow")


def status(
    config: AppConfig | None = None,
    *,
    cdp_connector: Callable[[int], CdpPage] | None = None,
) -> bool:
    """True when the CDP port file exists, connect works, and the composer is visible.

    Does not launch Chrome. Does not check keyring.
    """
    cfg = config or load_config()
    port = read_port(cfg)
    if port is None:
        return False

    connector = cdp_connector or CdpPage.connect
    try:
        session = connector(port)
        facts = FlowUi(session).page_facts()
        return bool(facts.logged_in)
    except (
        ProviderAuthError,
        ProviderError,
        OSError,
        TimeoutError,
        httpx.HTTPError,
        websocket.WebSocketException,
    ):
        return False


def logout(config: AppConfig | None = None) -> None:
    """Delete the CDP port file and legacy keyring entries. Does not kill Chrome."""
    cfg = config or load_config()
    for name in ("flow_access_token", "flow_cookies"):
        try:
            delete_secret(name)
        except ConfigError:
            logger.info("legacy secret %s was not stored", name)

    p = port_file(cfg)
    if p.is_file():
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("could not remove cdp port file: %s", exc)


@dataclass(frozen=True)
class DoctorReport:
    """One row per check. ok False means the step failed. detail is already redacted."""

    name: str
    ok: bool
    detail: str


def doctor(
    config: AppConfig | None = None,
    *,
    cdp_connector: Callable[[int], CdpPage] | None = None,
) -> list[DoctorReport]:
    """Run zero-credit checks. Never calls insert_text, press_enter, or approve."""
    cfg = config or load_config()
    port = read_port(cfg)
    if port is None:
        return [DoctorReport(name="cdp_port", ok=False, detail="missing port file")]

    reports: list[DoctorReport] = [DoctorReport(name="cdp_port", ok=True, detail=f"port {port}")]

    connector = cdp_connector or CdpPage.connect
    try:
        session = connector(port)
    except (
        ProviderAuthError,
        ProviderError,
        OSError,
        TimeoutError,
        httpx.HTTPError,
        websocket.WebSocketException,
    ) as exc:
        reports.append(DoctorReport(name="cdp_connect", ok=False, detail=redact(str(exc))[:200]))
        return reports

    reports.append(DoctorReport(name="cdp_connect", ok=True, detail="connected"))

    ui = FlowUi(session)
    current_href = ""
    try:
        facts = ui.page_facts()
        current_href = facts.href
        if facts.logged_in:
            reports.append(DoctorReport(name="logged_in", ok=True, detail="composer visible"))
        else:
            reports.append(
                DoctorReport(
                    name="logged_in", ok=False, detail=f"composer missing host={facts.host}"
                )
            )
    except (ProviderError, OSError, TimeoutError, websocket.WebSocketException) as exc:
        reports.append(
            DoctorReport(
                name="logged_in",
                ok=False,
                detail=f"composer missing: {redact(str(exc))[:200]}",
            )
        )

    has_stored = False
    try:
        record = ClipCache(cfg.cache_dir).read_job_record("flow", "_project")
        if isinstance(record, dict):
            url_str = str(record.get("url", ""))
            parsed = urllib.parse.urlsplit(url_str)
            if parsed.scheme == "https" and parsed.hostname == "flow.google.com":
                has_stored = True
    except (OSError, ValueError, json.JSONDecodeError):
        has_stored = False

    has_titled = False
    new_project_visible = False
    try:
        links, new_project_visible = ui.list_projects()
        has_titled = any(link.title == PROJECT_TITLE for link in links)
    except (ProviderError, OSError, TimeoutError, websocket.WebSocketException):
        pass

    on_home = urllib.parse.urlsplit(current_href).path in ("", "/")
    if new_project_visible:
        reports.append(DoctorReport(name="new_project", ok=True, detail="visible"))
    elif has_stored or has_titled:
        reports.append(DoctorReport(name="new_project", ok=True, detail="stored project"))
    elif not on_home:
        reports.append(
            DoctorReport(
                name="new_project",
                ok=True,
                detail="tab is inside a project; open flow.google.com home to check",
            )
        )
    else:
        reports.append(DoctorReport(name="new_project", ok=False, detail="not found"))

    try:
        res = ui._eval_json(
            JS_FOCUS_COMPOSER, timeout=EVAL_TIMEOUT_SECONDS, step="doctor_selectors"
        )
        if isinstance(res, dict) and "ok" in res:
            reports.append(DoctorReport(name="selectors", ok=True, detail="composer ok"))
        else:
            reports.append(DoctorReport(name="selectors", ok=False, detail="bad payload"))
    except (ProviderError, OSError, TimeoutError, websocket.WebSocketException):
        reports.append(DoctorReport(name="selectors", ok=False, detail="bad payload"))

    try:
        reply = ui.latest_reply()
        flattened = reply.replace("\r", " ").replace("\n", " ")
        detail = redact(flattened[-DOCTOR_TAIL_CHARS:])
        reports.append(DoctorReport(name="reply_region", ok=True, detail=detail))
    except (ProviderError, OSError, TimeoutError, websocket.WebSocketException) as exc:
        reports.append(
            DoctorReport(
                name="reply_region",
                ok=False,
                detail=redact(str(exc))[:DOCTOR_TAIL_CHARS],
            )
        )

    reports.append(
        DoctorReport(
            name="confirm_setting",
            ok=True,
            detail="cannot verify; Flow setting 'Confirm before generating' must be Always",
        )
    )

    return reports
