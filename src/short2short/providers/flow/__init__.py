"""EXPERIMENTAL. Unofficial Google Flow (flow.google.com) adapter.

Private UI changes without notice and may violate Google Terms of Service.
Drives the visible Flow UI through actions a user would take (focus the composer,
insert text, press Enter, click Approve). Uses a dedicated Chrome profile on
loopback CDP. Does not bypass safety filters, rewrite prompts, add humanizer
delays or mouse jitter, rotate proxies, or solve captchas.
"""

from __future__ import annotations

from short2short.providers.flow.auth import CdpPage, login, logout, status
from short2short.providers.flow.provider import FlowProvider, build_flow_provider
from short2short.providers.flow.ui import CdpSession, FlowUi

__all__ = [
    "CdpPage",
    "CdpSession",
    "FlowProvider",
    "FlowUi",
    "build_flow_provider",
    "login",
    "logout",
    "status",
]
