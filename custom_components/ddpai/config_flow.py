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
    CountrySelector,
    CountrySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import DdpaiApiClient, DdpaiAuthError, DdpaiError, auth_error_key
from .const import (
    CONF_COUNTRY,
    CONF_ENABLE_LIVE_VIEW,
    CONF_ENABLE_MEDIA_BROWSER,
    CONF_PARKED_CAPTURE,
    DEFAULT_PARKED_CAPTURE,
    CONF_SCAN_INTERVAL,
    CONF_TUTK_LICENSE_KEY,
    CONF_RTSP_ALERT_MINUTES,
    CONF_RTSP_ALERTS,
    CONF_RTSP_ALTERNATE,
    CONF_RTSP_CAMERA,
    CONF_RTSP_DRIVING,
    CONF_RTSP_DRIVING_GRACE,
    CONF_RTSP_MODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    DEFAULT_RTSP_ALERT_MINUTES,
    DEFAULT_RTSP_ALERTS,
    DEFAULT_RTSP_ALTERNATE,
    DEFAULT_RTSP_CAMERA,
    DEFAULT_RTSP_DRIVING,
    DEFAULT_RTSP_DRIVING_GRACE,
    DEFAULT_RTSP_MODE,
    DEFAULT_RTSP_PORT,
    RTSP_ALERT_TYPES,
    RTSP_CAMERAS,
    RTSP_MODES,
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
                    vol.Required(CONF_COUNTRY, default=country): CountrySelector(
                        CountrySelectorConfig(no_sort=False)
                    ),
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
    """Options: the general settings, and RTSP for NVRs."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which group of settings to change."""
        return self.async_show_menu(step_id="init", menu_options=["general", "rtsp"])

    async def async_step_rtsp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """RTSP for NVRs: mode, lenses, triggers, port and login."""
        options = self.config_entry.options
        if user_input is not None:
            merged = {**options, **user_input}
            for key in (CONF_RTSP_PORT, CONF_RTSP_ALTERNATE, CONF_RTSP_ALERT_MINUTES, CONF_RTSP_DRIVING_GRACE):
                merged[key] = int(merged[key])
            # Optional fields left empty are absent; clear them too.
            for key in (CONF_RTSP_USERNAME, CONF_RTSP_PASSWORD):
                if not user_input.get(key):
                    merged.pop(key, None)
            return self.async_create_entry(data=merged)
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        rtsp = getattr(coordinator, "rtsp", None)
        urls = (
            "\n".join(
                f"`{rtsp.stream_url(uuid, lens)}`"
                for uuid in coordinator.data
                for lens in rtsp.lenses(uuid)
            )
            if rtsp is not None and coordinator.data
            else "-"
        )

        def box(minimum: int, maximum: int, unit: str | None = None) -> NumberSelector:
            config = NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX)
            if unit:  # the selector refuses a unit of None
                config["unit_of_measurement"] = unit
            return NumberSelector(config)

        return self.async_show_form(
            step_id="rtsp",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RTSP_MODE, default=options.get(CONF_RTSP_MODE, DEFAULT_RTSP_MODE)): SelectSelector(
                        SelectSelectorConfig(options=RTSP_MODES, translation_key="rtsp_mode")
                    ),
                    vol.Required(CONF_RTSP_CAMERA, default=options.get(CONF_RTSP_CAMERA, DEFAULT_RTSP_CAMERA)): SelectSelector(
                        SelectSelectorConfig(options=RTSP_CAMERAS, translation_key="rtsp_camera")
                    ),
                    vol.Required(
                        CONF_RTSP_ALTERNATE, default=options.get(CONF_RTSP_ALTERNATE, DEFAULT_RTSP_ALTERNATE)
                    ): box(5, 600, "s"),
                    vol.Required(CONF_RTSP_ALERTS, default=options.get(CONF_RTSP_ALERTS, DEFAULT_RTSP_ALERTS)): SelectSelector(
                        SelectSelectorConfig(options=RTSP_ALERT_TYPES, multiple=True, translation_key="rtsp_alerts")
                    ),
                    vol.Required(
                        CONF_RTSP_ALERT_MINUTES, default=options.get(CONF_RTSP_ALERT_MINUTES, DEFAULT_RTSP_ALERT_MINUTES)
                    ): box(1, 120, "min"),
                    vol.Required(CONF_RTSP_DRIVING, default=options.get(CONF_RTSP_DRIVING, DEFAULT_RTSP_DRIVING)): BooleanSelector(),
                    vol.Required(
                        CONF_RTSP_DRIVING_GRACE, default=options.get(CONF_RTSP_DRIVING_GRACE, DEFAULT_RTSP_DRIVING_GRACE)
                    ): box(0, 60, "min"),
                    vol.Required(CONF_RTSP_PORT, default=options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): box(1024, 65535),
                    vol.Optional(
                        CONF_RTSP_USERNAME, description={"suggested_value": options.get(CONF_RTSP_USERNAME, "")}
                    ): TextSelector(),
                    vol.Optional(
                        CONF_RTSP_PASSWORD, description={"suggested_value": options.get(CONF_RTSP_PASSWORD, "")}
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
            description_placeholders={"urls": urls},
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Polling, media browser and live view."""
        if user_input is not None:
            user_input[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            key = "".join(user_input.get(CONF_TUTK_LICENSE_KEY, "").split())
            # A blank field keeps the stored key (the form shows it masked).
            user_input[CONF_TUTK_LICENSE_KEY] = key or self.config_entry.options.get(CONF_TUTK_LICENSE_KEY, "")
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        options = self.config_entry.options
        return self.async_show_form(
            step_id="general",
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
                    vol.Required(
                        CONF_ENABLE_LIVE_VIEW,
                        default=options.get(CONF_ENABLE_LIVE_VIEW, True),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_PARKED_CAPTURE,
                        default=options.get(CONF_PARKED_CAPTURE, DEFAULT_PARKED_CAPTURE),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_TUTK_LICENSE_KEY,
                        description={"suggested_value": options.get(CONF_TUTK_LICENSE_KEY, "")},
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
        )
