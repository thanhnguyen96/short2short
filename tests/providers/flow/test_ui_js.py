"""Tests for ui_js snippet generators and constants."""

from __future__ import annotations

import inspect
import json

import pytest

from short2short.providers.flow import ui_js


def test_public_snippets_contain_markers() -> None:
    """Each public snippet string must contain its designated marker."""
    markers = [
        (ui_js.JS_LOGGED_IN, "fp_marker_logged_in"),
        (ui_js.JS_FOCUS_COMPOSER, "fp_marker_focus"),
        (ui_js.JS_PROJECTS, "fp_marker_projects"),
        (ui_js.JS_NEW_PROJECT, "fp_marker_new_project"),
        (ui_js.js_new_project(), "fp_marker_new_project"),
        (ui_js.JS_MEDIA_SNAPSHOT, "fp_marker_media"),
        (ui_js.JS_TILE_COUNT, "fp_marker_tile_count"),
        (ui_js.js_open_tile(0), "fp_marker_open_tile"),
        (ui_js.JS_VISIBILITY, "fp_marker_visibility"),
        (ui_js.JS_CLICK_SEND, "fp_marker_click_send"),
        (ui_js.JS_LATEST_REPLY, "fp_marker_reply"),
        (ui_js.JS_APPROVAL_TEXT, "fp_marker_approval"),
        (ui_js.JS_PROGRESS, "fp_marker_progress"),
        (ui_js.JS_CLICK_APPROVE, "fp_marker_click_approve"),
        (ui_js.JS_APPROVE_STILL_THERE, "fp_marker_approve_present"),
        (ui_js.js_open_project("https://flow.google.com/project/p1"), "fp_marker_open_project"),
        (
            ui_js.js_fetch_media_b64("https://flow-content.google/video/abc"),
            "fp_marker_fetch_media",
        ),
        (ui_js.js_media_url_from_performance(), "fp_marker_perf_url"),
    ]
    for snippet, marker in markers:
        assert marker in snippet, f"Marker {marker} missing from snippet"


def test_open_project_rejects_non_flow_url() -> None:
    """js_open_project raises ValueError if scheme or host is not flow."""
    with pytest.raises(ValueError, match="refusing non-flow project url"):
        ui_js.js_open_project("https://evil.example/project/1")

    with pytest.raises(ValueError, match="refusing non-flow project url"):
        ui_js.js_open_project("http://flow.google.com/project/1")


def test_open_project_quotes_url_safely() -> None:
    """Target URL appears exactly once inside the snippet as a json-dumped literal."""
    url = "https://flow.google.com/project/abc"
    snippet = ui_js.js_open_project(url)
    quoted = json.dumps(url)
    assert quoted in snippet
    assert snippet.count(url) == 1


def test_fetch_media_rejects_non_media_url() -> None:
    """js_fetch_media_b64 raises ValueError on non-https or non-media host."""
    with pytest.raises(ValueError, match="refusing non-media url"):
        ui_js.js_fetch_media_b64("http://flow-content.google/video/x")

    with pytest.raises(ValueError, match="refusing non-media url"):
        ui_js.js_fetch_media_b64("https://evil.example/video/x")


def test_fetch_media_contains_credentials_omit_and_quoted_url() -> None:
    """js_fetch_media_b64 contains credentials: 'omit' and URL once as JSON string."""
    url = "https://flow-content.google/video/11111111-2222-3333-4444-555555555555"
    snippet = ui_js.js_fetch_media_b64(url)
    assert "credentials: 'omit'" in snippet
    assert json.dumps(url) in snippet
    assert snippet.count(url) == 1


def test_no_builder_takes_prompt_parameter() -> None:
    """Assert ui_js source has no function parameter named prompt."""
    functions = inspect.getmembers(ui_js, inspect.isfunction)
    for name, fn in functions:
        sig = inspect.signature(fn)
        assert "prompt" not in sig.parameters, f"Function {name} has prompt parameter"


def test_open_project_matches_exact_url_not_subpages() -> None:
    js = ui_js.js_open_project("https://flow.google.com/project/abc")
    assert "startsWith" not in js
    assert "norm(a.href) === norm(targetUrl)" in js


def test_composer_snippets_scoped_to_agent_panel() -> None:
    """Page-level prompt box generates without an approval card; only the agent panel is used."""
    for snippet in (ui_js.JS_FOCUS_COMPOSER, ui_js.JS_COMPOSER_TEXT, ui_js.JS_CLICK_SEND):
        assert "fpComposer()" in snippet or "fpAgentPanel()" in snippet
    assert "flow-agent-panel" in ui_js.JS_HELPERS


def test_approval_snippets_skip_answered_cards() -> None:
    """Answered cards keep aria-disabled options in the chat; only open cards count."""
    assert "aria-disabled" in ui_js.JS_HELPERS
    for snippet in (ui_js.JS_APPROVAL_TEXT, ui_js.JS_CLICK_APPROVE, ui_js.JS_APPROVE_STILL_THERE):
        assert "fpApproveEls()" in snippet
