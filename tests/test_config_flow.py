"""Config flow (car/charger instance and shared grid connection) and options flows."""
from __future__ import annotations

import shutil
from pathlib import Path

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.cable_juice_planner.const import CONF_ENTITY_PREFIX, CONF_SITE, DOMAIN

from .conftest import (
    CHARGER_OPTIONS,
    CONSUMPTION,
    EV_LEVEL,
    EV_ODOMETER,
    EV_RANGE,
    PRICE,
    async_setup,
    form_defaults,
    make_entry,
    make_site_entry,
    set_source_states,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHARGER_ENTITIES = {key: value for key, value in CHARGER_OPTIONS.items() if key.endswith("_entity_id")}


async def _start_instance(hass: HomeAssistant, prefix: str = "flow"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["instance", "site"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "instance"})
    assert result["step_id"] == "instance"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: prefix, "language": "da"})


async def _configure(hass: HomeAssistant, result, step_id: str, user_input: dict):
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == step_id, result
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def _submit_defaults_until(hass: HomeAssistant, result, step_id: str | None):
    """Press submit on every page until step_id (None: to the end)."""
    while result["type"] is FlowResultType.FORM and result["step_id"] != step_id:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_defaults(result))
    return result


async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    # no site entry -> no load balancing page
    result = await _configure(
        hass, result, "ev",
        {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER},
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    result = await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION})
    result = await _configure(hass, result, "powerwall", form_defaults(result))
    result = await _configure(hass, result, "price_provider", {"price_providers": ["energidataservice"]})
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", {"refund": 0.0, "sell_fee": 0.03})
    # no production entity -> no solar charging / weather pages
    result = await _configure(hass, result, "solar", form_defaults(result))
    result = await _configure(hass, result, "notifications", form_defaults(result))
    result = await _configure(hass, result, "advanced", {**form_defaults(result), "dashboard": False})
    result = await _configure(hass, result, "database", form_defaults(result))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cable Juice Planner (flow)"
    assert result["data"][CONF_ENTITY_PREFIX] == "flow"
    options = result["options"]
    assert options["status_entity_id"] == CHARGER_OPTIONS["status_entity_id"]
    assert options["charging_loss"] == -0.05  # shown in %, stored as a fraction
    assert options["usable_battery_level_entity_id"] == EV_LEVEL
    assert options["dashboard"] is False


async def test_ev_entities_all_or_nothing(hass: HomeAssistant) -> None:
    result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", form_defaults(result))
    result = await _configure(hass, result, "ev", {"usable_battery_level_entity_id": EV_LEVEL})
    assert result["step_id"] == "ev"
    assert result["errors"] == {"base": "ev_incomplete"}
    # charger only (no car integration) is allowed
    result = await _configure(hass, result, "ev", {})
    assert result["step_id"] == "ev_specs"


async def test_solar_charging_min_above_max(hass: HomeAssistant) -> None:
    result = await _start_instance(hass)
    result = await _submit_defaults_until(hass, await _configure(hass, result, "charger", CHARGER_ENTITIES), "ev")
    result = await _configure(hass, result, "ev", {})
    result = await _submit_defaults_until(hass, result, "price_provider")
    result = await _configure(hass, result, "price_provider", {"price_providers": ["energidataservice"]})
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", form_defaults(result))
    result = await _configure(hass, result, "solar", {**form_defaults(result), "production_entity_id": "sensor.solar_production"})
    result = await _configure(
        hass, result, "solar_charging",
        {**form_defaults(result), "charging_single_phase_min_amp": 16, "charging_single_phase_max_amp": 6},
    )
    assert result["errors"] == {"charging_single_phase_min_amp": "min_above_max"}


async def test_reserved_and_duplicate_prefix(hass: HomeAssistant) -> None:
    result = await _start_instance(hass, prefix="site")
    assert result["errors"] == {CONF_ENTITY_PREFIX: "invalid_prefix"}

    make_entry("taken").add_to_hass(hass)
    result = await _start_instance(hass, prefix="taken")
    assert result["errors"] == {CONF_ENTITY_PREFIX: "prefix_in_use"}


async def test_old_prefix_with_cjp_is_normalized(hass: HomeAssistant) -> None:
    result = await _start_instance(hass, prefix="cjp_tesla")
    assert result["step_id"] == "charger"
    assert result["description_placeholders"]["prefix"] == "tesla"


async def test_prefix_taken_by_pyscript(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.cjp_flow_overview", "PyScript")
    result = await _start_instance(hass)
    assert result["errors"] == {CONF_ENTITY_PREFIX: "prefix_entities_exist"}


async def test_site_flow_once(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "site"})
    assert result["step_id"] == "site"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**form_defaults(result), "fuse_l1": 20, "urgent_trip_hours": 6})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SITE: True}
    assert result["options"]["fuse_l1"] == 20.0
    assert result["options"]["urgent_trip_hours"] == 6
    await hass.async_block_till_done()

    # with a site the menu is skipped and the car flow shows the load balancing page
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "instance"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: "second", "language": "da"})
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", form_defaults(result))
    assert result["step_id"] == "load_balancing"


async def test_import_legacy_pyscript_config(hass: HomeAssistant, tmp_path: Path) -> None:
    """A PyScript ev_config.yaml in the config dir is offered and its values prefill the pages."""
    shutil.copy(FIXTURES / "ev_config.yaml", tmp_path / "ev_config.yaml")
    hass.config.config_dir = str(tmp_path)
    result = await _start_instance(hass, prefix="ev")
    assert result["step_id"] == "import_legacy"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_source": "ev"})
    assert result["step_id"] == "charger"
    assert form_defaults(result)["status_entity_id"] == "sensor.ev_charger_status"

    result = await _submit_defaults_until(hass, result, None)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["legacy_prefix"] == "ev"
    assert result["data"]["initial_settings"]["min_daily_battery_level"] == 35.0
    options = result["options"]
    assert options["power_voltage"] == 240.0
    assert options["battery_size"] == 69.0
    assert options["charge_port_door_entity_id"] == "cover.my_car_charge_port_door"
    assert options["notify_list"] == ["notify.mobile_app_phone"]
    assert options["language"] == "da"


async def test_options_flow_changes_battery_size(hass: HomeAssistant, loaded_entry) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_ev"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["step_id"] == "ev_specs"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "battery_size": 82.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.options["battery_size"] == 82.0
    assert loaded_entry.runtime_data.config["ev_car"]["battery_size"] == 82.0  # reloaded


async def test_site_options_flow(hass: HomeAssistant) -> None:
    set_source_states(hass)
    site = make_site_entry()
    await async_setup(hass, site)
    result = await hass.config_entries.options.async_init(site.entry_id)
    assert result["step_id"] == "site"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "fuse_l2": 35})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert site.options["fuse_l2"] == 35.0
    await hass.config_entries.async_unload(site.entry_id)
