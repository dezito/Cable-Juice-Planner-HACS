"""Cable Juice Planner binary sensor: a public charging session waits for its cost."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import is_ev_configured
from .coordinator import CJPCoordinator
from .entity import CJPUpdatingEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    if is_ev_configured(coordinator.config):
        async_add_entities([CJPPublicChargingSessionDone(coordinator, "binary_sensor", "public_charging_session_done")])


class CJPPublicChargingSessionDone(CJPUpdatingEntity, BinarySensorEntity):
    """On when a public charging session ended and its cost must be entered."""

    _unrecorded_attributes = frozenset({MATCH_ALL})

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self._key)
        return None if value is None else value == "on"
