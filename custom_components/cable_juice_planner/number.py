"""Cable Juice Planner number settings (replace the input_number helpers)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import NUMBER_SETTINGS
from .coordinator import CJPCoordinator
from .entity import CJPUpdatingEntity, requirement_met
from .utils.helpers import float_or_none, get_dict_value_with_path

# values the user changes while using the car (trip, virtual battery), the rest are configuration
PRIMARY_NUMBERS = ("battery_level", "trip_charge_procent", "trip_range_needed", "public_charging_session_cost")


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    async_add_entities(
        CJPNumber(coordinator, key, info) for key, info in NUMBER_SETTINGS.items() if requirement_met(coordinator, info.get("requires"))
    )


class CJPNumber(CJPUpdatingEntity, NumberEntity, RestoreEntity):
    """A numeric setting stored in the coordinator."""

    def __init__(self, coordinator: CJPCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "number", key)
        # {distance} / {distance_kwh} in the entity name (translations/<language>.json)
        self._attr_translation_placeholders = {"distance": coordinator.distance_unit, "distance_kwh": f"{coordinator.distance_unit}/kWh"}
        self._attr_native_min_value = info["min"]
        self._max = info["max"]
        self._attr_native_step = info["step"]
        self._attr_mode = NumberMode.BOX if info["mode"] == "box" else NumberMode.SLIDER
        if info.get("unit"):
            # {valuta} / {valuta_kwh}: the currency of Home Assistant, {distance}: its length unit
            self._attr_native_unit_of_measurement = info["unit"].format(
                valuta=coordinator.t("ui.common.valuta"),
                valuta_kwh=coordinator.t("ui.common.valuta_kwh"),
                distance=coordinator.distance_unit,
            )
        if key not in PRIMARY_NUMBERS:
            self._attr_entity_category = EntityCategory.CONFIG
        default = info["default"]
        if info.get("config"):
            default = get_dict_value_with_path(coordinator.config, info["config"], default)
        self._default = float_or_none(self.initial_setting(default))
        if self._default is None:
            self._default = float(info["default"])

    @property
    def native_max_value(self) -> float:
        # trip_range_needed follows the estimated total range (set_estimated_range)
        return self.coordinator.setting_limits.get(self._key, {}).get("max", self._max)

    def _valid(self, value) -> float | None:
        value = float_or_none(value)
        if value is None:
            return None
        return min(max(value, self.native_min_value), max(self.native_max_value, self._max))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = None
        if (last := await self.async_get_restored_state()) is not None:
            value = self._valid(last.state)
        if value is None and (legacy := self.legacy_state()) is not None:
            value = self._valid(legacy.state)
        if value is None:
            value = self._default
        self.coordinator.async_set_setting(self._key, value, source="initial")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.settings.get(self._key, self._default)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_setting(self._key, float(value))
        self.async_write_ha_state()
