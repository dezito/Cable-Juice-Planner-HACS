"""Cable Juice Planner buttons (replace the input_button helpers)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BUTTON_KEYS
from .coordinator import CJPCoordinator
from .entity import CJPEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    async_add_entities(CJPButton(coordinator, "button", key) for key in BUTTON_KEYS)


class CJPButton(CJPEntity, ButtonEntity):
    """enforce_planning (plan now, ignoring the unchanged-input shortcut), trip_reset, restart_script."""

    async def async_press(self) -> None:
        self.coordinator.async_press(self._key)
