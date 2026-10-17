"""Pure parsers for Flow agent text and media URLs."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum

from short2short.errors import ProviderError
from short2short.providers.flow.constants import (
    ASPECT_PHRASE,
    ERROR_HINTS,
    POLICY_HINTS,
    PROGRESS_HINTS,
    PROMPT_SUFFIX_TEMPLATE,
    REFUSAL_HINTS,
)

__all__ = [
    "ApprovalInfo",
    "ReplyKind",
    "classify_reply",
    "compose_user_message",
    "new_media_uuids",
    "parse_approval",
    "redact_media_url",
    "uuid_from_media_url",
]


@dataclass(frozen=True)
class ApprovalInfo:
    """Credit cost and model name read from an approval card."""

    credits: int
    model: str | None


class ReplyKind(StrEnum):
    """Classification of one agent-panel text snapshot."""

    APPROVAL = "approval"
    REFUSAL = "refusal"
    ERROR = "error"
    PROGRESS = "progress"
    UNKNOWN = "unknown"


_CREDIT_RE: re.Pattern[str] = re.compile(
    r"\b(\d{1,6})\s+credits?\b",
    re.IGNORECASE,
)
_MODEL_RE: re.Pattern[str] = re.compile(
    r"\b(Veo\s+\d+(?:\.\d+)?(?:\s*-\s*|\s+)(?:Lite|Fast|Quality|Ultra))\b",
    re.IGNORECASE,
)
_MEDIA_UUID_RE: re.Pattern[str] = re.compile(
    r"flow-content\.google/video/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_PROGRESS_PERCENT_RE: re.Pattern[str] = re.compile(r"\b\d{1,3}%")
_APPROVE_WORD_RE: re.Pattern[str] = re.compile(r"\bApprove\b")


def parse_approval(text: str) -> ApprovalInfo | None:
    """Return credits and model, or None if no credit count is present.

    Model may be None when credits parse. Does not raise.
    """
    credit_match = _CREDIT_RE.search(text)
    if not credit_match:
        return None

    credits = int(credit_match.group(1))

    model: str | None = None
    model_match = _MODEL_RE.search(text)
    if model_match:
        raw_model = model_match.group(1)
        model = re.sub(
            r"\b(Veo)\s+(\d+(?:\.\d+)?)(?:\s*-\s*|\s+)(Lite|Fast|Quality|Ultra)\b",
            r"\1 \2 - \3",
            raw_model,
            flags=re.IGNORECASE,
        )

    return ApprovalInfo(credits=credits, model=model)


def classify_reply(text: str) -> ReplyKind:
    """Classify agent text. Approval wins over other hints if a credit count exists."""
    if parse_approval(text) is not None and _APPROVE_WORD_RE.search(text):
        return ReplyKind.APPROVAL

    text_upper = text.upper()
    if any(hint in text_upper for hint in REFUSAL_HINTS) or any(
        hint in text_upper for hint in POLICY_HINTS
    ):
        return ReplyKind.REFUSAL

    if any(hint in text_upper for hint in ERROR_HINTS):
        return ReplyKind.ERROR

    if any(hint in text_upper for hint in PROGRESS_HINTS) or bool(
        _PROGRESS_PERCENT_RE.search(text)
    ):
        return ReplyKind.PROGRESS

    return ReplyKind.UNKNOWN


def uuid_from_media_url(url: str) -> str | None:
    """Return the video uuid or None. Ignores query and fragment."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme and parts.scheme not in {"http", "https"}:
        return None
    if parts.netloc and parts.hostname != "flow-content.google":
        return None
    match = _MEDIA_UUID_RE.search(url)
    return match.group(1) if match else None


def redact_media_url(url: str) -> str:
    """Return scheme://host/path with query and fragment removed.

    Non-URL input is returned unchanged after stripping CR/LF.
    Does not log.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url.replace("\n", " ").replace("\r", " ")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return url.replace("\n", " ").replace("\r", " ")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def compose_user_message(prompt: str, aspect_value: str) -> str:
    """Append the fixed aspect suffix. Prompt text is not rewritten.

    aspect_value is '9:16' | '16:9' | '1:1'. Unknown value raises ProviderError.
    Empty or whitespace prompt raises ProviderError('Prompt cannot be empty').
    """
    if not prompt.strip():
        raise ProviderError("Prompt cannot be empty", provider="flow")
    if aspect_value not in ASPECT_PHRASE:
        raise ProviderError(f"Unknown aspect: {aspect_value}", provider="flow")
    suffix = PROMPT_SUFFIX_TEMPLATE.format(aspect_phrase=ASPECT_PHRASE[aspect_value])
    return f"{prompt}{suffix}"


def new_media_uuids(
    baseline: set[str] | frozenset[str],
    current: set[str] | frozenset[str],
) -> list[str]:
    """Return sorted uuids present in current but not baseline."""
    return sorted(current - baseline)
