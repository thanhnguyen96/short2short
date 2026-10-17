"""Scripted CDP session for tests."""

from __future__ import annotations

import base64
import json
from typing import Any


class FakeCdp:
    """Scripted CDP. evaluate matches a marker substring; send matches the CDP method.

    scripts: list of (marker, payload). payload is a dict (encoded with json.dumps),
    a str returned raw, or an Exception instance to raise.
    First matching unused script wins. Each script is consumed once.
    Unmatched visibility checks report a visible tab.
    Other unmatched evaluate raises AssertionError naming the expression's first 80 chars.
    """

    def __init__(self, scripts: list[tuple[str, Any]] | None = None) -> None:
        self.scripts: list[tuple[str, Any]] = list(scripts or [])
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.expressions: list[str] = []

    def evaluate(self, expression: str, *, timeout: float = 60.0) -> str:
        """Match expression by marker substring and return or raise payload."""
        self.expressions.append(expression)
        for i, (marker, payload) in enumerate(self.scripts):
            if marker in expression:
                self.scripts.pop(i)
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, str):
                    return payload
                return json.dumps(payload)
        if "fp_marker_visibility" in expression:
            return json.dumps({"state": "visible"})
        raise AssertionError(f"Unmatched evaluate: {expression[:80]}")

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Match method by name and return or raise payload."""
        self.sent.append((method, params or {}))
        for i, (marker, payload) in enumerate(self.scripts):
            if marker == method:
                self.scripts.pop(i)
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, dict):
                    return payload
                if isinstance(payload, str):
                    parsed: dict[str, Any] = json.loads(payload)
                    return parsed
        if method == "Page.captureScreenshot":
            return {"result": {"data": base64.b64encode(b"png").decode()}}
        return {"id": 1, "result": {}}
