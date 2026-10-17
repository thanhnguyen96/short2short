from __future__ import annotations

import os
import re
from pathlib import Path

from short2short.errors import PathSecurityError

KEYRING_SERVICE: str = "short2short"


def redact(text: str) -> str:
    """Replace secret-shaped substrings with '***' while keeping key names.

    Case-insensitive matching for:
    - cookie and authorization headers/keys to end of line
    - api_key, apikey, token, bearer, secret to whitespace/quote/&/,
    - Bearer <nonspace>
    - sk-... and ya29.... tokens
    - query values for key=, access_token=, token=, signature=, sig=
    """
    # 1. cookie and authorization to end of line
    text = re.sub(
        r'(?i)\b(["\']?(?:cookie|authorization)["\']?\s*[:=]\s*)(?:["\']?[^\r\n]+)',
        r"\g<1>***",
        text,
    )

    # 2. api_key, apikey, token, bearer, secret to whitespace/quote/&/,
    text = re.sub(
        r'(?i)\b(["\']?(?:api_key|apikey|token|bearer|secret)["\']?\s*[:=]\s*)(?:["\']?[^"\'\s,;&\r\n]+["\']?)',
        r"\g<1>***",
        text,
    )

    # 3. Query string parameters for key, access_token, token, signature, sig
    text = re.sub(
        r"(?i)([?&](?:key|access_token|token|signature|sig)=)[^&\s#]+",
        r"\g<1>***",
        text,
    )
    text = re.sub(
        r"(?i)\b((?:signature|sig|access_token)=)[^&\s#]+",
        r"\g<1>***",
        text,
    )

    # 4. Bearer <nonspace>
    text = re.sub(
        r"(?i)\b(bearer\s+)\S+",
        r"\g<1>***",
        text,
    )

    # 5. sk-... OpenAI style keys
    text = re.sub(
        r"\bsk-[A-Za-z0-9_-]+",
        "***",
        text,
    )

    # 6. ya29.... Google OAuth style tokens
    text = re.sub(
        r"\bya29\.[A-Za-z0-9_-]+",
        "***",
        text,
    )

    return text


def safe_join(root: Path, *parts: str) -> Path:
    """Safely resolve root and parts, preventing directory traversal.

    Rejects empty, absolute, drive-qualified parts and any path outside realpath(root).
    """
    root_real = os.path.realpath(str(root))

    target = Path(root_real)
    for part in parts:
        if not part or not part.strip():
            raise PathSecurityError(f"Empty path component rejected: {part!r}")
        part_path = Path(part)
        if part_path.is_absolute() or os.path.isabs(part):
            raise PathSecurityError(f"Absolute path component rejected: {part!r}")
        if os.path.splitdrive(part)[0]:
            raise PathSecurityError(f"Drive-qualified path component rejected: {part!r}")
        target = target / part

    target_real = os.path.realpath(str(target))
    try:
        common = os.path.commonpath([root_real, target_real])
    except ValueError as exc:
        raise PathSecurityError(f"Path traversal outside root: {target}") from exc

    if common != root_real:
        raise PathSecurityError(f"Path traversal outside root: {target} escapes {root_real}")

    return Path(target_real)


def assert_loopback_host(host: str) -> None:
    """Raise PathSecurityError unless host is '127.0.0.1'."""
    if host != "127.0.0.1":
        raise PathSecurityError(f"Host must be '127.0.0.1', got: {host!r}")
