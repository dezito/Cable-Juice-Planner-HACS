"""Selectors, schema building and validation of the config flow pages."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
import re

import voluptuous as vol

from homeassistant.helpers import selector

from ..const import PHASE_ORDERS, SITE_DEFAULTS, SITE_FIELDS, CONFIG_KEYS_RENAMING, ENTITY_OPTION_KEYS, LANGUAGES, LIST_OPTION_KEYS, NUMBER_SETTINGS, OPTION_PATHS, currency_symbol, default_options, normalize_language
from ..utils.helpers import delete_dict_key_with_path, get_dict_value_with_path, set_dict_value_with_path


PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9_]{0,40})?$")  # optional, entity ids are cjp_<prefix>_*
RESERVED_PREFIXES = {"site"}  # sensor.cjp_site_* belong to the shared grid connection
IMPORT_NONE = "none"
BACKUP_EXPORT = "export"
CRON_INTERVALS = ["1", "2", "3", "4", "5", "6", "10", "12", "15", "20", "30"]
PRICE_PROVIDERS = ["energidataservice", "stromligning"]
ENERGIDATASERVICE_URL = "https://github.com/MTrab/energidataservice"  # {eds_url} in the step descriptions
EV_REQUIRED = ("usable_battery_level_entity_id", "estimated_battery_range_entity_id", "odometer_entity_id")
CONFIG_FLOW_STEPS = [
    "charger",
    "charger_specs",
    "load_balancing",
    "ev",
    "ev_specs",
    "home",
    "powerwall",
    "price_provider",
    "prices_energidataservice",
    "prices_stromligning",
    "prices_stromligning_tariffs",
    "price_fees",
    "solar",
    "solar_charging",
    "weather",
    "notifications",
    "advanced",
    "database",
]


def _entity(domain: str | list[str], *, device_class: str | list[str] | None = None, integration: str | None = None, multiple: bool = False) -> selector.EntitySelector:
    """Entity picker limited to the relevant entities (domain + device class or integration)."""
    entity_filter: dict[str, Any] = {"domain": domain}
    if device_class:
        entity_filter["device_class"] = device_class
    if integration:
        entity_filter["integration"] = integration
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=[entity_filter], multiple=multiple))


def _number(minimum: float, maximum: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config: dict[str, Any] = {"min": minimum, "max": maximum, "step": step, "mode": selector.NumberSelectorMode.BOX}
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))


BOOLEAN = selector.BooleanSelector()
SELECTORS: dict[str, Any] = {
    "language": selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=code, label=label) for code, label in LANGUAGES.items()],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    ),
    # charger
    "status_entity_id": _entity("sensor"),
    "power_consumtion_entity_id": _entity("sensor", device_class="power"),
    "kwh_meter_entity_id": _entity("sensor", device_class="energy"),
    "lifetime_kwh_meter_entity_id": _entity("sensor", device_class="energy"),
    "enabled_entity_id": _entity(["switch", "binary_sensor", "input_boolean"]),
    "cable_connected_entity_id": _entity(["binary_sensor", "sensor"]),
    "co2_entity_id": _entity("sensor", integration="energidataservice"),
    "power_voltage": _number(100, 480, 1, "V"),
    "charging_phases": _number(1, 3, 1),
    "charging_max_amp": _number(6, 80, 1, "A"),
    # shown in % (0 or a loss), stored as a fraction (-5 % -> -0.05) like ev.py
    "charging_loss": _number(-50, 0, 0.1, "%"),
    # load balancing (only shown when a site entry exists)
    "site_priority": _number(1, 10, 1),
    "site_phase_order": selector.SelectSelector(selector.SelectSelectorConfig(options=list(PHASE_ORDERS), mode=selector.SelectSelectorMode.DROPDOWN)),
    # car
    "usable_battery_level_entity_id": _entity("sensor", device_class="battery"),
    "estimated_battery_range_entity_id": _entity("sensor", device_class="distance"),
    "odometer_entity_id": _entity("sensor", device_class="distance"),
    "charge_port_door_entity_id": _entity(["cover", "binary_sensor", "sensor", "lock"]),
    "charge_cable_entity_id": _entity(["binary_sensor", "sensor"]),
    "charging_limit_entity_id": _entity(["number", "sensor"]),
    "location_entity_id": _entity(["device_tracker", "person"]),
    "last_update_entity_id": _entity("sensor", device_class="timestamp"),
    "climate_entity_id": _entity(["climate", "switch", "button", "input_boolean", "input_button"]),
    "battery_size": _number(1, 250, 0.1, "kWh"),
    "last_charging_hour_before_departure_buffer": _number(0, 12, 1, "h"),
    # home / home battery
    "power_consumption_entity_id": _entity("sensor", device_class="power"),
    "power_consumption_entity_id_include_powerwall_charging": BOOLEAN,
    "power_consumption_entity_id_include_powerwall_discharging": BOOLEAN,
    "ignore_consumption_from_entity_ids": _entity("sensor", device_class="power", multiple=True),
    "powerwall_watt_flow_entity_id": _entity("sensor", device_class="power"),
    "invert_powerwall_watt_flow_entity_id": BOOLEAN,
    "powerwall_battery_level_entity_id": _entity("sensor", device_class="battery"),
    "powerwall_battery_size": _number(0.1, 1000, 0.1, "kWh"),
    "powerwall_charging_power_limit": _number(100, 100000, 100, "W"),
    "powerwall_discharging_power": _number(100, 100000, 100, "W"),
    # prices (only entities from the price integrations)
    "energidataservice_buy_entity_id": _entity("sensor", integration="energidataservice"),
    "energidataservice_sell_entity_id": _entity("sensor", integration="energidataservice"),
    "stromligning_buy_current_price": _entity("sensor", integration="stromligning"),
    "stromligning_buy_tomorrow_available": _entity("binary_sensor", integration="stromligning"),
    "stromligning_buy_forecasts": _entity("sensor", integration="stromligning"),
    "stromligning_sell_current_price": _entity("sensor", integration="stromligning"),
    "stromligning_sell_tomorrow_available": _entity("binary_sensor", integration="stromligning"),
    "stromligning_sell_forecasts": _entity("sensor", integration="stromligning"),
    "stromligning_sell_distribution": _entity("sensor", integration="stromligning"),
    "stromligning_sell_electricity_tax": _entity("sensor", integration="stromligning"),
    "stromligning_sell_nettariff": _entity("sensor", integration="stromligning"),
    "stromligning_sell_systemtariff": _entity("sensor", integration="stromligning"),
    "stromligning_sell_surcharge": _entity("sensor", integration="stromligning"),
    "refund": _number(0, 10, 0.001, "{valuta_kwh}"),
    "sell_fee": _number(0, 10, 0.001, "{valuta_kwh}"),
    # solar / weather
    "production_entity_id": _entity("sensor", device_class="power"),
    "forecast_entity_id": _entity("sensor", integration="solcast_solar"),
    "inverter_discharging_power_limit": _number(100, 100000, 100, "W"),
    "enable_selling_during_expensive_hours": BOOLEAN,
    # -1 = use the hourly sell price
    "solar_sell_fixed_price": _number(-1, 10, 0.01, "{valuta_kwh}"),
    "solarpower_use_before_minutes": _number(0, 180, 1, "min"),
    "max_to_current_hour": BOOLEAN,
    "allow_grid_charging_above_solar_available": _number(-10000, 10000, 10, "W"),
    "charging_single_phase_min_amp": _number(0, 32, 1, "A"),
    "charging_single_phase_max_amp": _number(0, 32, 1, "A"),
    "charging_three_phase_min_amp": _number(0, 32, 1, "A"),
    "hourly_service_entity_id": _entity("weather"),
    "daily_service_entity_id": _entity("weather"),
    "outdoor_temp_entity_id": _entity("sensor", device_class="temperature"),
    # notifications
    "efficiency_on_cable_plug_in": BOOLEAN,
    "preheating": BOOLEAN,
    # advanced / database
    "cron_interval": selector.SelectSelector(selector.SelectSelectorConfig(options=CRON_INTERVALS, mode=selector.SelectSelectorMode.DROPDOWN)),
    "dashboard": BOOLEAN,
    "enable_diagnostic_file_log": BOOLEAN,
    "testing_mode": BOOLEAN,
    "power_values_db_data_to_save": _number(1, 100, 1, "d"),
    "solar_available_db_data_to_save": _number(1, 100, 1),
    "kwh_avg_prices_db_data_to_save": _number(1, 100, 1, "d"),
    "drive_efficiency_db_data_to_save": _number(1, 100, 1),
    "km_kwh_efficiency_db_data_to_save": _number(1, 100, 1),
    "charging_history_db_data_to_save": _number(1, 60, 1),
}
REQUIRED_KEYS = {
    "language",
    "site_priority",
    "site_phase_order",
    "status_entity_id",
    "power_consumtion_entity_id",
    "kwh_meter_entity_id",
    "lifetime_kwh_meter_entity_id",
    "power_voltage",
    "charging_phases",
    "charging_max_amp",
    "charging_loss",
    "battery_size",
    "last_charging_hour_before_departure_buffer",
    "powerwall_battery_size",
    "powerwall_charging_power_limit",
    "powerwall_discharging_power",
    "energidataservice_buy_entity_id",
    "stromligning_buy_current_price",
    "refund",
    "sell_fee",
    "inverter_discharging_power_limit",
    "solar_sell_fixed_price",
    "solarpower_use_before_minutes",
    "allow_grid_charging_above_solar_available",
    "charging_single_phase_min_amp",
    "charging_single_phase_max_amp",
    "charging_three_phase_min_amp",
    "cron_interval",
    "power_values_db_data_to_save",
    "solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save",
    "drive_efficiency_db_data_to_save",
    "km_kwh_efficiency_db_data_to_save",
    "charging_history_db_data_to_save",
}
INT_KEYS = {
    "cron_interval",
    "site_priority",
    "last_charging_hour_before_departure_buffer",
    "solarpower_use_before_minutes",
    "power_values_db_data_to_save",
    "solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save",
    "drive_efficiency_db_data_to_save",
    "km_kwh_efficiency_db_data_to_save",
    "charging_history_db_data_to_save",
}


def _form_value(key: str, value: Any) -> Any:
    if key == "charging_loss":
        return round(min(float(value or 0.0), 0.0) * 100, 1)
    if key == "cron_interval":
        return str(int(value)) if value not in (None, "") else "5"
    if key in LIST_OPTION_KEYS:
        return list(value or [])
    return value


def _selector(key: str, currency: str | None) -> Any:
    """SELECTORS[key] with {valuta_kwh} in the unit replaced by the currency of Home Assistant."""
    field = SELECTORS[key]
    if isinstance(field, selector.NumberSelector) and "{valuta_kwh}" in str(field.config.get("unit_of_measurement")):
        unit = field.config["unit_of_measurement"].replace("{valuta_kwh}", f"{currency_symbol(currency) or 'kr'}/kWh")
        return selector.NumberSelector(selector.NumberSelectorConfig(**{**field.config, "unit_of_measurement": unit}))
    return field


def build_schema(
    keys: list[str], values: dict[str, Any], notify_services: list[str] | None = None, currency: str | None = None
) -> vol.Schema:
    schema: dict[Any, Any] = {}
    for key in keys:
        value = _form_value(key, values.get(key))
        if key == "notify_list":
            options = sorted({*(notify_services or []), *value})
            schema[vol.Optional(key, description={"suggested_value": value})] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN)
            )
        elif key in ENTITY_OPTION_KEYS:
            marker = vol.Required if key in REQUIRED_KEYS else vol.Optional
            schema[marker(key, description={"suggested_value": value or None})] = _selector(key, currency)
        elif key in LIST_OPTION_KEYS:
            schema[vol.Optional(key, description={"suggested_value": value})] = _selector(key, currency)
        elif key in REQUIRED_KEYS:
            schema[vol.Required(key, default=value)] = _selector(key, currency)
        else:
            schema[vol.Optional(key, default=bool(value))] = _selector(key, currency)
    return vol.Schema(schema)


def normalize_input(keys: list[str], user_input: dict[str, Any]) -> dict[str, Any]:
    """Convert form values to option values (cleared optional entities become '')."""
    result: dict[str, Any] = {}
    for key in keys:
        value = user_input.get(key)
        if key in ENTITY_OPTION_KEYS:
            result[key] = value or ""
        elif key in LIST_OPTION_KEYS:
            result[key] = [item for item in (value or []) if item]
        elif key == "charging_loss":
            result[key] = round(min(float(value or 0.0), 0.0) / 100, 4)
        elif key in INT_KEYS:
            result[key] = int(float(value))
        elif SELECTORS.get(key) is BOOLEAN:
            result[key] = bool(value)
        elif isinstance(value, (int, float)):
            result[key] = float(value)
        else:
            result[key] = value
    return result


def validate_step(step: str, values: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if step == "ev":
        configured = [key for key in EV_REQUIRED if values.get(key)]
        if configured and len(configured) != len(EV_REQUIRED):
            errors["base"] = "ev_incomplete"
    if step == "solar" and values.get("forecast_entity_id") and not values.get("production_entity_id"):
        errors["production_entity_id"] = "production_required"
    if step == "solar_charging" and values["charging_single_phase_min_amp"] > values["charging_single_phase_max_amp"]:
        errors["charging_single_phase_min_amp"] = "min_above_max"
    return errors


def options_from_legacy(legacy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flatten a PyScript <prefix>_config.yaml into config entry options + initial entity values."""
    legacy = deepcopy(legacy)
    for old_path, new_path in CONFIG_KEYS_RENAMING.items():
        value = get_dict_value_with_path(legacy, old_path)
        if value is not None:
            set_dict_value_with_path(legacy, new_path, value)
            delete_dict_key_with_path(legacy, old_path)
    options = default_options()
    for key, path in OPTION_PATHS.items():
        value = get_dict_value_with_path(legacy, path)
        if value is None:
            continue
        if key in ENTITY_OPTION_KEYS:
            options[key] = value or ""
        elif key in LIST_OPTION_KEYS:
            options[key] = [e for e in value if e] if isinstance(value, list) else []
        elif key in INT_KEYS:
            options[key] = int(value)
        else:
            options[key] = value
    options["language"] = normalize_language(options.get("language"), "da")
    if str(options.get("cron_interval")) not in CRON_INTERVALS:
        options["cron_interval"] = 5
    settings = {}
    for key, info in NUMBER_SETTINGS.items():
        if info.get("config") and isinstance(value := get_dict_value_with_path(legacy, info["config"]), (int, float)):
            settings[key] = value
    return options, settings


def providers_from_options(options: dict[str, Any]) -> list[str]:
    providers = []
    if options.get("energidataservice_buy_entity_id"):
        providers.append("energidataservice")
    if options.get("stromligning_buy_current_price"):
        providers.append("stromligning")
    return providers or ["energidataservice"]


SITE_SELECTORS: dict[str, Any] = {
    "fuse_l1": _number(6, 400, 1, "A"),
    "fuse_l2": _number(6, 400, 1, "A"),
    "fuse_l3": _number(6, 400, 1, "A"),
    "margin": _number(0, 20, 0.5, "A"),
    "voltage": _number(100, 480, 1, "V"),
    "phase_l1_entity_id": _entity("sensor", device_class=["current", "power"]),
    "phase_l2_entity_id": _entity("sensor", device_class=["current", "power"]),
    "phase_l3_entity_id": _entity("sensor", device_class=["current", "power"]),
    "urgent_trip_hours": _number(0, 72, 1, "h"),
}


def site_schema(values: dict[str, Any]) -> vol.Schema:
    schema: dict[Any, Any] = {}
    for key in SITE_FIELDS:
        if key.endswith("_entity_id"):
            schema[vol.Optional(key, description={"suggested_value": values.get(key) or None})] = SITE_SELECTORS[key]
        else:
            schema[vol.Required(key, default=values.get(key, SITE_DEFAULTS[key]))] = SITE_SELECTORS[key]
    return vol.Schema(schema)


def site_options(user_input: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key in SITE_FIELDS:
        value = user_input.get(key)
        if key.endswith("_entity_id"):
            options[key] = value or ""
        elif key == "urgent_trip_hours":
            options[key] = int(float(value if value is not None else SITE_DEFAULTS[key]))
        else:
            options[key] = float(value if value is not None else SITE_DEFAULTS[key])
    return options

