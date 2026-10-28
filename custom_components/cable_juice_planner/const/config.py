from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..utils.helpers import get_dict_value_with_path, set_dict_value_with_path

# Same nested layout as <prefix>_config.yaml in the PyScript version, so the ported
# algorithm reads CONFIG["charger"]["charging_max_amp"] etc. unchanged. Values the
# dashboard changes at runtime are entities (see the *_SETTINGS dicts below); their
# defaults stay here because the algorithm falls back to them.
DEFAULT_CONFIG: dict[str, Any] = {
    "charger": {
        "entity_ids": {
            "status_entity_id": "",
            "power_consumtion_entity_id": "",
            "kwh_meter_entity_id": "",
            "lifetime_kwh_meter_entity_id": "",
            "enabled_entity_id": "",
            "co2_entity_id": "",
            "cable_connected_entity_id": "",
            "other_ev_using_this_charger_entity_ids": [],
        },
        "power_voltage": 240.0,
        "charging_phases": 3.0,
        "charging_max_amp": 16.0,
        "charging_loss": -0.05,
    },
    "cron_interval": 5,
    "dashboard": True,
    "database": {
        "power_values_db_data_to_save": 15,
        "solar_available_db_data_to_save": 10,
        "kwh_avg_prices_db_data_to_save": 15,
        "drive_efficiency_db_data_to_save": 15,
        "km_kwh_efficiency_db_data_to_save": 15,
        "charging_history_db_data_to_save": 12,
    },
    "enable_diagnostic_file_log": False,
    "ev_car": {
        "entity_ids": {
            "climate_entity_id": "",
            "odometer_entity_id": "",
            "estimated_battery_range_entity_id": "",
            "usable_battery_level_entity_id": "",
            "charge_port_door_entity_id": "",
            "charge_cable_entity_id": "",
            "charging_limit_entity_id": "",
            "location_entity_id": "",
            "last_update_entity_id": "",
        },
        "battery_size": 75.0,
        "min_daily_battery_level": 30.0,
        "min_trip_battery_level": 25.0,
        "min_charge_limit_battery_level": 50.0,
        "max_recommended_charge_limit_battery_level": 90.0,
        "very_cheap_grid_charging_max_battery_level": 70.0,
        "ultra_cheap_grid_charging_max_battery_level": 90.0,
        "typical_daily_distance_non_working_day": 50.0,
        "workday_distance_needed_monday": 50.0,
        "workday_distance_needed_tuesday": 50.0,
        "workday_distance_needed_wednesday": 50.0,
        "workday_distance_needed_thursday": 50.0,
        "workday_distance_needed_friday": 50.0,
        "workday_distance_needed_saturday": 50.0,
        "workday_distance_needed_sunday": 50.0,
        "last_charging_hour_before_departure_buffer": 2,
    },
    "forecast": {
        "entity_ids": {
            "hourly_service_entity_id": "",
            "daily_service_entity_id": "",
            "outdoor_temp_entity_id": "",
        }
    },
    "home": {
        "entity_ids": {
            "power_consumption_entity_id": "",
            "powerwall_watt_flow_entity_id": "",
            "powerwall_battery_level_entity_id": "",
            "ignore_consumption_from_entity_ids": [],
        },
        "power_consumption_entity_id_include_powerwall_charging": False,
        "power_consumption_entity_id_include_powerwall_discharging": False,
        "invert_powerwall_watt_flow_entity_id": False,
    },
    "language": "da",
    "notification": {
        "efficiency_on_cable_plug_in": True,
        "preheating": True,
    },
    "notify_list": [],
    "prices": {
        "entity_ids": {
            "energidataservice_buy_entity_id": "",
            "energidataservice_sell_entity_id": "",
            "stromligning_buy_current_price": "",
            "stromligning_buy_tomorrow_available": "",
            "stromligning_buy_forecasts": "",
            "stromligning_sell_current_price": "",
            "stromligning_sell_tomorrow_available": "",
            "stromligning_sell_forecasts": "",
            "stromligning_sell_distribution": "",
            "stromligning_sell_electricity_tax": "",
            "stromligning_sell_nettariff": "",
            "stromligning_sell_systemtariff": "",
            "stromligning_sell_surcharge": "",
        },
        "refund": 0.0,
        "sell_fee": 0.3,
    },
    "solar": {
        "entity_ids": {
            "production_entity_id": "",
            "forecast_entity_id": "",
        },
        "solarpower_use_before_minutes": 15,
        "max_to_current_hour": False,
        "allow_grid_charging_above_solar_available": -100.0,
        "charging_single_phase_min_amp": 6.0,
        "charging_single_phase_max_amp": 16.0,
        "charging_three_phase_min_amp": 5.0,
        "production_price": -1.0,
        "ev_charge_after_powerwall_battery_level": 0.0,
        "powerwall_battery_size": 10.0,
        "inverter_discharging_power_limit": 5000.0,
        "powerwall_charging_power_limit": 5000.0,
        "powerwall_discharging_power": 5000.0,
        "enable_selling_during_expensive_hours": True,
    },
    "testing_mode": False,
    # load balancing with the other cars of the site (only used when a site entry exists)
    "site": {
        "priority": 1,
        "phase_order": "L1-L2-L3",
    },
}

# Old path -> new path in the PyScript config (applied on import)
CONFIG_KEYS_RENAMING = {
    "home.ignore_consumption_from_entity_id": "home.ignore_consumption_from_entity_ids",
    "ev_car.daily_drive_distance": "ev_car.typical_daily_distance_non_working_day",
    "charger.entity_ids.dynamic_circuit_limit": "charger.entity_ids.dynamic_circuit_limit_entity_id",
    "prices.entity_ids.power_prices_entity_id": "prices.entity_ids.energidataservice_buy_entity_id",
}

# Config flow option key -> path in DEFAULT_CONFIG
OPTION_PATHS: dict[str, str] = {
    "language": "language",
    # charger
    "status_entity_id": "charger.entity_ids.status_entity_id",
    "power_consumtion_entity_id": "charger.entity_ids.power_consumtion_entity_id",
    "kwh_meter_entity_id": "charger.entity_ids.kwh_meter_entity_id",
    "lifetime_kwh_meter_entity_id": "charger.entity_ids.lifetime_kwh_meter_entity_id",
    "enabled_entity_id": "charger.entity_ids.enabled_entity_id",
    "cable_connected_entity_id": "charger.entity_ids.cable_connected_entity_id",
    "co2_entity_id": "charger.entity_ids.co2_entity_id",
    "power_voltage": "charger.power_voltage",
    "charging_phases": "charger.charging_phases",
    "charging_max_amp": "charger.charging_max_amp",
    "charging_loss": "charger.charging_loss",
    # ev
    "usable_battery_level_entity_id": "ev_car.entity_ids.usable_battery_level_entity_id",
    "estimated_battery_range_entity_id": "ev_car.entity_ids.estimated_battery_range_entity_id",
    "odometer_entity_id": "ev_car.entity_ids.odometer_entity_id",
    "charge_port_door_entity_id": "ev_car.entity_ids.charge_port_door_entity_id",
    "charge_cable_entity_id": "ev_car.entity_ids.charge_cable_entity_id",
    "charging_limit_entity_id": "ev_car.entity_ids.charging_limit_entity_id",
    "location_entity_id": "ev_car.entity_ids.location_entity_id",
    "last_update_entity_id": "ev_car.entity_ids.last_update_entity_id",
    "climate_entity_id": "ev_car.entity_ids.climate_entity_id",
    "battery_size": "ev_car.battery_size",
    "last_charging_hour_before_departure_buffer": "ev_car.last_charging_hour_before_departure_buffer",
    # home
    "power_consumption_entity_id": "home.entity_ids.power_consumption_entity_id",
    "power_consumption_entity_id_include_powerwall_charging": "home.power_consumption_entity_id_include_powerwall_charging",
    "power_consumption_entity_id_include_powerwall_discharging": "home.power_consumption_entity_id_include_powerwall_discharging",
    "ignore_consumption_from_entity_ids": "home.entity_ids.ignore_consumption_from_entity_ids",
    "powerwall_watt_flow_entity_id": "home.entity_ids.powerwall_watt_flow_entity_id",
    "invert_powerwall_watt_flow_entity_id": "home.invert_powerwall_watt_flow_entity_id",
    "powerwall_battery_level_entity_id": "home.entity_ids.powerwall_battery_level_entity_id",
    "powerwall_battery_size": "solar.powerwall_battery_size",
    "powerwall_charging_power_limit": "solar.powerwall_charging_power_limit",
    "powerwall_discharging_power": "solar.powerwall_discharging_power",
    # prices
    "energidataservice_buy_entity_id": "prices.entity_ids.energidataservice_buy_entity_id",
    "energidataservice_sell_entity_id": "prices.entity_ids.energidataservice_sell_entity_id",
    "stromligning_buy_current_price": "prices.entity_ids.stromligning_buy_current_price",
    "stromligning_buy_tomorrow_available": "prices.entity_ids.stromligning_buy_tomorrow_available",
    "stromligning_buy_forecasts": "prices.entity_ids.stromligning_buy_forecasts",
    "stromligning_sell_current_price": "prices.entity_ids.stromligning_sell_current_price",
    "stromligning_sell_tomorrow_available": "prices.entity_ids.stromligning_sell_tomorrow_available",
    "stromligning_sell_forecasts": "prices.entity_ids.stromligning_sell_forecasts",
    "stromligning_sell_distribution": "prices.entity_ids.stromligning_sell_distribution",
    "stromligning_sell_electricity_tax": "prices.entity_ids.stromligning_sell_electricity_tax",
    "stromligning_sell_nettariff": "prices.entity_ids.stromligning_sell_nettariff",
    "stromligning_sell_systemtariff": "prices.entity_ids.stromligning_sell_systemtariff",
    "stromligning_sell_surcharge": "prices.entity_ids.stromligning_sell_surcharge",
    "refund": "prices.refund",
    "sell_fee": "prices.sell_fee",
    # solar
    "production_entity_id": "solar.entity_ids.production_entity_id",
    "forecast_entity_id": "solar.entity_ids.forecast_entity_id",
    "inverter_discharging_power_limit": "solar.inverter_discharging_power_limit",
    "enable_selling_during_expensive_hours": "solar.enable_selling_during_expensive_hours",
    "solar_sell_fixed_price": "solar.production_price",
    "solarpower_use_before_minutes": "solar.solarpower_use_before_minutes",
    "max_to_current_hour": "solar.max_to_current_hour",
    "allow_grid_charging_above_solar_available": "solar.allow_grid_charging_above_solar_available",
    "charging_single_phase_min_amp": "solar.charging_single_phase_min_amp",
    "charging_single_phase_max_amp": "solar.charging_single_phase_max_amp",
    "charging_three_phase_min_amp": "solar.charging_three_phase_min_amp",
    # weather
    "hourly_service_entity_id": "forecast.entity_ids.hourly_service_entity_id",
    "daily_service_entity_id": "forecast.entity_ids.daily_service_entity_id",
    "outdoor_temp_entity_id": "forecast.entity_ids.outdoor_temp_entity_id",
    # notifications
    "notify_list": "notify_list",
    "efficiency_on_cable_plug_in": "notification.efficiency_on_cable_plug_in",
    "preheating": "notification.preheating",
    # advanced / database
    "cron_interval": "cron_interval",
    "dashboard": "dashboard",
    "enable_diagnostic_file_log": "enable_diagnostic_file_log",
    "testing_mode": "testing_mode",
    "site_priority": "site.priority",
    "site_phase_order": "site.phase_order",
    "power_values_db_data_to_save": "database.power_values_db_data_to_save",
    "solar_available_db_data_to_save": "database.solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save": "database.kwh_avg_prices_db_data_to_save",
    "drive_efficiency_db_data_to_save": "database.drive_efficiency_db_data_to_save",
    "km_kwh_efficiency_db_data_to_save": "database.km_kwh_efficiency_db_data_to_save",
    "charging_history_db_data_to_save": "database.charging_history_db_data_to_save",
}

# Config/options flow pages: step id -> option keys shown on that page
FLOW_STEPS: dict[str, list[str]] = {
    "general": ["language"],
    "charger": [
        "status_entity_id",
        "power_consumtion_entity_id",
        "kwh_meter_entity_id",
        "lifetime_kwh_meter_entity_id",
        "enabled_entity_id",
        "cable_connected_entity_id",
        "co2_entity_id",
    ],
    "charger_specs": ["power_voltage", "charging_phases", "charging_max_amp", "charging_loss"],
    "load_balancing": ["site_priority", "site_phase_order"],
    "ev": [
        "usable_battery_level_entity_id",
        "estimated_battery_range_entity_id",
        "odometer_entity_id",
        "charge_port_door_entity_id",
        "charge_cable_entity_id",
        "charging_limit_entity_id",
        "location_entity_id",
        "last_update_entity_id",
        "climate_entity_id",
    ],
    "ev_specs": ["battery_size", "last_charging_hour_before_departure_buffer"],
    "home": [
        "power_consumption_entity_id",
        "power_consumption_entity_id_include_powerwall_charging",
        "power_consumption_entity_id_include_powerwall_discharging",
        "ignore_consumption_from_entity_ids",
    ],
    "powerwall": [
        "powerwall_watt_flow_entity_id",
        "invert_powerwall_watt_flow_entity_id",
        "powerwall_battery_level_entity_id",
        "powerwall_battery_size",
        "powerwall_charging_power_limit",
        "powerwall_discharging_power",
    ],
    "prices_energidataservice": ["energidataservice_buy_entity_id", "energidataservice_sell_entity_id"],
    "prices_stromligning": [
        "stromligning_buy_current_price",
        "stromligning_buy_tomorrow_available",
        "stromligning_buy_forecasts",
        "stromligning_sell_current_price",
        "stromligning_sell_tomorrow_available",
        "stromligning_sell_forecasts",
    ],
    "prices_stromligning_tariffs": [
        "stromligning_sell_distribution",
        "stromligning_sell_electricity_tax",
        "stromligning_sell_nettariff",
        "stromligning_sell_systemtariff",
        "stromligning_sell_surcharge",
    ],
    "price_fees": ["refund", "sell_fee"],
    "solar": ["production_entity_id", "forecast_entity_id", "inverter_discharging_power_limit", "enable_selling_during_expensive_hours", "solar_sell_fixed_price"],
    "solar_charging": [
        "solarpower_use_before_minutes",
        "max_to_current_hour",
        "allow_grid_charging_above_solar_available",
        "charging_single_phase_min_amp",
        "charging_single_phase_max_amp",
        "charging_three_phase_min_amp",
    ],
    "weather": ["hourly_service_entity_id", "daily_service_entity_id", "outdoor_temp_entity_id"],
    "notifications": ["notify_list", "efficiency_on_cable_plug_in", "preheating"],
    "advanced": ["cron_interval", "dashboard", "enable_diagnostic_file_log", "testing_mode"],
    "database": [
        "power_values_db_data_to_save",
        "solar_available_db_data_to_save",
        "kwh_avg_prices_db_data_to_save",
        "drive_efficiency_db_data_to_save",
        "km_kwh_efficiency_db_data_to_save",
        "charging_history_db_data_to_save",
    ],
}

PRICE_PROVIDER_KEYS: dict[str, list[str]] = {
    "energidataservice": FLOW_STEPS["prices_energidataservice"],
    "stromligning": FLOW_STEPS["prices_stromligning"] + FLOW_STEPS["prices_stromligning_tariffs"],
}

# Options flow menu -> pages walked through in order
OPTIONS_MENU: dict[str, list[str]] = {
    "general": ["general"],
    "charger": ["charger", "charger_specs", "load_balancing"],
    "ev": ["ev", "ev_specs"],
    "home": ["home", "powerwall"],
    "prices": ["price_provider", "prices_energidataservice", "prices_stromligning", "prices_stromligning_tariffs", "price_fees"],
    "solar": ["solar", "solar_charging", "weather"],
    "notifications": ["notifications"],
    "advanced": ["advanced", "database"],
}

# Option keys holding a list of entity ids / services
# other_ev_using_this_charger_entity_ids is no longer configured: the shared grid connection
# (priority + main fuse) replaces it, the engine keeps its empty default
LIST_OPTION_KEYS = {"ignore_consumption_from_entity_ids", "notify_list"}
# Option keys holding a single entity id ("" when not configured)
ENTITY_OPTION_KEYS = {key for key, path in OPTION_PATHS.items() if ".entity_ids." in path} - LIST_OPTION_KEYS


def default_options() -> dict[str, Any]:
    """Flat option dict with the default value for every config flow key."""
    return {key: deepcopy(get_dict_value_with_path(DEFAULT_CONFIG, path)) for key, path in OPTION_PATHS.items()}


def build_config(options: dict[str, Any]) -> dict[str, Any]:
    """Build the nested CONFIG dict from flat config entry options."""
    config = deepcopy(DEFAULT_CONFIG)
    for key, path in OPTION_PATHS.items():
        if key in options and options[key] is not None:
            set_dict_value_with_path(config, path, deepcopy(options[key]))
    return config


def is_charger_configured(config: dict) -> bool:
    ids = config["charger"]["entity_ids"]
    return bool(ids["kwh_meter_entity_id"] and ids["lifetime_kwh_meter_entity_id"] and ids["power_consumtion_entity_id"] and ids["status_entity_id"])


def is_solar_configured(config: dict) -> bool:
    return bool(config["solar"]["entity_ids"]["production_entity_id"])


def is_powerwall_configured(config: dict) -> bool:
    return bool(config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"])


def is_ev_configured(config: dict) -> bool:
    ids = config["ev_car"]["entity_ids"]
    return bool(ids["odometer_entity_id"] and ids["estimated_battery_range_entity_id"] and ids["usable_battery_level_entity_id"])
