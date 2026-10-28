"""Cable Juice Planner - EV charging planner for Home Assistant."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import async_get as async_get_restore_data
from homeassistant.helpers.typing import ConfigType

from .management.backup import BACKUP_DIR, async_export, async_import
from .const import (
    CONF_ENTITY_PREFIX,
    CONF_SITE,
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_PUBLIC_CHARGING,
    SERVICE_DEBUG_INFO,
    SERVICE_ENFORCE_PLANNING,
    SERVICE_EXPORT_DEBUG,
    SERVICE_EXPORT_DATA,
    SERVICE_IMPORT_DATA,
)
from .coordinator import CJPCoordinator
from .const import MIGRATED_LAST_FULL_CHARGE, normalize_prefix
from .management.blueprint_install import async_install_blueprints
from .management.dashboard_build import async_remove_dashboard_file, async_setup_dashboard
from .management.debug_export import DEBUG_DIR, DebugDownloadView, async_export_debug, async_notify_debug, install_log_buffer
from .utils.helpers import float_or_none
from .site import CJPSite

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CJPConfigEntry = ConfigEntry[CJPCoordinator]
SITE_PLATFORMS = ["sensor"]
REMOVED_ENTITIES = (("number", "solar_sell_fixed_price"), ("switch", "deactivate_script"), ("button", "restart_script"))

SERVICE_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("file"): cv.string,
        vol.Optional("restore_configuration", default=False): cv.boolean,
    }
)
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

    async def _export_debug(call: ServiceCall) -> ServiceResponse:
        files = {}
        for coordinator in _coordinators(hass, call):
            name = await async_export_debug(hass, coordinator)
            await async_notify_debug(hass, coordinator, name)
            files[coordinator.object_prefix] = f"{DEBUG_DIR}/{name}"
        return {"files": files} if call.return_response else None

    async def _enforce_planning(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            coordinator.async_enforce_planning()

    async def _add_public_charging(call: ServiceCall) -> ServiceResponse:
        coordinators = _coordinators(hass, call)
        if len(coordinators) != 1:
            raise ServiceValidationError("Choose the Cable Juice Planner instance (config_entry_id)")
        data = {key: value for key, value in call.data.items() if key != "config_entry_id"}
        return await coordinators[0].async_add_public_charging_session(**data)

    async def _export_data(call: ServiceCall) -> ServiceResponse:
        files = {}
        for coordinator in _coordinators(hass, call):
            files[coordinator.object_prefix] = f"{BACKUP_DIR}/{await async_export(hass, coordinator.entry)}"
        return {"files": files} if call.return_response else None

    async def _import_data(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.data.get(CONF_SITE):
            raise ServiceValidationError(f"{call.data['config_entry_id']} is not a Cable Juice Planner instance")
        try:
            await async_import(hass, entry, call.data["file"].split("/")[-1], call.data["restore_configuration"])
        except HomeAssistantError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(DOMAIN, SERVICE_EXPORT_DATA, _export_data, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_IMPORT_DATA, _import_data, schema=IMPORT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DEBUG_INFO, _debug_info, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_EXPORT_DEBUG, _export_debug, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    install_log_buffer()
    if getattr(hass, "http", None) is not None:
        hass.http.register_view(DebugDownloadView())
    hass.services.async_register(DOMAIN, SERVICE_ENFORCE_PLANNING, _enforce_planning, schema=SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_PUBLIC_CHARGING, _add_public_charging, schema=PUBLIC_CHARGING_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    await async_install_blueprints(hass)
    return True


async def _async_run_after_home_assistant_started(
    hass: HomeAssistant,
    entry: ConfigEntry,
    action: Callable[[], Coroutine[Any, Any, None]],
    name: str,
) -> None:
    """Run post-setup work after every integration has had its startup turn."""
    if hass.state is CoreState.running:
        await action()
        return

    async def _async_run() -> None:
        # Let all other homeassistant_started listeners run before inspecting
        # their entities. This keeps generated dashboards independent of setup order.
        await asyncio.sleep(0)
        await action()

    @callback
    def _started(_event: Event) -> None:
        entry.async_create_task(hass, _async_run(), name=name)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started))


async def async_setup_entry(hass: HomeAssistant, entry: CJPConfigEntry) -> bool:
    if entry.data.get(CONF_SITE):
        return await _async_setup_site(hass, entry)
    coordinator = CJPCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    if coordinator.config.get("dashboard"):
        await _async_run_after_home_assistant_started(
            hass,
            entry,
            lambda: async_setup_dashboard(hass, entry, coordinator.config, coordinator.prefix),
            f"cjp_{coordinator.prefix}_dashboard_startup",
        )
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
    if entry.version == 2 and entry.minor_version < 2:
        _async_migrate_removed_entities(hass, entry)
    if entry.version == 2 and entry.minor_version < 3:
        _async_migrate_last_full_charge(hass, entry)
    return True


@callback
def _async_migrate_last_full_charge(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Version 2.3: last_full_charge is a sensor set by the integration, no longer a datetime entity."""
    data = dict(entry.data)
    ent_reg = er.async_get(hass)
    if entity_id := ent_reg.async_get_entity_id("datetime", DOMAIN, f"{entry.entry_id}_last_full_charge"):
        stored = async_get_restore_data(hass).last_states.get(entity_id)
        if stored and stored.state.state not in ("unknown", "unavailable"):
            data[MIGRATED_LAST_FULL_CHARGE] = stored.state.state
        ent_reg.async_remove(entity_id)
    hass.config_entries.async_update_entry(entry, data=data, minor_version=3)
    _LOGGER.info("Migrated Cable Juice Planner entry %s to version 2.3 (last full charge %s)", entry.title, data.get(MIGRATED_LAST_FULL_CHARGE))


@callback
def _async_migrate_removed_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Version 2.2: solar_sell_fixed_price moved from a number entity into the options,
    deactivate_script and restart_script are gone."""
    options = dict(entry.options)
    ent_reg = er.async_get(hass)
    for platform, key in REMOVED_ENTITIES:
        entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        if entity_id is None:
            continue
        if key == "solar_sell_fixed_price" and (stored := async_get_restore_data(hass).last_states.get(entity_id)):
            if (value := float_or_none(stored.state.state)) is not None:
                options[key] = value
        ent_reg.async_remove(entity_id)
    hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    _LOGGER.info("Migrated Cable Juice Planner entry %s to version 2.2 (solar sell price %s)", entry.title, options.get("solar_sell_fixed_price"))


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
    await _async_run_after_home_assistant_started(hass, entry, site.async_start, "cjp_site_startup")
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
    from .management.storage import CJPStorage  # noqa: PLC0415

    # the data can be imported again (config flow of a new instance, or import_data)
    try:
        await async_export(hass, entry, "deleted")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not back up %s, its databases are kept in .storage", entry.title)
        return
    await CJPStorage(hass, entry.entry_id).async_remove()
    await async_remove_dashboard_file(hass, entry.entry_id)


async def _async_update_listener(hass: HomeAssistant, entry: CJPConfigEntry) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    if coordinator.ignore_next_update:
        # the learned charging loss was written to the options, no reload needed
        coordinator.ignore_next_update = False
        return
    hass.config_entries.async_schedule_reload(entry.entry_id)
