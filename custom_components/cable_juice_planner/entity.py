"""Base entity for Cable Juice Planner."""
from __future__ import annotations

from typing import Any

from homeassistant.core import State, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, ENTITY_HELP_GROUPS, MANUFACTURER, TITLE, device_name, is_ev_configured, is_powerwall_configured, is_solar_configured
from .coordinator import CJPCoordinator

# Platform -> helper domain used by the PyScript version (values are migrated once)
LEGACY_DOMAINS = {
    "switch": "input_boolean",
    "number": "input_number",
    "select": "input_select",
    "time": "input_datetime",
    "datetime": "input_datetime",
}


def requirement_met(coordinator: CJPCoordinator, requires: str | None) -> bool:
    """Entities only created for some configurations (like init() pruned DEFAULT_ENTITIES)."""
    config = coordinator.config
    if requires == "ev":
        return is_ev_configured(config)
    if requires == "no_ev":
        return not is_ev_configured(config)
    if requires == "solar":
        return is_solar_configured(config)
    if requires == "powerwall_level":
        return is_powerwall_configured(config) or bool(config["home"]["entity_ids"]["powerwall_battery_level_entity_id"])
    return True


class _HelpValues(dict):
    def __missing__(self, key):
        return "?"


def help_values(coordinator: CJPCoordinator) -> _HelpValues:
    """Current settings and sensor states for the {key} placeholders of the help texts."""
    values = _HelpValues()
    for key, value in {**coordinator.data, **coordinator.settings}.items():
        if isinstance(value, bool):
            value = "on" if value else "off"
        elif isinstance(value, float):
            value = int(value) if value.is_integer() else round(value, 2)
        values[key] = value
    return values


class CJPEntity(Entity):
    """Common Cable Juice Planner entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # static texts, no need to store them with every state
    _unrecorded_attributes = frozenset({"description", "help"})

    def __init__(self, coordinator: CJPCoordinator, platform: str, key: str) -> None:
        self.coordinator = coordinator
        self._key = key
        self._platform_domain = platform
        self.entity_id = f"{platform}.{coordinator.object_prefix}_{key}"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        # name from translations/<language>.json and icon from icons.json (entity.<platform>.<key>)
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name(coordinator.prefix, coordinator.car_name),
            manufacturer=MANUFACTURER,
            model=TITLE,
            entry_type=DeviceEntryType.SERVICE,
        )

    def legacy_state(self) -> State | None:
        """State of the matching PyScript helper (input_boolean.ev_x, ...)."""
        domain = LEGACY_DOMAINS.get(self._platform_domain)
        legacy_prefix = self.coordinator.legacy_prefix
        if domain is None or legacy_prefix is None:
            return None
        state = self.hass.states.get(f"{domain}.{legacy_prefix}_{self._key}")
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state

    def initial_setting(self, default: Any) -> Any:
        """Value from the imported PyScript config file (or default)."""
        return self.coordinator.entry.data.get("initial_settings", {}).get(self._key, default)

    def help_attributes(self) -> dict:
        """description (short, per entity) and help (the dashboard settings text of the group)."""
        texts = self.coordinator.texts
        result = {}
        if description := texts.get(f"entity_description.{self._key}"):
            result["description"] = description
        group = ENTITY_HELP_GROUPS.get(self._key)
        if group and (text := texts.get(f"entity_help.{group}")):
            try:
                result["help"] = text.format_map(help_values(self.coordinator))
            except (ValueError, IndexError):
                result["help"] = text
        return result

    @property
    def extra_state_attributes(self):
        attributes = dict(self.coordinator.attributes.get(self._key) or {})
        attributes.update(self.help_attributes())
        return attributes or None


class CJPUpdatingEntity(CJPEntity):
    """Entity that writes its state when the coordinator publishes its key."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self.coordinator.signal, self._handle_update))

    @callback
    def _handle_update(self, key: Any) -> None:
        if key == self._key:
            self.async_write_ha_state()
