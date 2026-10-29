"""Apple Music Lossless provider implementation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from music_assistant.providers.apple_music.provider import AppleMusicProvider
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CACHE_ENV_DIR,
    CACHE_ENV_ENABLED,
    CACHE_ENV_MAX_MB,
    CONF_CACHE_DIR,
    CONF_CACHE_ENABLED,
    CONF_CACHE_MAX_SIZE_MB,
    CONF_FAIRPLAY_GATEWAY,
    CONF_FAIRPLAY_TOKEN,
    DEFAULT_CACHE_ENABLED,
    DEFAULT_CACHE_MAX_SIZE_MB,
    DEFAULT_FAIRPLAY_GATEWAY,
    DEFAULT_FAIRPLAY_TOKEN,
)
from .streaming import AppleMusicLosslessStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant import MusicAssistant
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails


def _cache_config_entries() -> tuple[ConfigEntry, ...]:
    """Return the track cache settings exposed in the provider configuration."""
    return (
        ConfigEntry(
            key=CONF_CACHE_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            default_value=DEFAULT_CACHE_ENABLED,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_CACHE_MAX_SIZE_MB,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_CACHE_MAX_SIZE_MB,
            range=(0, 1_000_000),
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_CACHE_DIR,
            type=ConfigEntryType.STRING,
            default_value="",
            advanced=True,
            requires_reload=True,
        ),
    )


def _as_bool(value: Any) -> bool:
    """Coerce a configuration or environment value to a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    if isinstance(value, int | float):
        return value != 0
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    """Coerce a configuration or environment value to an integer."""
    if isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _default_cache_dir(mass: MusicAssistant) -> Path:
    """Return the host application's cache location, with a portable fallback."""
    for attribute in ("cache_path", "storage_path", "data_path"):
        base = getattr(mass, attribute, None)
        if base:
            try:
                return Path(base)
            except TypeError:
                continue
    return Path(tempfile.gettempdir())


class AppleMusicLosslessProvider(AppleMusicProvider):
    """Apple Music provider with ALAC lossless streaming and a local track cache."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize the lossless Apple Music provider."""
        super().__init__(mass, manifest, config)
        self.streaming_manager = AppleMusicLosslessStreamingManager(self)

    @property
    def stream_format_supersedes_catalog(self) -> bool:
        """Let a played ALAC stream update the AAC format stored on library mappings."""
        return True

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Add the track cache settings to the inherited provider configuration."""
        base_entries = tuple(await super().get_config_entries())
        known_keys = {entry.key for entry in base_entries}
        return (
            *base_entries,
            *(entry for entry in _cache_config_entries() if entry.key not in known_keys),
        )

    async def handle_async_init(self) -> None:
        """Initialize the provider, configure FairPlay and register local routes."""
        await super().handle_async_init()
        self.streaming_manager.configure_fairplay(
            str(self.get_setup_value(CONF_FAIRPLAY_GATEWAY, DEFAULT_FAIRPLAY_GATEWAY)),
            str(self.get_setup_value(CONF_FAIRPLAY_TOKEN, DEFAULT_FAIRPLAY_TOKEN)),
        )
        enabled, max_size_mb, cache_dir = self._cache_settings()
        await self.streaming_manager.configure_cache(enabled, max_size_mb, cache_dir)
        self.streaming_manager.register_routes()

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider and stop cache maintenance."""
        await self.streaming_manager.close()
        await super().unload(is_removed)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield the decrypted ALAC stream, caching it for later playback."""
        async for chunk in self.streaming_manager.get_audio_stream(streamdetails, seek_position):
            yield chunk

    async def get_diagnostics(self) -> dict[str, Any]:
        """Return diagnostics including the lossless route and cache state."""
        diagnostics = cast("dict[str, Any]", await super().get_diagnostics())
        diagnostics["lossless"] = {
            "cdm_available": self.streaming_manager.cdm_available,
            "route": self.streaming_manager.route_url,
            "cache": await self.streaming_manager.cache_diagnostics(),
        }
        return diagnostics

    def _cache_settings(self) -> tuple[bool, int, Path]:
        """Return the effective cache enabled flag, size limit and directory."""
        enabled = _as_bool(self._get_option(CONF_CACHE_ENABLED, DEFAULT_CACHE_ENABLED))
        max_size_mb = _as_int(
            self._get_option(CONF_CACHE_MAX_SIZE_MB, DEFAULT_CACHE_MAX_SIZE_MB),
            DEFAULT_CACHE_MAX_SIZE_MB,
        )
        directory = str(self._get_option(CONF_CACHE_DIR, "") or "").strip()
        if (value := os.environ.get(CACHE_ENV_ENABLED)) is not None:
            enabled = _as_bool(value)
        if (value := os.environ.get(CACHE_ENV_MAX_MB)) is not None:
            max_size_mb = _as_int(value, max_size_mb)
        if (value := os.environ.get(CACHE_ENV_DIR)) is not None:
            directory = value.strip()
        base = Path(directory).expanduser() if directory else _default_cache_dir(self.mass)
        if not directory:
            base = base / "apple_music_lossless"
        return enabled, max(0, max_size_mb), base / str(self.instance_id)

    def _get_option(self, key: str, default: Any) -> Any:
        """Read a provider option from config values or setup data."""
        try:
            value = self.config.get_value(key)
        except (AttributeError, KeyError, TypeError, ValueError):
            value = None
        if value is None:
            try:
                value = self.get_setup_value(key, default)
            except (AttributeError, KeyError, TypeError, ValueError):
                return default
        return default if value is None else value
