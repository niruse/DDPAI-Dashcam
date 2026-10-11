"""Base entity for the DDPAI Dashcam integration."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import DdpaiCoordinator

# Marketing names for the model codes the cloud reports.
MODEL_NAMES = {
    "DR2008": "Z60 Pro",
    "DR1003": "MINI2X",
}


def model_name(info: dict[str, Any]) -> str | None:
    """Human model name, e.g. ``DDPAI DR2008_Overseas`` → ``Z60 Pro (DR2008)``."""
    raw = info.get("model") or info.get("productName")
    if not raw:
        return None
    code = raw.replace("DDPAI", "").strip().split("_")[0]
    name = MODEL_NAMES.get(code)
    return f"{name} ({code})" if name else raw


class DdpaiEntity(CoordinatorEntity[DdpaiCoordinator]):
    """Base entity tied to one DDPAI camera."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: DdpaiCoordinator, uuid: str) -> None:
        super().__init__(coordinator)
        self._uuid = uuid
        info = (self.camera or {}).get("info") or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uuid)},
            name=info.get("deviceName") or info.get("name") or uuid,
            manufacturer="DDPAI",
            model=model_name(info),
            sw_version=info.get("version"),
            connections={("mac", info["macAddr"])} if info.get("macAddr") else set(),
        )

    @property
    def camera(self) -> dict[str, Any] | None:
        """The camera's slice of the coordinator data."""
        return (self.coordinator.data or {}).get(self._uuid)

    @property
    def status(self) -> dict[str, Any]:
        """The camera's last 4G status reply."""
        return (self.camera or {}).get("status") or {}

    @property
    def info(self) -> dict[str, Any]:
        """The camera's record from the account device list."""
        return (self.camera or {}).get("info") or {}

    @property
    def available(self) -> bool:
        """Available while the camera is still bound to the account."""
        return super().available and self.camera is not None
