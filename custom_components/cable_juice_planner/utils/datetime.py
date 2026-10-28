"""Cable Juice Planner trip times (replace the input_datetime helpers with date and time).

The settings hold naive local datetimes (None = not set, the old 1900-01-01 00:00).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from ..const import DATETIME_SETTINGS
from ..coordinator import CJPCoordinator
from ..entity import CJPUpdatingEntity


def _parse_local(value) -> dt.datetime | None:
    """State (ISO with timezone) or legacy input_datetime state -> naive local datetime."""
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = dt_util.as_local(parsed).replace(tzinfo=None)
    return None if parsed.year <= 1900 else parsed


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    async_add_entities(CJPDateTime(coordinator, key, info) for key, info in DATETIME_SETTINGS.items())


class CJPDateTime(CJPUpdatingEntity, DateTimeEntity, RestoreEntity):
    """A date and time stored in the coordinator."""

    def __init__(self, coordinator: CJPCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "datetime", key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = None
        if (last := await self.async_get_restored_state()) is not None and last.state not in ("unknown", "unavailable"):
            value = _parse_local(last.state)
        elif (legacy := self.legacy_state()) is not None:
            value = _parse_local(legacy.state)
        self.coordinator.async_set_setting(self._key, value, source="initial")

    @property
    def native_value(self) -> dt.datetime | None:
        value = self.coordinator.settings.get(self._key)
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=dt_util.get_default_time_zone())
        return None

    async def async_set_value(self, value: dt.datetime) -> None:
        self.coordinator.async_set_setting(self._key, dt_util.as_local(value).replace(tzinfo=None, microsecond=0))
        self.async_write_ha_state()
