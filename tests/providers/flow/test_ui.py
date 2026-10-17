"""Tests for FlowUi driving the page via CDP."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from tests.providers.flow.fake_cdp import FakeCdp

from short2short.errors import ProviderError, ProviderTimeoutError
from short2short.providers.flow.ui import FlowUi, PageFacts, ProjectLink


def test_page_facts_maps_logged_in_payload() -> None:
    """page_facts correctly unpacks logged_in, host, href, and composer."""
    payload = {
        "logged_in": True,
        "host": "flow.google.com",
        "href": "https://flow.google.com/project/p1",
        "composer": True,
    }
    cdp = FakeCdp([("fp_marker_logged_in", payload)])
    ui = FlowUi(cdp)
    facts = ui.page_facts()
    assert isinstance(facts, PageFacts)
    assert facts.logged_in is True
    assert facts.host == "flow.google.com"
    assert facts.href == "https://flow.google.com/project/p1"
    assert facts.composer is True


def test_page_facts_raises_on_missing_keys(tmp_path: Path) -> None:
    """page_facts raises ProviderError if required keys are missing."""
    cdp = FakeCdp([("fp_marker_logged_in", {"host": "flow.google.com"})])
    ui = FlowUi(cdp, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="missing keys"):
        ui.page_facts()


def test_send_prompt_calls_focus_insert_and_enter_in_order() -> None:
    """send_prompt focuses composer, inserts text, and sends Enter keyDown/keyUp."""
    msg = "generate a cinematic drone shot"
    cdp = FakeCdp(
        [
            ("fp_marker_composer_text", {"ok": True, "text": ""}),
            ("fp_marker_focus", {"ok": True}),
            ("fp_marker_composer_text", {"ok": True, "text": msg}),
            ("fp_marker_composer_text", {"ok": True, "text": ""}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None)
    ui.send_prompt(msg)

    assert len(cdp.sent) == 3
    assert cdp.sent[0] == ("Input.insertText", {"text": "generate a cinematic drone shot"})
    assert cdp.sent[1] == (
        "Input.dispatchKeyEvent",
        {
            "type": "keyDown",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "text": "\r",
        },
    )
    assert cdp.sent[2] == (
        "Input.dispatchKeyEvent",
        {
            "type": "keyUp",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
        },
    )
    for method, _ in cdp.sent:
        assert method != "Input.dispatchMouseEvent"


def test_wait_until_success_and_read_decision_text() -> None:
    """wait_until polls until non-None; read_decision_text extracts decision text."""
    call_count = [0]

    def pred() -> str | None:
        call_count[0] += 1
        return "Approve 10 credits" if call_count[0] >= 2 else None

    ticks = [0.0]

    def time_fn() -> float:
        return ticks[0]

    def sleep_fn(_s: float) -> None:
        ticks[0] += 1.0

    ui = FlowUi(FakeCdp([]), sleep_fn=sleep_fn, time_fn=time_fn)
    result = ui.wait_until("agent_reply", pred, timeout=10.0)
    assert result == "Approve 10 credits"
    assert call_count[0] == 2

    # read_decision_text: approval_text non-empty takes precedence
    cdp_app = FakeCdp([("fp_marker_approval", {"text": "Approve 10 credits"})])
    ui_app = FlowUi(cdp_app)
    assert ui_app.read_decision_text("my prompt") == "Approve 10 credits"

    # read_decision_text: cut away full message
    msg = "cinematic drone shot"
    reply_full = f"prompt: {msg}\nVeo 3.1 - Fast. 14% generating"
    cdp_reply = FakeCdp(
        [
            ("fp_marker_approval", {"text": ""}),
            ("fp_marker_reply", {"text": reply_full}),
        ]
    )
    ui_reply = FlowUi(cdp_reply)
    assert ui_reply.read_decision_text(msg) == "Veo 3.1 - Fast. 14% generating"

    # read_decision_text: cut away first 80 chars if full prompt truncated
    long_msg = "A" * 100
    reply_prefix = f"prompt: {'A' * 80}\nRefused policy"
    cdp_pre = FakeCdp(
        [
            ("fp_marker_approval", {"text": ""}),
            ("fp_marker_reply", {"text": reply_prefix}),
        ]
    )
    ui_pre = FlowUi(cdp_pre)
    assert ui_pre.read_decision_text(long_msg) == "Refused policy"

    # read_decision_text: fallback to full reply if neither found
    cdp_neither = FakeCdp(
        [
            ("fp_marker_approval", {"text": ""}),
            ("fp_marker_reply", {"text": "Something went wrong"}),
        ]
    )
    ui_neither = FlowUi(cdp_neither)
    assert ui_neither.read_decision_text("unrelated") == "Something went wrong"


def test_wait_until_timeout_captures_screenshot(tmp_path: Path) -> None:
    """wait_until times out, raises ProviderTimeoutError, and writes screenshot."""
    ticks = [0.0]

    def time_fn() -> float:
        return ticks[0]

    def sleep_fn(_s: float) -> None:
        ticks[0] += 10_000.0

    ui = FlowUi(FakeCdp([]), sleep_fn=sleep_fn, time_fn=time_fn, screenshot_dir=tmp_path)
    with pytest.raises(ProviderTimeoutError, match="Flow UI changed: test_step: timed out"):
        ui.wait_until("test_step", lambda: None, timeout=5.0)

    screenshots = list(tmp_path.glob("flow-test_step-*.png"))
    assert len(screenshots) == 1


def test_approve_clicks_and_confirms_disappearance() -> None:
    """approve clicks approve button and verifies it is no longer present."""
    cdp = FakeCdp(
        [
            ("fp_marker_click_approve", {"ok": True, "tag": "DIV", "role": "radio"}),
            ("fp_marker_approve_present", {"present": False}),
        ]
    )
    ui = FlowUi(cdp)
    ui.approve()


def test_approve_fails_when_click_returns_false(tmp_path: Path) -> None:
    """approve raises ProviderError when JS returns ok: false with no_approve."""
    cdp = FakeCdp([("fp_marker_click_approve", {"ok": False, "reason": "no_approve"})])
    ui = FlowUi(cdp, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="no_approve"):
        ui.approve()
    assert len(list(tmp_path.glob("flow-approve-*.png"))) == 1


def test_approve_fails_when_control_still_present(tmp_path: Path) -> None:
    """approve raises ProviderError when control does not disappear within timeout."""
    ticks = [0.0]

    def time_fn() -> float:
        return ticks[0]

    def sleep_fn(_s: float) -> None:
        ticks[0] += 25.0

    cdp = FakeCdp(
        [
            ("fp_marker_click_approve", {"ok": True, "tag": "DIV", "role": "radio"}),
            ("fp_marker_approve_present", {"present": True}),
            ("fp_marker_approve_present", {"present": True}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=sleep_fn, time_fn=time_fn, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="still present"):
        ui.approve()
    assert len(list(tmp_path.glob("flow-approve-*.png"))) == 1


def test_find_new_media_returns_new_uuid() -> None:
    """find_new_media returns newly appeared uuid ignoring baseline."""
    cdp = FakeCdp(
        [
            (
                "fp_marker_media",
                {
                    "uuids": [
                        "11111111-1111-1111-1111-111111111111",
                        "22222222-2222-2222-2222-222222222222",
                    ],
                    "latest_video_url": (
                        "https://flow-content.google/video/22222222-2222-2222-2222-222222222222?sig=x"
                    ),
                },
            )
        ]
    )
    ui = FlowUi(cdp)
    res = ui.find_new_media(frozenset({"11111111-1111-1111-1111-111111111111"}))
    assert res == "22222222-2222-2222-2222-222222222222"


def test_fetch_media_base64_roundtrip() -> None:
    """fetch_media_base64 decodes base64 payload from in-page fetch."""
    data = b"\x00\x01\x02\x03test-video-bytes"
    b64 = base64.b64encode(data).decode()
    cdp = FakeCdp([("fp_marker_fetch_media", {"ok": True, "b64": b64})])
    ui = FlowUi(cdp)
    fetched = ui.fetch_media_base64(
        "https://flow-content.google/video/11111111-2222-3333-4444-555555555555"
    )
    assert fetched == data


def test_fetch_media_base64_http_error(tmp_path: Path) -> None:
    """fetch_media_base64 raises ProviderError on non-200 HTTP response."""
    cdp = FakeCdp([("fp_marker_fetch_media", {"ok": False, "status": 403})])
    ui = FlowUi(cdp, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="HTTP 403"):
        ui.fetch_media_base64(
            "https://flow-content.google/video/11111111-2222-3333-4444-555555555555"
        )


def test_open_project_rejects_evil_url_before_evaluate() -> None:
    """open_project rejects non-flow url before sending any CDP evaluate."""
    cdp = FakeCdp([])
    ui = FlowUi(cdp)
    with pytest.raises(ValueError, match="refusing non-flow project url"):
        ui.open_project("https://evil.example/")
    assert len(cdp.expressions) == 0


def test_screenshot_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    """Failure during Page.captureScreenshot is caught and logged, not raised."""
    cdp = FakeCdp(
        [
            ("fp_marker_focus", {"ok": False, "reason": "no_composer"}),
            ("fp_marker_focus", {"ok": False, "reason": "no_composer"}),
            ("Page.captureScreenshot", OSError("disk failure")),
        ]
    )
    clock = iter([0.0, 0.0, 10_000.0])
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, time_fn=lambda: next(clock), screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="no_composer"):
        ui.focus_composer()


def test_focus_composer_waits_for_panel(tmp_path: Path) -> None:
    """Composer that renders after a moment is focused instead of failing."""
    cdp = FakeCdp(
        [
            ("fp_marker_focus", {"ok": False, "reason": "no_composer"}),
            ("fp_marker_focus", {"ok": True}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    ui.focus_composer()
    assert sum("fp_marker_focus" in e for e in cdp.expressions) == 2


def test_list_projects_filters_non_flow_hosts() -> None:
    """list_projects extracts ProjectLinks and filters out non-flow hosts."""
    cdp = FakeCdp(
        [
            (
                "fp_marker_projects",
                {
                    "href": "https://flow.google.com/",
                    "projects": [
                        {"text": "my project", "href": "https://flow.google.com/project/p1"},
                        {"text": "evil project", "href": "https://evil.example/project/p2"},
                    ],
                    "new_project": True,
                },
            )
        ]
    )
    ui = FlowUi(cdp)
    links, new_proj = ui.list_projects()
    assert new_proj is True
    assert len(links) == 1
    assert links[0] == ProjectLink("my project", "https://flow.google.com/project/p1")


def test_insert_text_empty_and_error(tmp_path: Path) -> None:
    """insert_text validates text not empty and checks CDP error."""
    ui = FlowUi(FakeCdp([]), screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="empty text"):
        ui.insert_text("")

    cdp_err = FakeCdp([("Input.insertText", {"error": {"message": "failed"}})])
    ui_err = FlowUi(cdp_err, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="cdp rejected insertText"):
        ui_err.insert_text("hello")


def test_new_project_missing_control(tmp_path: Path) -> None:
    """new_project raises ProviderError when control is not found."""
    cdp = FakeCdp(
        [
            ("fp_marker_projects", {"href": "https://flow.google.com/", "projects": []}),
            ("fp_marker_projects", {"projects": [], "new_project": True}),
            (
                "fp_marker_logged_in",
                {
                    "href": "https://flow.google.com/",
                    "host": "flow.google.com",
                    "logged_in": True,
                    "composer": True,
                },
            ),
            ("fp_marker_new_project", {"ok": False, "reason": "no_new_project"}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="no_new_project"):
        ui.new_project()


def test_new_project_control_never_visible_times_out(tmp_path: Path) -> None:
    """new_project times out when the home page never shows the control."""
    clock = iter([0.0, 0.0, 10_000.0])
    cdp = FakeCdp([("fp_marker_projects", {"projects": [], "new_project": False})] * 3)
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, time_fn=lambda: next(clock), screenshot_dir=tmp_path)
    with pytest.raises(ProviderTimeoutError, match="new_project_visible"):
        ui.new_project()
    assert not any("fp_marker_new_project" in e for e in cdp.expressions)


def test_latest_signed_video_url_unexpected_host(tmp_path: Path) -> None:
    """latest_signed_video_url raises ProviderError when host is unexpected."""
    cdp = FakeCdp([("fp_marker_perf_url", {"url": "https://evil.example/video/abc"})])
    ui = FlowUi(cdp, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="unexpected host"):
        ui.latest_signed_video_url()


def test_open_project_from_subpage_waits_for_exact_url(tmp_path: Path) -> None:
    """Tab on /project/<id>/tools is not the project page; wait until URL matches exactly."""
    proj = "https://flow.google.com/project/abc"

    def facts(href: str) -> dict[str, object]:
        return {"href": href, "host": "flow.google.com", "logged_in": True, "composer": True}

    cdp = FakeCdp(
        [
            ("fp_marker_open_project", {"ok": True, "href": proj + "/tools"}),
            ("fp_marker_logged_in", facts(proj + "/tools")),
            ("fp_marker_logged_in", facts(proj)),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    assert ui.open_project(proj) == proj


def test_send_prompt_clicks_send_button_when_enter_ignored(tmp_path: Path) -> None:
    """First Enter ignored by the editor: click the send button, then succeed."""
    msg = "drone show"
    clock = iter([0.0, 0.0, 0.0, 10_000.0, 0.0, 0.0])
    cdp = FakeCdp(
        [
            ("fp_marker_composer_text", {"ok": True, "text": ""}),
            ("fp_marker_focus", {"ok": True}),
            ("fp_marker_composer_text", {"ok": True, "text": msg}),
            ("fp_marker_composer_text", {"ok": True, "text": msg}),
            ("fp_marker_composer_text", {"ok": True, "text": msg}),
            ("fp_marker_click_send", {"ok": True}),
            ("fp_marker_composer_text", {"ok": True, "text": ""}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, time_fn=lambda: next(clock), screenshot_dir=tmp_path)
    ui.send_prompt(msg)
    keydowns = [p for m, p in cdp.sent if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"]
    assert len(keydowns) == 1
    assert any("fp_marker_click_send" in e for e in cdp.expressions)


def test_ensure_visible_restores_hidden_window(tmp_path: Path) -> None:
    """Hidden tab: minimize/restore the window, front the tab, wait until visible."""
    cdp = FakeCdp(
        [
            ("fp_marker_visibility", {"state": "hidden"}),
            ("Browser.getWindowForTarget", {"result": {"windowId": 7}}),
            ("fp_marker_visibility", {"state": "hidden"}),
            ("fp_marker_visibility", {"state": "visible"}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    ui.ensure_visible()
    states = [p["bounds"]["windowState"] for m, p in cdp.sent if m == "Browser.setWindowBounds"]
    assert states == ["minimized", "normal"]
    assert ("Page.bringToFront", {}) in cdp.sent


def test_ensure_visible_fails_when_tab_stays_hidden(tmp_path: Path) -> None:
    """Tab that never becomes visible raises with a hint instead of sending blind."""
    clock = iter([0.0, 10_000.0])
    cdp = FakeCdp([("fp_marker_visibility", {"state": "hidden"})] * 3)
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, time_fn=lambda: next(clock), screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="tab stays hidden"):
        ui.ensure_visible()


def test_send_prompt_refuses_when_draft_present(tmp_path: Path) -> None:
    """A leftover draft would be sent together with our prompt; refuse instead."""
    cdp = FakeCdp(
        [
            ("fp_marker_composer_text", {"ok": True, "text": "old draft"}),
            ("fp_marker_focus", {"ok": True}),
        ]
    )
    ui = FlowUi(cdp, screenshot_dir=tmp_path)
    with pytest.raises(ProviderError, match="composer not empty"):
        ui.send_prompt("new prompt")
    assert cdp.sent == [] or all(m == "Page.captureScreenshot" for m, _ in cdp.sent)


def test_send_prompt_reuses_identical_unsent_draft(tmp_path: Path) -> None:
    """Composer already holding exactly our message is sent without re-typing it."""
    msg = "drone show\n over river"
    flat = "drone show over river"
    cdp = FakeCdp(
        [
            ("fp_marker_composer_text", {"ok": True, "text": flat}),
            ("fp_marker_focus", {"ok": True}),
            ("fp_marker_composer_text", {"ok": True, "text": flat}),
            ("fp_marker_composer_text", {"ok": True, "text": ""}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    ui.send_prompt(msg)
    assert not any(m == "Input.insertText" for m, _ in cdp.sent)


def test_newest_tile_video_url_returns_to_project(tmp_path: Path) -> None:
    """Opening the newest tile yields its signed URL and navigates back to the project."""
    proj = "https://flow.google.com/project/abc"
    url = "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=x"
    facts = {"href": proj, "host": "flow.google.com", "logged_in": True, "composer": True}
    cdp = FakeCdp(
        [
            ("fp_marker_open_tile", {"ok": True}),
            ("fp_marker_perf_url", {"url": url}),
            ("fp_marker_open_project", {"ok": True}),
            ("fp_marker_logged_in", facts),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    assert ui.newest_tile_video_url(proj, frozenset()) == url
    assert any("fp_marker_open_project" in e for e in cdp.expressions)


def test_newest_tile_video_url_ignores_baseline_uuid(tmp_path: Path) -> None:
    """A tile whose video is already in the baseline is not new."""
    proj = "https://flow.google.com/project/abc"
    old = "11111111-2222-3333-4444-555555555555"
    facts = {"href": proj, "host": "flow.google.com", "logged_in": True, "composer": True}
    cdp = FakeCdp(
        [
            ("fp_marker_open_tile", {"ok": True}),
            ("fp_marker_perf_url", {"url": f"https://flow-content.google/video/{old}?sig=x"}),
            ("fp_marker_open_project", {"ok": True}),
            ("fp_marker_logged_in", facts),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    assert ui.newest_tile_video_url(proj, frozenset({old})) == ""


def test_ensure_confirm_always_switches_never(tmp_path: Path) -> None:
    """Never means no approval card; switch it to Always and confirm it stuck."""
    cdp = FakeCdp(
        [
            ("fp_marker_close_settings", {"ok": True}),
            ("fp_marker_focus", {"ok": True}),
            ("fp_marker_open_settings", {"ok": True}),
            ("fp_marker_confirm_setting", {"ok": True, "value": "Never"}),
            ("fp_marker_set_confirm_always", {"ok": True}),
            ("fp_marker_confirm_setting", {"ok": True, "value": "Always"}),
            ("fp_marker_close_settings", {"ok": True}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    assert ui.ensure_confirm_always() is True
    assert any("fp_marker_close_settings" in e for e in cdp.expressions)


def test_ensure_confirm_always_noop_when_already_always(tmp_path: Path) -> None:
    """Already Always: nothing is clicked."""
    cdp = FakeCdp(
        [
            ("fp_marker_close_settings", {"ok": True}),
            ("fp_marker_focus", {"ok": True}),
            ("fp_marker_open_settings", {"ok": True}),
            ("fp_marker_confirm_setting", {"ok": True, "value": "Always"}),
            ("fp_marker_close_settings", {"ok": True}),
        ]
    )
    ui = FlowUi(cdp, sleep_fn=lambda _s: None, screenshot_dir=tmp_path)
    assert ui.ensure_confirm_always() is False
    assert not any("fp_marker_set_confirm_always" in e for e in cdp.expressions)
