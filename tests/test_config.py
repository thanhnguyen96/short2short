from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from short2short.config import (
    AppConfig,
    delete_secret,
    get_secret,
    load_config,
    save_config,
    set_secret,
)
from short2short.errors import ConfigError


def test_load_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.cdp_host == "127.0.0.1"
    assert cfg.tasks_dir == tmp_path / ".short2short" / "tasks"
    assert cfg.cache_dir == tmp_path / ".short2short" / "cache"
    assert cfg.chrome_profile_dir == tmp_path / ".short2short" / "chrome-profile"


def test_config_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg = AppConfig(
        ffmpeg_path="custom_ffmpeg",
        max_clips=7,
        tts_voice="en-US-JennyNeural",
    )
    save_config(cfg, cfg_file)
    loaded = load_config(cfg_file)
    assert loaded.ffmpeg_path == "custom_ffmpeg"
    assert loaded.max_clips == 7
    assert loaded.tts_voice == "en-US-JennyNeural"


def test_load_config_secret_key_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('llm_api_key = "secret"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="Secret key"):
        load_config(cfg_file)

    cfg_file.write_text('flow_token = "secret"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="Secret key"):
        load_config(cfg_file)


def test_load_config_relative_dirs_resolve_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'tasks_dir = "my-tasks"\ncache_dir = "my-cache"\n',
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.tasks_dir == fake_home / ".short2short" / "my-tasks"
    assert loaded.cache_dir == fake_home / ".short2short" / "my-cache"


def test_load_config_invalid_cdp_host_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('cdp_host = "0.0.0.0"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="cdp_host"):
        load_config(cfg_file)


def test_get_secret_env_beats_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHORT2SHORT_LLM_API_KEY", "env_secret_val")
    mock_get_pwd = MagicMock(return_value="keyring_secret_val")
    monkeypatch.setattr("keyring.get_password", mock_get_pwd)

    assert get_secret("llm_api_key") == "env_secret_val"
    mock_get_pwd.assert_not_called()


def test_get_secret_from_keyring_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHORT2SHORT_LLM_API_KEY", raising=False)
    mock_get_pwd = MagicMock(return_value="keyring_val")
    monkeypatch.setattr("keyring.get_password", mock_get_pwd)

    assert get_secret("llm_api_key") == "keyring_val"
    mock_get_pwd.assert_called_once()


def test_get_secret_unknown_name_raises() -> None:
    with pytest.raises(ConfigError, match="Unknown secret name"):
        get_secret("unknown_secret_name")


def test_get_secret_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHORT2SHORT_LLM_API_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda svc, name: None)

    with pytest.raises(ConfigError, match="Missing secret 'llm_api_key'"):
        get_secret("llm_api_key")


def test_set_and_delete_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_set = MagicMock()
    mock_del = MagicMock()
    monkeypatch.setattr("keyring.set_password", mock_set)
    monkeypatch.setattr("keyring.delete_password", mock_del)

    set_secret("pexels_api_key", "secret123")
    mock_set.assert_called_once_with("short2short", "pexels_api_key", "secret123")

    delete_secret("pexels_api_key")
    mock_del.assert_called_once_with("short2short", "pexels_api_key")

    with pytest.raises(ConfigError):
        set_secret("pexels_api_key", "   ")


def test_load_config_flow_fields_defaults() -> None:
    cfg = AppConfig()
    assert cfg.flow_max_credits_per_clip == 10
    assert cfg.flow_auto_approve is True
    assert "flow-content.google" in cfg.download_allow_hosts


def test_load_config_invalid_flow_max_credits_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("flow_max_credits_per_clip = 0\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(cfg_file)


def test_load_config_flow_fields_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        "flow_max_credits_per_clip = 4\nflow_auto_approve = false\n",
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.flow_max_credits_per_clip == 4
    assert loaded.flow_auto_approve is False
