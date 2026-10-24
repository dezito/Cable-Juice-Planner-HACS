"""Constants for the Cable Juice Planner integration."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .helpers import get_dict_value_with_path, set_dict_value_with_path

DOMAIN = "cable_juice_planner"
MANUFACTURER = "dezito"
TITLE = "Cable Juice Planner"

CONF_ENTITY_PREFIX = "entity_prefix"
DEFAULT_ENTITY_PREFIX = ""  # optional; entity ids are cjp_<prefix>_<key> (cjp_<key> without)
ENTITY_ID_PREFIX = "cjp"


def normalize_prefix(prefix: str | None) -> str:
    """User prefix without the fixed cjp_ (entries before version 2 stored it with it)."""
    prefix = (prefix or "").strip().lower()
    if prefix == ENTITY_ID_PREFIX:
        return ""
    if prefix.startswith(f"{ENTITY_ID_PREFIX}_"):
        return prefix[len(ENTITY_ID_PREFIX) + 1:]
    return prefix


def object_prefix(prefix: str | None) -> str:
    """cjp or cjp_<prefix>: the start of every entity id of an instance."""
    prefix = normalize_prefix(prefix)
    return f"{ENTITY_ID_PREFIX}_{prefix}" if prefix else ENTITY_ID_PREFIX


CONF_CAR_NAME = "name"


def instance_title(prefix: str | None, name: str | None = None) -> str:
    """Entry title and notification title: the car name, else the prefix."""
    label = (name or "").strip() or normalize_prefix(prefix)
    return f"Cable Juice Planner ({label})" if label else "Cable Juice Planner"


def sidebar_title(prefix: str | None, name: str | None = None) -> str:
    """Dashboard sidebar title: just the car name, else the prefix."""
    return (name or "").strip() or normalize_prefix(prefix) or "Cable Juice Planner"


def device_name(prefix: str | None, name: str | None = None) -> str:
    """Device of an instance: the car name (entities show as '<name> Overview')."""
    return (name or "").strip() or instance_title(prefix)

PLATFORMS = ["sensor", "binary_sensor", "switch", "number", "select", "time", "datetime", "button", "event"]

LEGACY_DB_FILES = {
    "charging_history": "{prefix}_charging_history_db.yaml",
    "power_values": "{prefix}_power_values_db.yaml",
    "solar_available": "{prefix}_solar_production_available_db.yaml",
    "kwh_avg_prices": "{prefix}_kwh_avg_prices_db.yaml",
    "drive_efficiency": "{prefix}_drive_efficiency_db.yaml",
    "km_kwh_efficiency": "{prefix}_km_kwh_efficiency_db.yaml",
}
# "runtime" holds state the PyScript version kept in sensor attributes (last odometer, ...)
# "charging_loss": the measured charging loss per power (engine_loss.py)
# "economy": the connection windows and their prices compared (engine_economy.py)
# "analysis": drives, departures and planned days (engine_analysis.py)
DB_NAMES = (*LEGACY_DB_FILES, "runtime", "charging_loss", "economy", "analysis")

STORAGE_VERSION = 1

POWER_VALUES_DB_VERSION = 1.0
SOLAR_PRODUCTION_AVAILABLE_DB_VERSION = 2.0
KWH_AVG_PRICES_DB_VERSION = 2.0

PRICE_ADDER_DAY_BETWEEN_DIVIDER = 30
CHARGING_ALLOWED_AFTER_GOTO_TIME = -120  # minutes

SOLAR_PRODUCTION_TRIGGER = 500.0
POWERWALL_CHARGING_TRIGGER = 500.0
POWERWALL_DISCHARGING_TRIGGER = 500.0

# Charging loss measurement: battery level that must be charged for a precise result
# (1% is ~0.75 kWh), the largest loss accepted, and how long to wait for a slow kWh meter
CHARGING_LOSS_MIN_BATTERY_LEVEL = 30.0
CHARGING_LOSS_MAX = -0.30
CHARGING_LOSS_METER_TIMEOUT = 30 * 60  # seconds

# Rolling history buffers (hours). Charger status and battery level are needed for 7
# days (peak battery level after the last plug in), power values for the 50 hour
# windows, everything else for get_state(try_history=True) and wake-up checks.
HISTORY_BUFFER_HOURS = 50
HISTORY_LONG_BUFFER_HOURS = 7 * 24 + 1
HISTORY_SHORT_BUFFER_HOURS = 25

ENTITY_UNAVAILABLE_STATES = (None, "unavailable", "unknown")
CHARGER_READY_STATUS = ("on", "connect", "connected", "ready", "ready_to_charge", "awaiting_authorization", "awaiting_start")
CHARGER_NOT_READY_STATUS = ("off", "disconnect", "disconnected")
CHARGER_COMPLETED_STATUS = ("completed", "finished")
CHARGER_CHARGING_STATUS = ("charging",)
EV_PLUGGED_STATES = ("on", "open", "plugged", "connect", "connected", "plugged_waiting_for_charge", "manual")
EV_UNPLUGGED_STATES = ("off", "closed", "unplugged", "disconnect", "disconnected")

CHARGER_ACTION_STATES = ("charge", "stop")
EV_REQUEST_TYPES = ("wake_up", "preheat_start", "preheat_stop")

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Languages with runtime texts in texts/<language>.json
LANGUAGES = {
    "da": "Dansk",
    "en": "English",
    "en-GB": "English (UK)",
    "de": "Deutsch",
    "pl": "Polski",
    "uk": "Українська",
}


def normalize_language(value: str | None, fallback: str = "en") -> str:
    """'da-DK' (PyScript config), 'en-GB' or 'da' -> a key of LANGUAGES."""
    token = str(value or "").replace("_", "-")
    if token in LANGUAGES:
        return token
    if token.lower() == "en-gb":
        return "en-GB"
    language = token.split("-")[0].lower()
    return language if language in LANGUAGES else fallback


WEATHER_CONDITION_DICT = {
    "sunny": 100,
    "windy": 80,
    "windy-variant": 80,
    "partlycloudy": 60,
    "cloudy": 40,
    "rainy": 40,
    "pouring": 20,
    "lightning": 20,
    "lightning-rainy": 20,
    "snowy": 20,
    "snowy-rainy": 20,
    "clear-night": 0,
    "fog": 20,
    "hail": 0,
    "exceptional": 0,
}

CHARGING_TYPES = {
    "error": {"priority": 1, "emoji": "☠️", "group": "status"},
    "no_rule": {"priority": 2, "emoji": "⚠️", "group": "status"},
    "manual": {"priority": 3, "emoji": "🔌", "group": "charge"},
    "low_battery": {"priority": 4, "emoji": "⛽", "group": "charge"},
    "battery_health": {"priority": 5, "emoji": "💯", "group": "charge"},
    "trip": {"priority": 6, "emoji": "🌍", "group": "drive"},
    "public_charging": {"priority": 6.5, "emoji": "🧾", "group": "charge"},
    "first_workday_preparation": {"priority": 7.0, "emoji": "1️⃣", "group": "workday"},
    "second_workday_preparation": {"priority": 7.1, "emoji": "2️⃣", "group": "workday"},
    "third_workday_preparation": {"priority": 7.2, "emoji": "3️⃣", "group": "workday"},
    "fourth_workday_preparation": {"priority": 7.3, "emoji": "4️⃣", "group": "workday"},
    "fifth_workday_preparation": {"priority": 7.4, "emoji": "5️⃣", "group": "workday"},
    "sixth_workday_preparation": {"priority": 7.5, "emoji": "6️⃣", "group": "workday"},
    "seventh_workday_preparation": {"priority": 7.6, "emoji": "7️⃣", "group": "workday"},
    "eighth_workday_preparation": {"priority": 7.7, "emoji": "8️⃣", "group": "workday"},
    "fill_up": {"priority": 8, "emoji": "📈", "group": "charge"},
    "under_min_avg_price": {"priority": 9.0, "emoji": "💵", "entity_name": "charge_very_cheap_battery_level", "group": "charge"},
    "half_min_avg_price": {"priority": 9.1, "emoji": "💶", "entity_name": "charge_ultra_cheap_battery_level", "group": "charge"},
    "grid_charging": {"priority": 10, "emoji": "⚡", "group": "charge"},
    "solar": {"priority": 11, "emoji": "☀️", "group": "charge"},
    "solar_corrected": {"priority": 11.1, "emoji": "🌤️", "group": "charge"},
    "powerwall": {"priority": 11.2, "emoji": "🛢️", "group": "charge"},
    "battery_stored": {"priority": 12, "emoji": "🔋", "group": "drive"},
    "charging_loss": {"priority": 95, "emoji": "🤖", "group": "charge"},
    "sunrise": {"priority": 96, "emoji": "🌅", "group": "info"},
    "sunset": {"priority": 97, "emoji": "🌇", "group": "info"},
    "goto": {"priority": 98, "emoji": "🚗", "group": "drive"},
    "homecoming": {"priority": 99, "emoji": "🏠", "group": "drive"},
    "battery_usage": {"priority": 100, "emoji": "📉", "group": "drive"},
    "distance": {"priority": 100.1, "emoji": "🛣️", "group": "drive"},
    "average": {"priority": 998, "emoji": "⌛", "group": "info"},
    "charging_problem": {"priority": 999, "emoji": "❗", "group": "status"},
    "charging_error": {"priority": 1000, "emoji": "❌", "group": "status"},
}

# the sections of the emoji description, in order; inline = all on one line
EMOJI_DESCRIPTION_GROUPS = (("charge", False), ("workday", True), ("drive", False), ("status", False), ("info", True))

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


# --------------------------------------------------------------------------- #
# Settings entities (the former input_* helpers)
# --------------------------------------------------------------------------- #
# "requires": ev / no_ev / solar / powerwall / powerwall_level -> the entity is only
# created for that configuration (like init() pruned DEFAULT_ENTITIES).
# "config": path in CONFIG with the initial value (the PyScript version wrote these
# from the config file into the helpers at every start).
# Values are restored on restart and migrated once from input_*.<legacy prefix>_<key>.

SWITCH_SETTINGS: dict[str, dict[str, Any]] = {
    "forced_charging_daily_battery_level": {"default": False},
    "allow_manual_charging_now": {"default": False},
    "allow_manual_charging_solar": {"default": False},
    "solar_charging": {"default": False, "requires": "solar"},
    "fill_up": {"default": False},
    "cheap_grid_charging": {"default": False},
    "learn_driving_distance": {"default": False, "requires": "ev"},
    "workplan_charging": {"default": False},
    "trip_preheat": {"default": False},
    **{f"workday_{day}": {"default": False} for day in WEEKDAYS},
    **{f"preheat_{day}": {"default": False} for day in WEEKDAYS},
    "calculate_charging_loss": {"default": False},
    # the plan uses the continuously measured charging loss instead of the configured one
    "use_measured_charging_loss": {"default": False, "requires": "ev"},
    "powerwall_discharge_above_needed": {"default": False, "requires": "powerwall_level"},
}


def _level(default: float, config: str | None = None, icon_min: int = 10) -> dict[str, Any]:
    return {"min": icon_min, "max": 100, "step": 5, "mode": "box", "unit": "%", "default": default, "config": config}


def _distance(config: str) -> dict[str, Any]:
    return {"min": 0, "max": 500, "step": 5, "mode": "box", "unit": "km", "default": 50.0, "config": config}


NUMBER_SETTINGS: dict[str, dict[str, Any]] = {
    "preheat_minutes_before": {"min": 0, "max": 60, "step": 5, "mode": "slider", "unit": "min", "default": 0.0},
    "typical_daily_distance": _distance("ev_car.typical_daily_distance_non_working_day"),
    **{f"workday_distance_needed_{day}": _distance(f"ev_car.workday_distance_needed_{day}") for day in WEEKDAYS},
    "min_daily_battery_level": _level(30.0, "ev_car.min_daily_battery_level"),
    "min_trip_battery_level": _level(25.0, "ev_car.min_trip_battery_level"),
    "min_charge_limit_battery_level": _level(50.0, "ev_car.min_charge_limit_battery_level"),
    "max_recommended_charge_limit_battery_level": _level(90.0, "ev_car.max_recommended_charge_limit_battery_level"),
    "very_cheap_grid_charging_max_battery_level": _level(70.0, "ev_car.very_cheap_grid_charging_max_battery_level"),
    "ultra_cheap_grid_charging_max_battery_level": _level(90.0, "ev_car.ultra_cheap_grid_charging_max_battery_level"),
    "battery_level": {"min": 0, "max": 100, "step": 1, "mode": "box", "unit": "%", "default": 0.0, "requires": "no_ev"},
    "completed_battery_level": {"min": 0, "max": 100, "step": 1, "mode": "box", "unit": "%", "default": 100.0, "requires": "no_ev"},
    "estimated_total_range": {"min": 0, "max": 1000, "step": 1, "mode": "box", "unit": "km", "default": 0.0, "requires": "no_ev"},
    "trip_charge_procent": {"min": 0, "max": 100, "step": 5, "mode": "slider", "unit": "%", "default": 0.0},
    "trip_range_needed": {"min": 0, "max": 1000, "step": 5, "mode": "slider", "unit": "km", "default": 0.0},
    "full_charge_recommended": {"min": 0, "max": 60, "step": 1, "mode": "slider", "unit": "d", "default": 0.0},
    "ev_charge_after_powerwall_battery_level": {"min": 0, "max": 100, "step": 1, "mode": "slider", "unit": "%", "default": 0.0, "config": "solar.ev_charge_after_powerwall_battery_level", "requires": "powerwall_level"},
    "public_charging_session_cost": {"min": -2, "max": 5000, "step": 0.01, "mode": "box", "unit": "kr", "default": 0.0, "requires": "ev"},
}

FORECAST_TYPE_OPTIONS = ["Average", "EMA (Exponential Moving Average)", "Trend"]
SELECT_SETTINGS: dict[str, dict[str, Any]] = {
    "forecast_type": {"options": FORECAST_TYPE_OPTIONS, "default": "EMA (Exponential Moving Average)"},
}

TIME_SETTINGS: dict[str, dict[str, Any]] = {
    **{f"workday_departure_{day}": {"default": "07:00:00"} for day in WEEKDAYS},
    **{f"workday_homecoming_{day}": {"default": "16:00:00"} for day in WEEKDAYS},
}

# None = "not set" (the PyScript version used 1900-01-01 00:00 = resetDatetime())
DATETIME_SETTINGS: dict[str, dict[str, Any]] = {
    "trip_date_time": {},
    "trip_homecoming_date_time": {},
}

BUTTON_KEYS = ("enforce_planning", "trip_reset")
# only with a car integration (the charging loss is measured with its battery level)
EV_BUTTON_KEYS = ("recalculate_charging_loss",)

# Output sensors: key -> (unit, device_class, state_class, requires)
SENSOR_DEFS: dict[str, tuple[str | None, str | None, str | None, str | None]] = {
    "current_charging_rule": (None, None, None, None),
    "charger_action": (None, "enum", None, None),
    "charging_limit": ("%", None, "measurement", None),
    "charger_phase_1_amps": ("A", "current", "measurement", None),
    "charger_phase_2_amps": ("A", "current", "measurement", None),
    "charger_phase_3_amps": ("A", "current", "measurement", None),
    "charging_watts": ("W", "power", "measurement", None),
    "overview": (None, None, None, None),
    "charging_history": (None, None, None, None),
    "emoji_description": (None, None, None, None),
    "kwh_cost_price": ("kr/kWh", None, "measurement", None),
    "charge_very_cheap_battery_level": ("%", None, "measurement", None),
    "charge_ultra_cheap_battery_level": ("%", None, "measurement", None),
    "solar_over_production_current_hour": ("W", "power", "measurement", "solar"),
    "solar_charged_percentage": ("%", None, "measurement", "solar"),
    "drive_efficiency": ("%", None, "measurement", "ev"),
    "km_per_kwh": ("km/kWh", None, "measurement", "ev"),
    "estimated_range": ("km", "distance", "measurement", "ev"),
    "drive_efficiency_last_battery_level": ("%", None, None, None),
    "drive_efficiency_last_odometer": ("km", "distance", None, "ev"),
    "co2_emitted": ("kg", "weight", "total_increasing", None),
    "kwh_charged_by_solar": ("kWh", "energy", "total_increasing", "solar"),
    # set by the integration when the battery was full (was a settable datetime / input_datetime)
    "last_full_charge": (None, "timestamp", None, None),
    # the measured charging loss at the max charging power, the model in the attributes
    "charging_loss_model": ("%", None, None, "ev"),
    # this month's saving compared with daily charging, the months in the attributes
    "economy": ("kr", None, None, None),
    # the share of the last 30 departures with the required battery level, the analysis in the attributes
    "analysis": ("%", None, None, "ev"),
}
# Sensor states kept across restarts (the PyScript helpers were input_numbers or
# template sensors that kept their state while PyScript reloaded)
# entry.data: the last full charge of the removed datetime entity, read once at startup
MIGRATED_LAST_FULL_CHARGE = "migrated_last_full_charge"

PERSISTENT_SENSOR_KEYS = (
    "co2_emitted",
    "kwh_charged_by_solar",
    "drive_efficiency_last_battery_level",
    "drive_efficiency_last_odometer",
    "last_full_charge",
)

SIGNAL_UPDATE = "cable_juice_planner_update_{entry_id}"

SERVICE_DEBUG_INFO = "debug_info"
SERVICE_ENFORCE_PLANNING = "enforce_planning"
SERVICE_ADD_PUBLIC_CHARGING = "manual_add_public_charging_session"
SERVICE_EXPORT_DATA = "export_data"
SERVICE_IMPORT_DATA = "import_data"


# --------------------------------------------------------------------------- #
# Site (shared grid connection): load balancing between the cars
# --------------------------------------------------------------------------- #
CONF_SITE = "site"
SITE_TITLE = "Cable Juice Planner – el-tilslutning"
# charger L1/L2/L3 -> grid phase index
PHASE_ORDERS: dict[str, tuple[int, int, int]] = {
    "L1-L2-L3": (0, 1, 2),
    "L2-L3-L1": (1, 2, 0),
    "L3-L1-L2": (2, 0, 1),
}
SITE_DEFAULTS: dict[str, Any] = {
    "fuse_l1": 25.0,
    "fuse_l2": 25.0,
    "fuse_l3": 25.0,
    "margin": 1.0,
    "voltage": 230.0,
    "phase_l1_entity_id": "",
    "phase_l2_entity_id": "",
    "phase_l3_entity_id": "",
    "urgent_trip_hours": 12,
}
SITE_FIELDS = list(SITE_DEFAULTS)
SITE_HOURS_AHEAD = 24 * 8
SITE_SENSORS = ("site_available_l1", "site_available_l2", "site_available_l3", "site_house_l1", "site_house_l2", "site_house_l3")
SIGNAL_SITE_UPDATE = "cable_juice_planner_site_update"


# Help texts of the dashboard settings: entity key -> runtime text entity_help.<id>
ENTITY_HELP_GROUPS: dict[str, str] = {
    "public_charging_session_cost": "public_charging",
    "public_charging_session_done": "public_charging",
    "forced_charging_daily_battery_level": "forced_charging",
    "allow_manual_charging_now": "manual_charging",
    "allow_manual_charging_solar": "manual_charging",
    "charge_very_cheap_battery_level": "cheap_targets",
    "charge_ultra_cheap_battery_level": "cheap_targets",
    "trip_charge_procent": "trip",
    "trip_range_needed": "trip",
    "trip_date_time": "trip",
    "trip_homecoming_date_time": "trip",
    "trip_preheat": "trip",
    "trip_reset": "trip",
    "solar_charging": "solar_charging",
    "fill_up": "fill_up",
    "typical_daily_distance": "fill_up",
    "learn_driving_distance": "workplan",
    "workplan_charging": "workplan",
    "workday_monday": "workplan",
    "workday_tuesday": "workplan",
    "workday_wednesday": "workplan",
    "workday_thursday": "workplan",
    "workday_friday": "workplan",
    "workday_saturday": "workplan",
    "workday_sunday": "workplan",
    "workday_departure_monday": "workplan",
    "workday_departure_tuesday": "workplan",
    "workday_departure_wednesday": "workplan",
    "workday_departure_thursday": "workplan",
    "workday_departure_friday": "workplan",
    "workday_departure_saturday": "workplan",
    "workday_departure_sunday": "workplan",
    "workday_homecoming_monday": "workplan",
    "workday_homecoming_tuesday": "workplan",
    "workday_homecoming_wednesday": "workplan",
    "workday_homecoming_thursday": "workplan",
    "workday_homecoming_friday": "workplan",
    "workday_homecoming_saturday": "workplan",
    "workday_homecoming_sunday": "workplan",
    "workday_distance_needed_monday": "workplan",
    "workday_distance_needed_tuesday": "workplan",
    "workday_distance_needed_wednesday": "workplan",
    "workday_distance_needed_thursday": "workplan",
    "workday_distance_needed_friday": "workplan",
    "workday_distance_needed_saturday": "workplan",
    "workday_distance_needed_sunday": "workplan",
    "full_charge_recommended": "full_charge",
    "last_full_charge": "full_charge",
    "calculate_charging_loss": "charging_loss",
    "use_measured_charging_loss": "charging_loss",
    "charging_loss_model": "charging_loss",
    "recalculate_charging_loss": "charging_loss",
    "cheap_grid_charging": "cheap_limits",
    "very_cheap_grid_charging_max_battery_level": "cheap_limits",
    "ultra_cheap_grid_charging_max_battery_level": "cheap_limits",
    "min_daily_battery_level": "battery_preheat",
    "min_trip_battery_level": "battery_preheat",
    "min_charge_limit_battery_level": "battery_preheat",
    "max_recommended_charge_limit_battery_level": "battery_preheat",
    "preheat_minutes_before": "battery_preheat",
    "preheat_monday": "battery_preheat",
    "preheat_tuesday": "battery_preheat",
    "preheat_wednesday": "battery_preheat",
    "preheat_thursday": "battery_preheat",
    "preheat_friday": "battery_preheat",
    "preheat_saturday": "battery_preheat",
    "preheat_sunday": "battery_preheat",
    "ev_charge_after_powerwall_battery_level": "powerwall",
    "powerwall_discharge_above_needed": "powerwall",
    "battery_level": "virtual_ev",
    "completed_battery_level": "virtual_ev",
    "estimated_total_range": "virtual_ev",
}
