"""Cable Juice Planner event entity: requests for the car (wake up, preheating).

The PyScript version pressed the wake-up button and started/stopped the climate of
the car itself. The integration fires these events instead; the user's automations
send the commands.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EV_REQUEST_TYPES, is_ev_configured
from .coordinator import CJPCoordinator
from .entity import CJPEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    if is_ev_configured(coordinator.config):
        async_add_entities([CJPEvRequest(coordinator, "event", "ev_request")])


class CJPEvRequest(CJPEntity, EventEntity):
    """wake_up / preheat_start / preheat_stop"""

    _attr_event_types = list(EV_REQUEST_TYPES)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self.coordinator.signal, self._handle_update))

    @callback
    def _handle_update(self, key: Any) -> None:
        if isinstance(key, tuple) and key[0] == "event":
            self._trigger_event(key[1], key[2] or None)
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        return self.help_attributes() or None
