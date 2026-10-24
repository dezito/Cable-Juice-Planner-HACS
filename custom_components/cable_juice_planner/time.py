"""Cable Juice Planner departure/homecoming times (replace the time-only input_datetime helpers)."""
from __future__ import annotations

import datetime
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import TIME_SETTINGS
from .coordinator import CJPCoordinator
from .entity import CJPUpdatingEntity


def _parse(value) -> datetime.time | None:
    try:
        return datetime.time.fromisoformat(str(value))
    except ValueError:
        return None


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    async_add_entities(CJPTime(coordinator, key, info) for key, info in TIME_SETTINGS.items())


class CJPTime(CJPUpdatingEntity, TimeEntity, RestoreEntity):
    """A time of day stored in the coordinator."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CJPCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "time", key)
        self._default = _parse(info["default"])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = None
        if (last := await self.async_get_restored_state()) is not None:
            value = _parse(last.state)
        if value is None and (legacy := self.legacy_state()) is not None:
            value = _parse(legacy.state)
        self.coordinator.async_set_setting(self._key, value or self._default, source="initial")

    @property
    def native_value(self) -> datetime.time | None:
        return self.coordinator.settings.get(self._key, self._default)

    async def async_set_value(self, value: datetime.time) -> None:
        self.coordinator.async_set_setting(self._key, value.replace(microsecond=0))
        self.async_write_ha_state()
