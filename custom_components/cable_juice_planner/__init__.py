"""Cable Juice Planner - EV charging planner for Home Assistant."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ENTITY_PREFIX, CONF_SITE, DOMAIN, PLATFORMS, SERVICE_ADD_PUBLIC_CHARGING, SERVICE_DEBUG_INFO, SERVICE_ENFORCE_PLANNING
from .coordinator import CJPCoordinator
from .const import normalize_prefix
from .blueprint_install import async_install_blueprints
from .dashboard import async_remove_dashboard_file, async_setup_dashboard
from .site import CJPSite

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CJPConfigEntry = ConfigEntry[CJPCoordinator]
SITE_PLATFORMS = ["sensor"]

SERVICE_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})
PUBLIC_CHARGING_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Required("charging_session_start"): cv.datetime,
        vol.Optional("battery_level_begin"): vol.Coerce(float),
        vol.Optional("battery_level_end"): vol.Coerce(float),
        vol.Optional("battery_level_added"): vol.Coerce(float),
        vol.Optional("battery_kwh_added"): vol.Coerce(float),
        vol.Required("total_charging_cost"): vol.Coerce(float),
        vol.Optional("include_charging_loss", default=True): cv.boolean,
        vol.Optional("add_to_history", default=False): cv.boolean,
    }
)


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[CJPCoordinator]:
    entry_id = call.data.get("config_entry_id")
    entries = [entry for entry in hass.config_entries.async_loaded_entries(DOMAIN) if isinstance(entry.runtime_data, CJPCoordinator)]
    if entry_id:
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(f"Cable Juice Planner config entry {entry_id} is not loaded")
    return [entry.runtime_data for entry in entries]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Cable Juice Planner services."""

    async def _debug_info(call: ServiceCall) -> ServiceResponse:
        output = {}
        for coordinator in _coordinators(hass, call):
            output[coordinator.object_prefix] = await coordinator.async_debug_info()
        return {"debug_info": output} if call.return_response else None

    async def _enforce_planning(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            coordinator.async_enforce_planning()

    async def _add_public_charging(call: ServiceCall) -> ServiceResponse:
        coordinators = _coordinators(hass, call)
        if len(coordinators) != 1:
            raise ServiceValidationError("Choose the Cable Juice Planner instance (config_entry_id)")
        data = {key: value for key, value in call.data.items() if key != "config_entry_id"}
        return await coordinators[0].async_add_public_charging_session(**data)

    hass.services.async_register(DOMAIN, SERVICE_DEBUG_INFO, _debug_info, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_ENFORCE_PLANNING, _enforce_planning, schema=SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_PUBLIC_CHARGING, _add_public_charging, schema=PUBLIC_CHARGING_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    await async_install_blueprints(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CJPConfigEntry) -> bool:
    if entry.data.get(CONF_SITE):
        return await _async_setup_site(hass, entry)
    coordinator = CJPCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    if coordinator.config.get("dashboard"):
        await async_setup_dashboard(hass, entry, coordinator.config, coordinator.prefix)
    coordinator.async_start()
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Version 2: the stored prefix no longer contains the fixed cjp_ (cjp_tesla -> tesla)."""
    if entry.version == 1:
        data = dict(entry.data)
        if CONF_ENTITY_PREFIX in data:
            data[CONF_ENTITY_PREFIX] = normalize_prefix(data[CONF_ENTITY_PREFIX])
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated Cable Juice Planner entry %s to version 2 (prefix %r)", entry.title, data.get(CONF_ENTITY_PREFIX))
    return True


async def _async_setup_site(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """The shared grid connection: load balancing between the car instances."""
    site = CJPSite(hass, entry)
    entry.runtime_data = site
    hass.data.setdefault(DOMAIN, {})["site"] = site
    for other in hass.config_entries.async_loaded_entries(DOMAIN):
        if isinstance(other.runtime_data, CJPCoordinator):
            site.register(other.runtime_data)
    await hass.config_entries.async_forward_entry_setups(entry, SITE_PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    await site.async_start()
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CJPConfigEntry) -> bool:
    if entry.data.get(CONF_SITE):
        unloaded = await hass.config_entries.async_unload_platforms(entry, SITE_PLATFORMS)
        if unloaded:
            hass.data.get(DOMAIN, {}).pop("site", None)
            await entry.runtime_data.async_stop()
        return unloaded
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: CJPConfigEntry) -> None:
    if entry.data.get(CONF_SITE):
        return
    from .storage import CJPStorage  # noqa: PLC0415

    await CJPStorage(hass, entry.entry_id).async_remove()
    await async_remove_dashboard_file(hass, entry.entry_id)


async def _async_update_listener(hass: HomeAssistant, entry: CJPConfigEntry) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    if coordinator.ignore_next_update:
        # the learned charging loss was written to the options, no reload needed
        coordinator.ignore_next_update = False
        return
    hass.config_entries.async_schedule_reload(entry.entry_id)
