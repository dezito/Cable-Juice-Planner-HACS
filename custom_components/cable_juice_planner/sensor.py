"""Cable Juice Planner output sensors (incl. the action sensors for the user's automations)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import MATCH_ALL, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHARGER_ACTION_STATES, DOMAIN, MANUFACTURER, SENSOR_DEFS, SIGNAL_SITE_UPDATE, SITE_SENSORS, SITE_TITLE
from .coordinator import CJPCoordinator
from .entity import CJPUpdatingEntity, requirement_met
from .site import CJPSite

MAX_STATE_LENGTH = 255
# markdown / plan attributes are large; keep them out of the recorder database
UNRECORDED = ("current_charging_rule", "overview", "charging_history", "emoji_description", "drive_efficiency", "km_per_kwh", "kwh_cost_price")


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    if isinstance(entry.runtime_data, CJPSite):
        async_add_entities(CJPSiteSensor(entry.runtime_data, key) for key in SITE_SENSORS)
        return
    coordinator: CJPCoordinator = entry.runtime_data
    async_add_entities(
        (CJPUnrecordedSensor if key in UNRECORDED else CJPActionSensor if key == "charger_action" else CJPSensor)(coordinator, key, *definition)
        for key, definition in SENSOR_DEFS.items()
        if requirement_met(coordinator, definition[3])
    )


class CJPSensor(CJPUpdatingEntity, SensorEntity):
    """State and attributes published by the worker."""

    def __init__(self, coordinator: CJPCoordinator, key: str, unit, device_class, state_class, _requires) -> None:
        super().__init__(coordinator, "sensor", key)
        if unit:
            self._attr_native_unit_of_measurement = unit
        if device_class:
            self._attr_device_class = SensorDeviceClass(device_class)
        if state_class:
            self._attr_state_class = SensorStateClass(state_class)
        if key == "charger_action":
            self._attr_options = list(CHARGER_ACTION_STATES)
        if key in ("co2_emitted", "kwh_charged_by_solar"):
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self):
        value = self.coordinator.data.get(self._key)
        if self._key == "charger_action" and value not in CHARGER_ACTION_STATES:
            return None
        if isinstance(value, str) and len(value) > MAX_STATE_LENGTH:
            return value[: MAX_STATE_LENGTH - 1] + "…"
        if self.state_class and isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return value


class CJPActionSensor(CJPSensor):
    """charger_action: the heartbeat attribute changes every calculation, keep it out of the recorder."""

    _unrecorded_attributes = frozenset({"updated", "description", "help"})


class CJPUnrecordedSensor(CJPSensor):
    _unrecorded_attributes = frozenset({MATCH_ALL})


class CJPSiteSensor(SensorEntity):
    """Current left per phase for more charging / house current per phase (site entry)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 1
    _unrecorded_attributes = frozenset({"cars"})

    def __init__(self, site: CJPSite, key: str) -> None:
        self.site = site
        self._key = key
        self._phase = int(key[-1]) - 1
        self._attr_translation_key = key
        self._attr_unique_id = f"{site.entry.entry_id}_{key}"
        self.entity_id = f"sensor.cjp_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site.entry.entry_id)}, name=SITE_TITLE, manufacturer=MANUFACTURER, model=SITE_TITLE, entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_SITE_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        if self._key.startswith("site_house"):
            return round(self.site.house_now[self._phase], 1)
        fuse = self.site.fuse()
        used = sum(request[self._phase] for request in self.site.requests.values())
        return round(max(fuse[self._phase] - self.site.house_now[self._phase] - used, 0.0), 1)

    @property
    def extra_state_attributes(self):
        if self._key != "site_available_l1":
            return None
        cars = []
        for position, entry_id in enumerate(self.site.order(), start=1):
            coordinator = self.site.members[entry_id]
            cars.append({
                "position": position,
                "prefix": coordinator.prefix,
                "priority": coordinator.site_priority,
                "urgent": self.site.urgent.get(entry_id, False),
                "phase_order": coordinator.site_phase_order,
                "request": [round(a, 1) for a in self.site.requests.get(entry_id, [0.0, 0.0, 0.0])],
                "available": [round(a, 1) for a in self.site.available_now.get(entry_id, [0.0, 0.0, 0.0])],
                "reserved_hours": len(self.site.reservations.get(entry_id, {})),
            })
        return {"cars": cars, "fuse": self.site.fuse(), "phase_sensors": self.site.has_phase_sensors}
