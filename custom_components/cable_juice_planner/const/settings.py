from __future__ import annotations

from typing import Any
from .base import WEEKDAYS

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
    # correct the price forecast with its measured bias (engine/analysis)
    "price_forecast_correction": {"default": True},
}


def _level(default: float, config: str | None = None, icon_min: int = 10) -> dict[str, Any]:
    return {"min": icon_min, "max": 100, "step": 5, "mode": "box", "unit": "%", "default": default, "config": config}


def _distance(config: str) -> dict[str, Any]:
    return {"min": 0, "max": 500, "step": 5, "mode": "box", "unit": "{distance}", "default": 50.0, "config": config}


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
    "estimated_total_range": {"min": 0, "max": 1000, "step": 1, "mode": "box", "unit": "{distance}", "default": 0.0, "requires": "no_ev"},
    "trip_charge_procent": {"min": 0, "max": 100, "step": 5, "mode": "slider", "unit": "%", "default": 0.0},
    "trip_range_needed": {"min": 0, "max": 1000, "step": 5, "mode": "slider", "unit": "{distance}", "default": 0.0},
    "full_charge_recommended": {"min": 0, "max": 60, "step": 1, "mode": "slider", "unit": "d", "default": 0.0},
    "ev_charge_after_powerwall_battery_level": {"min": 0, "max": 100, "step": 1, "mode": "slider", "unit": "%", "default": 0.0, "config": "solar.ev_charge_after_powerwall_battery_level", "requires": "powerwall_level"},
    "public_charging_session_cost": {"min": -2, "max": 5000, "step": 0.01, "mode": "box", "unit": "{valuta}", "default": 0.0, "requires": "ev"},
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
    "kwh_cost_price": ("{valuta_kwh}", None, "measurement", None),
    "charge_very_cheap_battery_level": ("%", None, "measurement", None),
    "charge_ultra_cheap_battery_level": ("%", None, "measurement", None),
    "solar_over_production_current_hour": ("W", "power", "measurement", "solar"),
    "solar_charged_percentage": ("%", None, "measurement", "solar"),
    "drive_efficiency": ("%", None, "measurement", "ev"),
    "km_per_kwh": ("{distance}/kWh", None, "measurement", "ev"),
    "estimated_range": ("{distance}", "distance", "measurement", "ev"),
    "drive_efficiency_last_battery_level": ("%", None, None, None),
    "drive_efficiency_last_odometer": ("{distance}", "distance", None, "ev"),
    "co2_emitted": ("kg", "weight", "total_increasing", None),
    "kwh_charged_by_solar": ("kWh", "energy", "total_increasing", "solar"),
    # set by the integration when the battery was full (was a settable datetime / input_datetime)
    "last_full_charge": (None, "timestamp", None, None),
    # the measured charging loss at the max charging power, the model in the attributes
    "charging_loss_model": ("%", None, None, "ev"),
    # this month's saving compared with daily charging, the months in the attributes
    "economy": ("{valuta}", None, None, None),
    # the share of the last 30 departures with the required battery level, the analysis in the attributes
    # (also without a car: the price forecast, house consumption forecast and planned days)
    "analysis": ("%", None, None, None),
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
