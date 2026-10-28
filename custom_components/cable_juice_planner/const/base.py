from __future__ import annotations

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
# "charging_loss": the measured charging loss per power (engine/loss.py)
# "economy": the connection windows and their prices compared (engine/economy.py)
# "analysis": drives, departures and planned days (engine/analysis)
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


# Symbol shown for hass.config.currency (ISO 4217); other currencies are shown with their code
CURRENCY_SYMBOLS = {
    "DKK": "kr",
    "SEK": "kr",
    "NOK": "kr",
    "ISK": "kr",
    "EUR": "€",
    "GBP": "£",
    "USD": "$",
    "PLN": "zł",
    "UAH": "₴",
    "CZK": "Kč",
    "HUF": "Ft",
}


def currency_symbol(currency: str | None) -> str | None:
    """'DKK' -> 'kr', 'CHF' -> 'CHF', None/'' -> None (the language's own valuta texts are used)."""
    if not currency:
        return None
    return CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())


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

# the sections of the emoji description, in order
EMOJI_DESCRIPTION_GROUPS = ("charge", "workday", "drive", "status", "info")
