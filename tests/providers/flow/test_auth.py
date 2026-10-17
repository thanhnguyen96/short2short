from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from tests.providers.flow.fake_cdp import FakeCdp

from short2short.config import AppConfig
from short2short.errors import (
    ConfigError,
    PathSecurityError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
)
from short2short.providers.flow.auth import (
    CdpPage,
    DoctorReport,
    chrome_argv,
    doctor,
    find_chrome,
    free_port,
    launch_chrome,
    login,
    logout,
    port_file,
    read_port,
    status,
    write_port,
)
from short2short.storage.cache import ClipCache


def test_chrome_argv() -> None:
    """Chrome argv contains loopback, profile dir, flow page URL; no stealth/proxy/automation."""
    argv = chrome_argv("chrome.exe", 9222, Path("C:/profile"))
    full_cmd = " ".join(argv).lower()

    assert "127.0.0.1" in full_cmd
    assert "--user-data-dir=" in full_cmd
    assert "--remote-debugging-port=9222" in full_cmd
    assert "--no-default-browser-check" in argv
    assert argv[-1] == "https://flow.google.com/"

    assert "suppress" not in full_cmd
    assert "proxy" not in full_cmd
    assert "extension" not in full_cmd
    assert "automation" not in full_cmd


def test_free_port() -> None:
    """free_port binds 127.0.0.1:0 and returns a positive port number."""
    port = free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_launch_chrome_detached() -> None:
    """launch_chrome uses detached flags and DEVNULL redirection."""
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return None

    launch_chrome(["chrome.exe"], popen=fake_popen)
    assert captured["argv"] == ["chrome.exe"]
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == subprocess.DEVNULL

    if os.name == "nt":
        expected_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(
            subprocess, "DETACHED_PROCESS", 0x8
        )
        assert captured["kwargs"]["creationflags"] == expected_flags
    else:
        assert captured["kwargs"]["start_new_session"] is True


def test_find_chrome_configured(tmp_path: Path) -> None:
    """find_chrome honors valid existing config.chrome_path."""
    dummy_exe = tmp_path / "chrome.exe"
    dummy_exe.touch()
    cfg = AppConfig(chrome_path=str(dummy_exe))
    assert find_chrome(cfg) == str(dummy_exe)


def test_find_chrome_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """find_chrome raises ProviderAuthError when chrome cannot be located."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setenv("PROGRAMFILES", "C:/nonexistent")
    monkeypatch.setenv("PROGRAMFILES(X86)", "C:/nonexistent")
    monkeypatch.setenv("LOCALAPPDATA", "C:/nonexistent")

    with pytest.raises(ProviderAuthError, match="Google Chrome executable not found"):
        find_chrome(AppConfig(chrome_path="C:/nonexistent/chrome.exe"))


def test_port_file_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """read_port and write_port correctly persist port to disk."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    assert read_port() is None

    write_port(9333)
    assert read_port() == 9333
    assert port_file().is_file()


def test_read_port_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """read_port catches ValueError and OSError only, returning None."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    p = port_file()
    p.write_text("not-an-int", encoding="utf-8")
    assert read_port() is None

    def fake_read_text(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    assert read_port() is None


class FakeWs:
    """Fake websocket connection for CdpPage tests."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.sent: list[dict[str, Any]] = []
        self.timeout: float | None = None
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def recv(self) -> str:
        if not self.responses:
            return ""
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return item
        return json.dumps(item)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def gettimeout(self) -> float | None:
        return self.timeout

    def close(self) -> None:
        self.closed = True


def test_cdp_page_connect() -> None:
    """CdpPage.connect targets flow.google.com page and asserts loopback."""
    targets = [
        {"type": "page", "url": "https://google.com"},
        {
            "type": "page",
            "url": "https://flow.google.com/project/p1",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/target1",
        },
    ]

    class FakeHttpResp:
        def json(self) -> list[dict[str, Any]]:
            return targets

    fake_ws = FakeWs([])
    cdp = CdpPage.connect(
        9222,
        http_get=lambda _: FakeHttpResp(),
        ws_factory=lambda url, timeout: fake_ws,
    )
    assert cdp.ws is fake_ws


def test_cdp_page_connect_no_flow_tab() -> None:
    """CdpPage.connect raises ProviderAuthError when no flow.google.com tab is present."""
    targets = [{"type": "page", "url": "https://other.google.com"}]

    class FakeHttpResp:
        def json(self) -> list[dict[str, Any]]:
            return targets

    with pytest.raises(
        ProviderAuthError, match=r"No Flow page tab \(flow\.google\.com\) found in Chrome"
    ):
        CdpPage.connect(9222, http_get=lambda _: FakeHttpResp())


def test_cdp_page_connect_non_loopback() -> None:
    """CdpPage.connect rejects non-loopback websocket URLs with PathSecurityError."""
    targets = [
        {
            "type": "page",
            "url": "https://flow.google.com/project/p1",
            "webSocketDebuggerUrl": "ws://evil.example.com:9222/devtools/page/target1",
        }
    ]

    class FakeHttpResp:
        def json(self) -> list[dict[str, Any]]:
            return targets

    with pytest.raises(PathSecurityError):
        CdpPage.connect(9222, http_get=lambda _: FakeHttpResp())


def test_cdp_page_connect_missing_ws_url() -> None:
    """CdpPage.connect raises ProviderAuthError when matched target lacks webSocketDebuggerUrl."""
    targets = [{"type": "page", "url": "https://flow.google.com/project/p1"}]

    class FakeHttpResp:
        def json(self) -> list[dict[str, Any]]:
            return targets

    with pytest.raises(ProviderAuthError, match="Target missing webSocketDebuggerUrl"):
        CdpPage.connect(9222, http_get=lambda _: FakeHttpResp())


def test_cdp_page_send_skips_events_and_bad_json() -> None:
    """CdpPage.send skips non-matching responses and bad JSON until matching request ID."""
    responses: list[Any] = [
        {"method": "Network.requestWillBeSent", "params": {}},
        "not-valid-json",
        {"id": 1, "result": {"data": "ok"}},
    ]
    fake_ws = FakeWs(responses)
    cdp = CdpPage(fake_ws)

    res = cdp.send("Some.method", {"param": 1})
    assert res["id"] == 1
    assert res["result"]["data"] == "ok"


def test_cdp_page_send_closed() -> None:
    """CdpPage.send raises ProviderError when connection is closed."""
    fake_ws = FakeWs([])
    cdp = CdpPage(fake_ws)

    with pytest.raises(ProviderError, match="CDP connection closed unexpectedly"):
        cdp.send("Some.method")


def test_cdp_page_evaluate_success() -> None:
    """CdpPage.evaluate evaluates expression and returns string result."""
    responses = [{"id": 1, "result": {"result": {"value": '{"ok": true}'}}}]
    fake_ws = FakeWs(responses)
    cdp = CdpPage(fake_ws)

    res = cdp.evaluate("1 + 1")
    assert res == '{"ok": true}'


def test_cdp_page_evaluate_exception() -> None:
    """CdpPage.evaluate raises ProviderError with redacted message on JS exception."""
    responses = [
        {
            "id": 1,
            "result": {
                "exceptionDetails": {
                    "text": "Uncaught Error: token=secret_token_xyz failed",
                }
            },
        }
    ]
    fake_ws = FakeWs(responses)
    cdp = CdpPage(fake_ws)

    with pytest.raises(ProviderError) as exc_info:
        cdp.evaluate("throw new Error('token=secret_token_xyz failed');")
    assert "secret_token_xyz" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_cdp_page_evaluate_timeout() -> None:
    """CdpPage.evaluate wraps TimeoutError in ProviderTimeoutError."""
    fake_ws = FakeWs([TimeoutError("socket timeout")])
    cdp = CdpPage(fake_ws)

    with pytest.raises(ProviderTimeoutError, match="CDP evaluate timed out"):
        cdp.evaluate("while(true);")


def test_cdp_page_close() -> None:
    """CdpPage.close safely closes websocket connection."""
    fake_ws = FakeWs([])
    cdp = CdpPage(fake_ws)
    cdp.close()
    assert fake_ws.closed is True


def test_login_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """login returns page href when logged in; does not touch secrets or cookies."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    monkeypatch.setattr(
        "short2short.config.set_secret",
        lambda *args, **kwargs: pytest.fail("set_secret should not be called"),
    )

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": True,
                "host": "flow.google.com",
                "href": "https://flow.google.com/project/p1",
                "composer": True,
            },
        )
    ]
    fake_cdp = FakeCdp(scripts)

    href = login(
        AppConfig(),
        wait_seconds=5.0,
        cdp_connector=lambda port: cast(CdpPage, fake_cdp),
        sleep_fn=lambda _: None,
    )

    assert href == "https://flow.google.com/project/p1"


def test_login_retry_connect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """login retries on connection error and succeeds once connected."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": True,
                "host": "flow.google.com",
                "href": "https://flow.google.com/",
                "composer": True,
            },
        )
    ]
    fake_cdp = FakeCdp(scripts)
    connect_calls = 0
    delays: list[float] = []

    def fake_connector(port: int) -> CdpPage:
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            raise OSError("connection refused")
        return cast(CdpPage, fake_cdp)

    href = login(
        AppConfig(),
        wait_seconds=10.0,
        cdp_connector=fake_connector,
        sleep_fn=delays.append,
    )

    assert href == "https://flow.google.com/"
    assert delays == [2.0]
    assert connect_calls == 2


def test_login_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """login raises ProviderAuthError when deadline passes without composer."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": False,
                "host": "accounts.google.com",
                "href": "https://accounts.google.com/",
                "composer": False,
            },
        )
    ]
    fake_cdp = FakeCdp(scripts)

    time_val = 100.0

    def fake_time() -> float:
        nonlocal time_val
        time_val += 10.0
        return time_val

    with pytest.raises(ProviderAuthError, match="Flow composer"):
        login(
            AppConfig(),
            wait_seconds=5.0,
            cdp_connector=lambda port: cast(CdpPage, fake_cdp),
            sleep_fn=lambda _: None,
            time_fn=fake_time,
        )


def test_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """status returns True only when port exists and composer is visible."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)

    # Missing port file -> False
    assert status() is False

    write_port(9222)

    # Connector raises OSError -> False
    assert status(cdp_connector=lambda port: (_ for _ in ()).throw(OSError("fail"))) is False

    # Composer not logged in -> False
    scripts_not_logged_in: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": False,
                "host": "accounts.google.com",
                "href": "https://accounts.google.com/",
                "composer": False,
            },
        )
    ]
    fake_cdp1 = FakeCdp(scripts_not_logged_in)
    assert status(cdp_connector=lambda port: cast(CdpPage, fake_cdp1)) is False

    # Logged in -> True
    scripts_logged_in: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": True,
                "host": "flow.google.com",
                "href": "https://flow.google.com/",
                "composer": True,
            },
        )
    ]
    fake_cdp2 = FakeCdp(scripts_logged_in)
    assert status(cdp_connector=lambda port: cast(CdpPage, fake_cdp2)) is True


def test_logout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """logout deletes port file and legacy keyring entries; handles ConfigError."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    deleted_secrets: list[str] = []

    def fake_delete_secret(name: str) -> None:
        if name == "flow_access_token":
            deleted_secrets.append(name)
        else:
            raise ConfigError("Secret not found")

    monkeypatch.setattr("short2short.providers.flow.auth.delete_secret", fake_delete_secret)

    logout()
    assert deleted_secrets == ["flow_access_token"]
    assert read_port() is None


def test_doctor_all_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor all-ok path returns 7 ok rows and never sends Input.* commands."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": True,
                "host": "flow.google.com",
                "href": "https://flow.google.com/",
                "composer": True,
            },
        ),
        (
            "fp_marker_projects",
            {
                "href": "https://flow.google.com/",
                "projects": [],
                "new_project": True,
            },
        ),
        ("fp_marker_focus", {"ok": True}),
        ("fp_marker_reply", {"text": "Agent reply ready", "source": "scroller"}),
    ]
    fake_cdp = FakeCdp(scripts)

    reports = doctor(AppConfig(), cdp_connector=lambda port: cast(CdpPage, fake_cdp))

    assert len(reports) == 7
    assert all(r.ok for r in reports)

    names = [r.name for r in reports]
    assert names == [
        "cdp_port",
        "cdp_connect",
        "logged_in",
        "new_project",
        "selectors",
        "reply_region",
        "confirm_setting",
    ]

    assert reports[0].detail == "port 9222"
    assert reports[1].detail == "connected"
    assert reports[2].detail == "composer visible"
    assert reports[3].detail == "visible"
    assert reports[4].detail == "composer ok"
    assert reports[5].detail == "Agent reply ready"
    assert "Confirm before generating" in reports[6].detail

    # Doctor must NEVER send Input.* CDP methods
    input_methods = [method for method, _ in fake_cdp.sent if method.startswith("Input.")]
    assert input_methods == []


def test_doctor_missing_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor returns a single failed row when the port file is missing."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)

    reports = doctor()
    assert reports == [DoctorReport(name="cdp_port", ok=False, detail="missing port file")]


def test_doctor_connect_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor stops after connection failure and returns port ok and connect fail."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    def fail_connector(port: int) -> CdpPage:
        raise ProviderAuthError("Connection refused by peer", provider="flow")

    reports = doctor(AppConfig(), cdp_connector=fail_connector)
    assert len(reports) == 2
    assert reports[0] == DoctorReport(name="cdp_port", ok=True, detail="port 9222")
    assert reports[1].name == "cdp_connect"
    assert reports[1].ok is False
    assert "Connection refused" in reports[1].detail


def test_doctor_new_project_stored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor reports stored project when New project control is not visible."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    cfg = AppConfig(cache_dir=tmp_path / "cache")
    ClipCache(cfg.cache_dir).write_job_record(
        "flow", "_project", {"url": "https://flow.google.com/project/stored1"}
    )

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": True,
                "host": "flow.google.com",
                "href": "https://flow.google.com/",
                "composer": True,
            },
        ),
        (
            "fp_marker_projects",
            {
                "href": "https://flow.google.com/",
                "projects": [],
                "new_project": False,
            },
        ),
        ("fp_marker_focus", {"ok": True}),
        ("fp_marker_reply", {"text": "", "source": "body"}),
    ]
    fake_cdp = FakeCdp(scripts)

    reports = doctor(cfg, cdp_connector=lambda port: cast(CdpPage, fake_cdp))
    np_report = next(r for r in reports if r.name == "new_project")
    assert np_report.ok is True
    assert np_report.detail == "stored project"


def test_doctor_new_project_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor reports not found when New project is not visible and no stored project exists."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    cfg = AppConfig(cache_dir=tmp_path / "cache")

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": True,
                "host": "flow.google.com",
                "href": "https://flow.google.com/",
                "composer": True,
            },
        ),
        (
            "fp_marker_projects",
            {
                "href": "https://flow.google.com/",
                "projects": [],
                "new_project": False,
            },
        ),
        ("fp_marker_focus", {"ok": True}),
        ("fp_marker_reply", {"text": "", "source": "body"}),
    ]
    fake_cdp = FakeCdp(scripts)

    reports = doctor(cfg, cdp_connector=lambda port: cast(CdpPage, fake_cdp))
    np_report = next(r for r in reports if r.name == "new_project")
    assert np_report.ok is False
    assert np_report.detail == "not found"


def test_doctor_selectors_bad_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor reports bad payload when JS_FOCUS_COMPOSER returns a dict without ok key."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": True,
                "host": "flow.google.com",
                "href": "https://flow.google.com/",
                "composer": True,
            },
        ),
        (
            "fp_marker_projects",
            {
                "href": "https://flow.google.com/",
                "projects": [],
                "new_project": True,
            },
        ),
        ("fp_marker_focus", {"unexpected": "payload"}),
        ("fp_marker_reply", {"text": "", "source": "body"}),
    ]
    fake_cdp = FakeCdp(scripts)

    reports = doctor(AppConfig(), cdp_connector=lambda port: cast(CdpPage, fake_cdp))
    sel_report = next(r for r in reports if r.name == "selectors")
    assert sel_report.ok is False
    assert sel_report.detail == "bad payload"


def test_doctor_logged_in_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor reports composer missing host when not logged in."""
    monkeypatch.setattr("short2short.providers.flow.auth.default_config_dir", lambda: tmp_path)
    write_port(9222)

    scripts: list[tuple[str, Any]] = [
        (
            "fp_marker_logged_in",
            {
                "logged_in": False,
                "host": "accounts.google.com",
                "href": "https://accounts.google.com/",
                "composer": False,
            },
        ),
        (
            "fp_marker_projects",
            {
                "href": "https://accounts.google.com/",
                "projects": [],
                "new_project": True,
            },
        ),
        ("fp_marker_focus", {"ok": True}),
        ("fp_marker_reply", {"text": "", "source": "body"}),
    ]
    fake_cdp = FakeCdp(scripts)

    reports = doctor(AppConfig(), cdp_connector=lambda port: cast(CdpPage, fake_cdp))
    login_report = next(r for r in reports if r.name == "logged_in")
    assert login_report.ok is False
    assert login_report.detail == "composer missing host=accounts.google.com"
