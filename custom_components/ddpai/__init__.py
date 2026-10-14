"""The DDPAI Dashcam integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import DdpaiApiClient
from .const import CONF_COUNTRY, DEFAULT_COUNTRY, DOMAIN, RUNTIME_OPTIONS
from .coordinator import DdpaiCoordinator
from .live import DdpaiLiveView, async_stop_all
from .rtsp import DdpaiRtsp
from .services import async_setup_services
from .views import DdpaiMediaView

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

DdpaiConfigEntry = ConfigEntry[DdpaiCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DdpaiConfigEntry) -> bool:
    """Set up DDPAI from a config entry."""
    client = DdpaiApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
    )
    coordinator = DdpaiCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    if not hass.data.get(f"{DOMAIN}_view"):
        hass.http.register_view(DdpaiMediaView(hass))
        hass.http.register_view(DdpaiLiveView(hass))
        hass.data[f"{DOMAIN}_view"] = True

    coordinator.rtsp = DdpaiRtsp(hass, entry, coordinator)
    coordinator.applied_options = dict(entry.options)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.rtsp.async_setup()
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: DdpaiConfigEntry) -> None:
    """Reload for option changes, except those applied on the fly (RTSP mode)."""
    coordinator = entry.runtime_data
    before = coordinator.applied_options
    changed = {key for key in set(before) | set(entry.options) if before.get(key) != entry.options.get(key)}
    coordinator.applied_options = dict(entry.options)
    if changed and not changed <= RUNTIME_OPTIONS:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DdpaiConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        if entry.runtime_data.rtsp is not None:
            await entry.runtime_data.rtsp.async_shutdown()
        await async_stop_all(entry.runtime_data)
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
