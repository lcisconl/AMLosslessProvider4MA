"""
Setup flow for the Apple Music Lossless provider.

The FairPlay gateway is configured before the normal Apple Music sign-in. The
address and API token are persisted in the provider setup data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError
from music_assistant.providers.apple_music.constants import (
    CONF_MUSIC_APP_TOKEN,
    CONF_MUSIC_USER_MANUAL_TOKEN,
    CONF_MUSIC_USER_TOKEN,
    CONF_MUSIC_USER_TOKEN_TIMESTAMP,
    MUSIC_APP_TOKEN,
)
from music_assistant.providers.apple_music.setup_flow import (
    _app_token_accepted,
    _musickit_authenticate,
)
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CONF_FAIRPLAY_GATEWAY,
    CONF_FAIRPLAY_TOKEN,
    DEFAULT_FAIRPLAY_GATEWAY,
    DEFAULT_FAIRPLAY_TOKEN,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession
    from music_assistant_models.config_entries import ConfigValueType


async def run_setup(session: SetupSession) -> None:
    """
    Run the FairPlay and Apple Music setup flow.

    :param session: The setup session driving the flow.
    """
    mass = session.mass
    collected: dict[str, ConfigValueType] = {}

    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        prefill = session.context.setup_data
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_FAIRPLAY_GATEWAY,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=str(
                        prefill.get(CONF_FAIRPLAY_GATEWAY) or DEFAULT_FAIRPLAY_GATEWAY
                    ),
                    validate=_is_valid_gateway,
                ),
                ConfigEntry(
                    key=CONF_FAIRPLAY_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    required=True,
                    default_value=str(prefill.get(CONF_FAIRPLAY_TOKEN) or DEFAULT_FAIRPLAY_TOKEN),
                ),
            ],
            step_id="fairplay",
            errors=errors,
        )
        gateway = str(values.get(CONF_FAIRPLAY_GATEWAY) or "").strip().rstrip("/")
        token = str(values.get(CONF_FAIRPLAY_TOKEN) or "").strip()
        if _is_valid_gateway(gateway) and token:
            collected[CONF_FAIRPLAY_GATEWAY] = gateway
            collected[CONF_FAIRPLAY_TOKEN] = token
            break
        errors = {
            CONF_FAIRPLAY_GATEWAY: "invalid_value",
            CONF_FAIRPLAY_TOKEN: "invalid_value",
        }

    app_token = MUSIC_APP_TOKEN
    if await _app_token_accepted(mass, app_token) is False:
        app_token_errors: dict[str, str | SetupFlowError] | None = None
        while True:
            values = await session.form(
                [
                    ConfigEntry(
                        key=CONF_MUSIC_APP_TOKEN,
                        type=ConfigEntryType.SECURE_STRING,
                        required=True,
                    )
                ],
                step_id="app_token",
                errors=app_token_errors,
            )
            app_token = str(values.get(CONF_MUSIC_APP_TOKEN) or "")
            if await _app_token_accepted(mass, app_token):
                break
            app_token_errors = {CONF_MUSIC_APP_TOKEN: "invalid_value"}
        collected[CONF_MUSIC_APP_TOKEN] = app_token

    errors = None
    while True:
        user_values = await session.form(
            [
                CONF_ENTRY_UNOFFICIAL_PROVIDER,
                ConfigEntry(
                    key=CONF_MUSIC_USER_MANUAL_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    required=False,
                    advanced=True,
                    help_link="https://www.music-assistant.io/music-providers/apple-music/",
                ),
            ],
            step_id="user",
            errors=errors,
        )
        manual_token = str(user_values.get(CONF_MUSIC_USER_MANUAL_TOKEN) or "").strip()
        attempt = dict(collected)
        if manual_token:
            attempt[CONF_MUSIC_USER_MANUAL_TOKEN] = manual_token
        else:
            params = await _musickit_authenticate(session, app_token)
            token = params.get("music-user-token")
            if not token:
                raise AbortFlow("auth_cancelled")
            attempt[CONF_MUSIC_USER_TOKEN] = token
            attempt[CONF_MUSIC_USER_TOKEN_TIMESTAMP] = int(
                params.get("music-user-token-timestamp", 0)
            )
        try:
            await session.finish(attempt)
            return
        except SetupFlowError as err:
            errors = {"base": err}


def _is_valid_gateway(value: ConfigValueType) -> bool:
    """Return whether a FairPlay gateway URL is usable."""
    if not isinstance(value, str):
        return False
    return value.startswith(("http://", "https://")) and len(value) > len("https://")
