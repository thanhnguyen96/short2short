from __future__ import annotations

from typing import TYPE_CHECKING

import short2short.config as _config
from short2short.providers.registry import register

if TYPE_CHECKING:
    from short2short.config import AppConfig
    from short2short.providers.base import VideoProvider

PROVIDER_INFO: dict[str, tuple[bool, int]] = {
    "fake": (False, 30),
    "stock": (False, 60),
    "veo_api": (True, 8),
    "flow": (True, 8),
}


def get_secret(name: str) -> str:
    """Retrieve secret value by name via config manager."""
    return _config.get_secret(name)


def _make_fake(config: AppConfig) -> VideoProvider:
    """Build fake video provider from configuration."""
    from short2short.providers.fake import FakeProvider

    return FakeProvider(bin=config.ffmpeg_path)


def _make_stock(config: AppConfig) -> VideoProvider:
    """Build Pexels stock video provider from configuration and secrets."""
    from short2short.providers.stock import StockProvider
    from short2short.storage.cache import ClipCache

    cache = ClipCache(config.cache_dir)
    api_key = get_secret("pexels_api_key")
    return StockProvider(
        api_key=api_key,
        cache=cache,
        allow_hosts=set(config.download_allow_hosts),
    )


def _make_veo_api(config: AppConfig) -> VideoProvider:
    """Build Veo API video provider from configuration and secrets."""
    from short2short.providers.veo_api import VeoApiProvider
    from short2short.storage.cache import ClipCache

    cache = ClipCache(config.cache_dir)
    api_key = get_secret("veo_api_key")
    return VeoApiProvider(
        api_key=api_key,
        model=config.veo_model,
        cache=cache,
        poll_interval=config.poll_interval_seconds,
        poll_timeout=config.poll_timeout_seconds,
    )


def _make_flow(config: AppConfig) -> VideoProvider:
    """Build Google Flow video provider lazily from configuration."""
    from short2short.providers.flow.provider import build_flow_provider
    from short2short.storage.cache import ClipCache

    cache = ClipCache(config.cache_dir)
    return build_flow_provider(config, cache)


def register_builtins() -> None:
    """Register all built-in video providers in standard order."""
    register("fake", _make_fake)
    register("stock", _make_stock)
    register("veo_api", _make_veo_api)
    register("flow", _make_flow)
