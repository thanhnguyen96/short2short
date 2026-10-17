from __future__ import annotations

import json
import logging
import math
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from short2short.errors import Short2ShortError
from short2short.security import redact

logger = logging.getLogger(__name__)

Runner = Callable[[list[str]], None]


def run_ffmpeg(bin: str, args: list[str], *, timeout: float = 600) -> None:
    """Run ffmpeg with arguments list.

    Raises TypeError if bin or any arg is not str.
    Raises Short2ShortError on non-zero exit, timeout, or OSError.
    """
    if not isinstance(bin, str):
        raise TypeError(f"bin must be str, got {type(bin).__name__}")
    for idx, arg in enumerate(args):
        if not isinstance(arg, str):
            raise TypeError(f"arg at index {idx} must be str, got {type(arg).__name__}: {arg!r}")

    cmd = [bin, *args]
    try:
        res = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stderr_str = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        tail = stderr_str[-500:] if len(stderr_str) > 500 else stderr_str
        raise Short2ShortError(f"ffmpeg timed out after {timeout}s: {redact(tail)}") from exc
    except OSError as exc:
        raise Short2ShortError(f"ffmpeg failed to execute: {redact(str(exc))}") from exc

    if res.returncode != 0:
        stderr_str = res.stderr.decode("utf-8", errors="replace") if res.stderr else ""
        tail = stderr_str[-500:] if len(stderr_str) > 500 else stderr_str
        raise Short2ShortError(f"ffmpeg exited with {res.returncode}: {redact(tail)}")


def default_runner(argv: list[str]) -> None:
    """Run command where argv[0] is the binary and argv[1:] are arguments."""
    if not argv:
        raise TypeError("argv must not be empty")
    run_ffmpeg(argv[0], argv[1:])


def ffprobe_duration(bin_ffmpeg: str, path: Path, *, timeout: float = 30) -> float:
    """Probe duration of media file using ffprobe.

    Derives ffprobe binary path from bin_ffmpeg.
    Raises Short2ShortError if ffprobe fails or duration is missing/non-finite.
    """
    p = Path(bin_ffmpeg)
    if p.name.lower() in ("ffmpeg", "ffmpeg.exe"):
        probe_bin = str(p.with_name(f"ffprobe{p.suffix}"))
    else:
        probe_bin = "ffprobe"

    cmd = [probe_bin, "-v", "error", "-print_format", "json", "-show_format", str(path)]
    try:
        res = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise Short2ShortError(f"ffprobe timed out after {timeout}s") from exc
    except OSError as exc:
        raise Short2ShortError(f"ffprobe failed to execute: {exc}") from exc

    if res.returncode != 0:
        stderr_str = res.stderr.decode("utf-8", errors="replace") if res.stderr else ""
        tail = stderr_str[-500:] if len(stderr_str) > 500 else stderr_str
        raise Short2ShortError(f"ffprobe exited with {res.returncode}: {redact(tail)}")

    try:
        stdout_str = res.stdout.decode("utf-8", errors="replace")
        data: dict[str, Any] = json.loads(stdout_str)
        fmt = data.get("format", {})
        dur_val = fmt.get("duration")
        if dur_val is None:
            raise Short2ShortError("ffprobe output missing format.duration")
        duration = float(dur_val)
        if not math.isfinite(duration) or duration < 0:
            raise Short2ShortError(f"ffprobe duration is non-finite or negative: {dur_val!r}")
        return duration
    except (json.JSONDecodeError, ValueError) as exc:
        raise Short2ShortError(f"Failed to parse ffprobe duration: {exc}") from exc
