"""
Apple Music Lossless music provider support for Music Assistant.

This is an experimental fork of the regular Apple Music provider. Catalog/search/
library work is inherited; only stream resolution is replaced with ALAC support.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import AppleMusicLosslessProvider

__all__ = ["AppleMusicLosslessProvider", "setup"]

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AppleMusicLosslessProvider(mass, manifest, config)
