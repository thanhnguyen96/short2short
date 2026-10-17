"""URLs, hosts, and timeouts for the flow.google.com UI adapter."""

from __future__ import annotations

FLOW_PAGE_URL: str = "https://flow.google.com/"
FLOW_PAGE_HOSTS: tuple[str, ...] = ("flow.google.com",)
MEDIA_HOST: str = "flow-content.google"
PROJECT_TITLE: str = "short2short"
PROJECT_RECORD_NAME: str = "_project.json"

PROMPT_SUFFIX_TEMPLATE: str = " Aspect: {aspect_phrase}. One video, no other changes."
ASPECT_PHRASE: dict[str, str] = {
    "9:16": "vertical 9:16",
    "16:9": "16:9",
    "1:1": "1:1",
}

LOGIN_TIMEOUT_SECONDS: int = 300
REPLY_TIMEOUT_SECONDS: float = 60.0
APPROVE_TIMEOUT_SECONDS: float = 20.0
NAV_TIMEOUT_SECONDS: float = 30.0
MEDIA_POLL_SECONDS: float = 600.0
POLL_INTERVAL_SECONDS: float = 5.0
SEND_CONFIRM_SECONDS: float = 10.0
EVAL_TIMEOUT_SECONDS: float = 30.0
DOWNLOAD_FETCH_TIMEOUT_SECONDS: float = 120.0
SCREENSHOT_TIMEOUT_SECONDS: float = 15.0

MAX_REPLY_CHARS: int = 4000
MAX_ERROR_TAIL: int = 300
DOCTOR_TAIL_CHARS: int = 200

APPROVE_TEXT_RE: str = r"^(check\s*)?Approve$"
LOGIN_HOST_DENY: str = "accounts.google.com"

POLICY_HINTS: tuple[str, ...] = (
    "SAFETY",
    "POLICY",
    "PROHIBITED",
    "UNSAFE",
    "BLOCKED",
    "CAN'T GENERATE",
    "CANNOT GENERATE",
    "NOT ALLOWED",
    "VIOLAT",
)
REFUSAL_HINTS: tuple[str, ...] = (
    "I CAN'T",
    "I CANNOT",
    "UNABLE TO",
    "I'M NOT ABLE",
    "I AM NOT ABLE",
)
ERROR_HINTS: tuple[str, ...] = (
    "SOMETHING WENT WRONG",
    "TRY AGAIN",
    "FAILED",
    "ERROR",
)
PROGRESS_HINTS: tuple[str, ...] = (
    "GENERATING",
    "QUEUE",
    "HIGH DEMAND",
)

CDP_PORT_FILE_NAME: str = "cdp-port"
