"""Cable Juice Planner setting switches (replace the input_boolean helpers)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import SWITCH_SETTINGS
from .coordinator import CJPCoordinator
from .entity import CJPUpdatingEntity, requirement_met

# shown with the charging modes on the dashboard, the rest are configuration
PRIMARY_SWITCHES = ("deactivate_script", "allow_manual_charging_now", "allow_manual_charging_solar", "solar_charging", "fill_up", "workplan_charging")


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    async_add_entities(
        CJPSwitch(coordinator, key, info) for key, info in SWITCH_SETTINGS.items() if requirement_met(coordinator, info.get("requires"))
    )


class CJPSwitch(CJPUpdatingEntity, SwitchEntity, RestoreEntity):
    """A setting stored in the coordinator."""

    def __init__(self, coordinator: CJPCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "switch", key)
        self._default = bool(self.initial_setting(info["default"]))
        if key not in PRIMARY_SWITCHES:
            self._attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = self._default
        if (last := await self.async_get_last_state()) is not None and last.state in ("on", "off"):
            value = last.state == STATE_ON
        elif (legacy := self.legacy_state()) is not None and legacy.state in ("on", "off"):
            value = legacy.state == STATE_ON
        self.coordinator.async_set_setting(self._key, value, source="initial")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.settings.get(self._key, self._default))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_set_setting(self._key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_set_setting(self._key, False)
        self.async_write_ha_state()
