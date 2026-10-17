from __future__ import annotations

import contextlib
import os
import re
import tomllib
import uuid
from pathlib import Path
from typing import Any

import keyring
import keyring.errors
import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from short2short.errors import ConfigError
from short2short.security import KEYRING_SERVICE

SECRET_NAMES: frozenset[str] = frozenset(
    {"llm_api_key", "pexels_api_key", "veo_api_key", "flow_cookies", "flow_access_token"}
)


def default_config_dir() -> Path:
    """Return default configuration directory (~/.short2short)."""
    return Path.home() / ".short2short"


def config_path() -> Path:
    """Return path to config.toml (~/.short2short/config.toml)."""
    return default_config_dir() / "config.toml"


class AppConfig(BaseModel):
    """Application configuration model with sensible defaults."""

    model_config = ConfigDict(extra="ignore")

    ffmpeg_path: str = "ffmpeg"
    tasks_dir: Path = Field(default_factory=lambda: default_config_dir() / "tasks")
    cache_dir: Path = Field(default_factory=lambda: default_config_dir() / "cache")
    chrome_path: str = ""
    chrome_profile_dir: Path = Field(
        default_factory=lambda: default_config_dir() / "chrome-profile"
    )
    cdp_host: str = "127.0.0.1"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    tts_voice: str = "vi-VN-HoaiMyNeural"
    subtitle_font_size: int = 18
    bgm_volume: float = 0.15
    max_parallel_jobs: int = 2
    max_clips: int = 12
    max_total_seconds: float = 120.0
    scene_count: int = 5
    scene_seconds: int = 8
    poll_interval_seconds: float = 5.0
    poll_timeout_seconds: float = 600.0
    veo_model: str = "veo-3.1-generate-preview"
    # Unused. Model is chosen in the Flow UI; the adapter only reads the approval card.
    flow_video_model: str = "veo_3_1_t2v_fast_ultra"
    flow_max_credits_per_clip: int = Field(default=10, ge=1)
    flow_auto_approve: bool = True
    download_allow_hosts: list[str] = Field(
        default_factory=lambda: [
            "videos.pexels.com",
            "images.pexels.com",
            "storage.googleapis.com",
            "flow-content.google",
        ]
    )
    fit_mode: str = "cover"  # "cover" | "contain"


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from TOML file.

    Missing file returns default AppConfig.
    Relative tasks_dir/cache_dir/chrome_profile_dir resolve against default_config_dir().
    Keys ending in api_key|secret|cookie|cookies|token or cdp_host != 127.0.0.1 raise ConfigError.
    """
    target_path = path if path is not None else config_path()
    if not target_path.is_file():
        return AppConfig()

    with open(target_path, "rb") as f:
        data = tomllib.load(f)

    secret_key_pattern = re.compile(r"(?:api_key|secret|cookies?|token)$", re.IGNORECASE)
    for key in data:
        if secret_key_pattern.search(key):
            raise ConfigError(
                f"Secret key {key!r} not allowed in config file; store via keyring or env vars"
            )

    if "cdp_host" in data and data["cdp_host"] != "127.0.0.1":
        raise ConfigError(f"cdp_host must be '127.0.0.1', got {data['cdp_host']!r}")

    for dir_key in ("tasks_dir", "cache_dir", "chrome_profile_dir"):
        if dir_key in data:
            val = Path(data[dir_key])
            if not val.is_absolute():
                data[dir_key] = default_config_dir() / val

    return AppConfig.model_validate(data)


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Save configuration to TOML file using tomli_w.

    Atomic write via temporary file and replace. Paths written as strings.
    """
    target_path = path if path is not None else config_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = config.model_dump()
    for k, v in list(data.items()):
        if isinstance(v, Path):
            data[k] = str(v)

    temp_file = target_path.with_name(f"{target_path.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        with open(temp_file, "wb") as f:
            tomli_w.dump(data, f)
        os.replace(temp_file, target_path)
    finally:
        if temp_file.exists():
            with contextlib.suppress(OSError):
                temp_file.unlink()


def get_secret(name: str) -> str:
    """Retrieve secret by name. Environment variable SHORT2SHORT_<NAME.UPPER()> takes precedence."""
    if name not in SECRET_NAMES:
        raise ConfigError(f"Unknown secret name: {name!r}. Must be one of {sorted(SECRET_NAMES)}")

    env_var = f"SHORT2SHORT_{name.upper()}"
    val = os.environ.get(env_var)
    if val is not None and val != "":
        return val

    try:
        val = keyring.get_password(KEYRING_SERVICE, name)
    except Exception:
        val = None

    if val is not None and val != "":
        return val

    raise ConfigError(
        f"Missing secret {name!r}. Set via environment variable {env_var} "
        f"or run `short2short config set-secret {name}`."
    )


def set_secret(name: str, value: str) -> None:
    """Store secret in keyring."""
    if name not in SECRET_NAMES:
        raise ConfigError(f"Unknown secret name: {name!r}. Must be one of {sorted(SECRET_NAMES)}")
    if not value or not value.strip():
        raise ConfigError("Secret value cannot be empty")
    keyring.set_password(KEYRING_SERVICE, name, value)


def delete_secret(name: str) -> None:
    """Delete secret from keyring, ignoring PasswordDeleteError."""
    if name not in SECRET_NAMES:
        raise ConfigError(f"Unknown secret name: {name!r}. Must be one of {sorted(SECRET_NAMES)}")
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception:
        pass
