"""Tests for pure parsers in flow.parse."""

from __future__ import annotations

import pytest

from short2short.errors import ProviderError
from short2short.providers.flow.parse import (
    ApprovalInfo,
    ReplyKind,
    classify_reply,
    compose_user_message,
    new_media_uuids,
    parse_approval,
    redact_media_url,
    uuid_from_media_url,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Approve\nThis will use 10 credits.\nVeo 3.1 - Lite", ApprovalInfo(10, "Veo 3.1 - Lite")),
        ("check Approve 10 credits Veo 3.1-Lite", ApprovalInfo(10, "Veo 3.1 - Lite")),
        ("Use 10 credit to generate", ApprovalInfo(10, None)),
        ("Approve generation", None),
        ("10 credits Veo 3.1 - Fast", ApprovalInfo(10, "Veo 3.1 - Fast")),
        ("1000000 credits", None),
        ("", None),
        ("0 credits", ApprovalInfo(0, None)),
    ],
)
def test_parse_approval(text: str, expected: ApprovalInfo | None) -> None:
    """Verify parse_approval extracts credits and normalizes model name per spec table."""
    assert parse_approval(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Approve\n10 credits\nVeo 3.1 - Lite", ReplyKind.APPROVAL),
        ("10 credits", ReplyKind.UNKNOWN),
        ("I can't generate that request", ReplyKind.REFUSAL),
        ("This request violates our policy", ReplyKind.REFUSAL),
        ("Something went wrong. Try again.", ReplyKind.ERROR),
        ("generation failed", ReplyKind.ERROR),
        ("generating failed", ReplyKind.ERROR),
        ("14%", ReplyKind.PROGRESS),
        (
            "generating your drone shot using Veo 3.1 - Lite. It's currently in the queue due to "
            "high demand",
            ReplyKind.PROGRESS,
        ),
        ("32%", ReplyKind.PROGRESS),
        ("generating", ReplyKind.PROGRESS),
        ("generating media.", ReplyKind.PROGRESS),
        ("hello", ReplyKind.UNKNOWN),
        ("", ReplyKind.UNKNOWN),
        ("a storm against the cliffs", ReplyKind.UNKNOWN),
    ],
)
def test_classify_reply(text: str, expected: ReplyKind) -> None:
    """Verify classify_reply prioritizes approval, refusal, error, progress per spec and R3."""
    assert classify_reply(text) == expected


@pytest.mark.parametrize(
    ("url", "expected_uuid", "expected_redacted"),
    [
        (
            "https://flow-content.google/video/11111111-2222-3333-4444-555555555555?sig=secret&Expires=1",
            "11111111-2222-3333-4444-555555555555",
            "https://flow-content.google/video/11111111-2222-3333-4444-555555555555",
        ),
        (
            "https://flow-content.google/image/abc",
            None,
            "https://flow-content.google/image/abc",
        ),
        (
            "not a url",
            None,
            "not a url",
        ),
        (
            "https://evil.example/video/11111111-2222-3333-4444-555555555555",
            None,
            "https://evil.example/video/11111111-2222-3333-4444-555555555555",
        ),
    ],
)
def test_media_urls(url: str, expected_uuid: str | None, expected_redacted: str) -> None:
    """Verify uuid extraction and URL redaction per spec section 4.4 table."""
    assert uuid_from_media_url(url) == expected_uuid
    assert redact_media_url(url) == expected_redacted


def test_compose_user_message_success() -> None:
    """Compose user prompt with aspect suffix preserving quotes and trailing newlines."""
    assert (
        compose_user_message('He said "hi"\n', "9:16")
        == 'He said "hi"\n Aspect: vertical 9:16. One video, no other changes.'
    )
    assert (
        compose_user_message("drone over ocean", "16:9")
        == "drone over ocean Aspect: 16:9. One video, no other changes."
    )
    assert (
        compose_user_message("square scene", "1:1")
        == "square scene Aspect: 1:1. One video, no other changes."
    )


@pytest.mark.parametrize("empty_prompt", ["", "   ", "\n\t  "])
def test_compose_user_message_empty(empty_prompt: str) -> None:
    """Empty or whitespace prompt raises ProviderError."""
    with pytest.raises(ProviderError, match="Prompt cannot be empty"):
        compose_user_message(empty_prompt, "9:16")


def test_compose_user_message_invalid_aspect() -> None:
    """Unknown aspect ratio raises ProviderError."""
    with pytest.raises(ProviderError, match="Unknown aspect"):
        compose_user_message("valid prompt", "4:3")


def test_new_media_uuids() -> None:
    """Return sorted difference current - baseline."""
    assert new_media_uuids({"a"}, {"a", "b"}) == ["b"]
    assert new_media_uuids(set(), {"c", "a", "b"}) == ["a", "b", "c"]
    assert new_media_uuids({"a", "b"}, {"a", "b"}) == []
    assert new_media_uuids(frozenset({"a"}), frozenset({"a", "b"})) == ["b"]
