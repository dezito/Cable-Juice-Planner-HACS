"""Cable Juice Planner select settings (replace input_select.<prefix>_forecast_type)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import SELECT_SETTINGS
from .coordinator import CJPCoordinator
from .entity import CJPUpdatingEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    async_add_entities(CJPSelect(coordinator, key, info) for key, info in SELECT_SETTINGS.items())


class CJPSelect(CJPUpdatingEntity, SelectEntity, RestoreEntity):
    """A select setting stored in the coordinator."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CJPCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "select", key)
        self._attr_options = list(info["options"])
        self._default = info["default"]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = self._default
        if (last := await self.async_get_last_state()) is not None and last.state in self.options:
            value = last.state
        elif (legacy := self.legacy_state()) is not None and legacy.state in self.options:
            value = legacy.state
        self.coordinator.async_set_setting(self._key, value, source="initial")

    @property
    def current_option(self) -> str | None:
        return self.coordinator.settings.get(self._key, self._default)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.async_set_setting(self._key, option)
        self.async_write_ha_state()
