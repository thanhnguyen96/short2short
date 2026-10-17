from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from short2short.errors import ConfigError

if TYPE_CHECKING:
    from short2short.config import AppConfig
    from short2short.providers.base import VideoProvider

Factory = Callable[["AppConfig"], "VideoProvider"]

_REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a provider factory function by name."""
    _REGISTRY[name] = factory


def get(name: str, config: AppConfig) -> VideoProvider:
    """Instantiate a provider by name, or raise ConfigError listing available providers."""
    if name not in _REGISTRY:
        avail = ", ".join(names())
        raise ConfigError(f"Unknown provider: {name!r}. Available providers: {avail}")
    return _REGISTRY[name](config)


def names() -> list[str]:
    """Return list of registered provider names in insertion order."""
    return list(_REGISTRY.keys())


def clear() -> None:
    """Clear all registered provider factories."""
    _REGISTRY.clear()
