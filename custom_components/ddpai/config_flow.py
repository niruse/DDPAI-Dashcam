"""Config flow for the DDPAI Dashcam integration.

Sign-in uses the same login (email or phone) and password as the DDPAI app.
The password is kept in the entry because the cloud session lapses after 30
minutes without use and has to be renewed by signing in again.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import DdpaiApiClient, DdpaiAuthError, DdpaiError, auth_error_key
from .const import (
    CONF_COUNTRY,
    CONF_ENABLE_MEDIA_BROWSER,
    CONF_SCAN_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import is_cloud_camera

_LOGGER = logging.getLogger(__name__)


class DdpaiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a DDPAI config flow."""

    VERSION = 1

    async def _async_try_login(
        self, username: str, password: str, country: str
    ) -> tuple[int | None, list[dict[str, Any]], dict[str, str]]:
        client = DdpaiApiClient(
            async_get_clientsession(self.hass), username, password, country
        )
        try:
            user_id = await client.async_login()
            devices = await client.async_get_devices()
        except DdpaiAuthError as err:
            return None, [], {"base": auth_error_key(err)}
        except DdpaiError as err:
            _LOGGER.warning("DDPAI sign-in check failed: %s", err)
            return None, [], {"base": "cannot_connect"}
        return user_id, devices, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the DDPAI app login."""
        errors: dict[str, str] = {}
        country = (self.hass.config.country or DEFAULT_COUNTRY).upper()
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            country = user_input[CONF_COUNTRY].strip().upper() or DEFAULT_COUNTRY
            user_id, devices, errors = await self._async_try_login(
                username, user_input[CONF_PASSWORD], country
            )
            if not errors:
                await self.async_set_unique_id(str(user_id))
                self._abort_if_unique_id_configured()
                if not any(is_cloud_camera(d) for d in devices):
                    return self.async_abort(reason="no_4g_camera")
                return self.async_create_entry(
                    title=username,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_COUNTRY: country,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=(user_input or {}).get(CONF_USERNAME, ""),
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    ),
                    vol.Required(CONF_COUNTRY, default=country): TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The stored password stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the current password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_id, _devices, errors = await self._async_try_login(
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
            )
            if not errors:
                if str(user_id) != entry.unique_id:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    )
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options: poll interval and media browser."""
        return DdpaiOptionsFlow()


class DdpaiOptionsFlow(OptionsFlow):
    """Handle DDPAI options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=3600,
                            step=10,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_ENABLE_MEDIA_BROWSER,
                        default=options.get(CONF_ENABLE_MEDIA_BROWSER, True),
                    ): BooleanSelector(),
                }
            ),
        )
