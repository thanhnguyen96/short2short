from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from short2short.config import AppConfig
from short2short.errors import ConfigError
from short2short.providers.base import VideoProvider
from short2short.providers.builtins import PROVIDER_INFO, register_builtins
from short2short.providers.registry import clear, get, names
from short2short.providers.stock import StockProvider
from short2short.providers.veo_api import VeoApiProvider


def test_provider_info() -> None:
    """Verify metadata mappings for all built-in providers."""
    assert PROVIDER_INFO == {
        "fake": (False, 30),
        "stock": (False, 60),
        "veo_api": (True, 8),
        "flow": (True, 8),
    }


def test_register_builtins_lifecycle_and_idempotency() -> None:
    """Register builtins after clear, verifying name order and idempotency."""
    clear()
    assert names() == []

    register_builtins()
    assert names() == ["fake", "stock", "veo_api", "flow"]

    # Calling a second time must preserve insertion order and state
    register_builtins()
    assert names() == ["fake", "stock", "veo_api", "flow"]


def test_get_fake(tmp_path: Path) -> None:
    """Factory for fake provider instantiates FakeProvider without external calls."""
    clear()
    register_builtins()

    config = AppConfig(cache_dir=tmp_path, ffmpeg_path="ffmpeg")
    provider = get("fake", config)

    assert isinstance(provider, VideoProvider)
    assert provider.name == "fake"
    assert provider.paid is False
    assert provider.max_clip_seconds == 30


def test_get_stock_missing_secret_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory for stock provider raises ConfigError if pexels_api_key secret is missing."""
    clear()
    register_builtins()

    def mock_missing_secret(name: str) -> str:
        raise ConfigError(f"Missing secret {name!r}")

    monkeypatch.setattr("short2short.providers.builtins.get_secret", mock_missing_secret)

    config = AppConfig(cache_dir=tmp_path)
    with pytest.raises(ConfigError, match="Missing secret 'pexels_api_key'"):
        get("stock", config)


def test_get_stock_with_secret_returns_stock_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory for stock provider returns StockProvider configured with cache and allow hosts."""
    clear()
    register_builtins()

    monkeypatch.setattr(
        "short2short.providers.builtins.get_secret",
        lambda name: "mock-pexels-key",
    )

    allow_hosts = ["videos.pexels.com", "custom.cdn.com"]
    config = AppConfig(cache_dir=tmp_path, download_allow_hosts=allow_hosts)

    provider = get("stock", config)
    assert isinstance(provider, StockProvider)
    assert provider.name == "stock"
    assert provider.api_key == "mock-pexels-key"
    assert provider.cache.root == tmp_path.resolve()
    assert provider.allow_hosts == set(allow_hosts)


def test_get_veo_api_missing_secret_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory for veo_api provider raises ConfigError if veo_api_key secret is missing."""
    clear()
    register_builtins()

    def mock_missing_secret(name: str) -> str:
        raise ConfigError(f"Missing secret {name!r}")

    monkeypatch.setattr("short2short.providers.builtins.get_secret", mock_missing_secret)

    config = AppConfig(cache_dir=tmp_path)
    with pytest.raises(ConfigError, match="Missing secret 'veo_api_key'"):
        get("veo_api", config)


def test_get_veo_api_with_secret_returns_veo_api_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory for veo_api provider returns VeoApiProvider configured with model and timeouts."""
    clear()
    register_builtins()

    monkeypatch.setattr(
        "short2short.providers.builtins.get_secret",
        lambda name: "mock-veo-key",
    )

    config = AppConfig(
        cache_dir=tmp_path,
        veo_model="veo-3.1-custom-preview",
        poll_interval_seconds=4.0,
        poll_timeout_seconds=300.0,
    )

    provider = get("veo_api", config)
    assert isinstance(provider, VeoApiProvider)
    assert provider.name == "veo_api"
    assert provider.model == "veo-3.1-custom-preview"
    assert provider.poll_interval_seconds == 4.0
    assert provider.poll_timeout_seconds == 300.0
    assert provider.cache.root == tmp_path.resolve()


def test_get_flow_lazy_factory_invoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory for flow provider calls build_flow_provider with config and ClipCache."""
    clear()
    register_builtins()

    mock_builder = MagicMock()
    monkeypatch.setattr("short2short.providers.flow.provider.build_flow_provider", mock_builder)

    config = AppConfig(cache_dir=tmp_path)
    get("flow", config)

    mock_builder.assert_called_once()
    args, _ = mock_builder.call_args
    assert args[0] is config
    assert args[1].root == tmp_path.resolve()
