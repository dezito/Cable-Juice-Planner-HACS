"""The values the future-charging rules share."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FutureContext:
    """The inputs of the method and the state its rules share."""

    battery_expenses: Any
    chargeHours: Any
    charge_hours_alternative: Any
    charging_plan: Any
    cheap_average_price: Any
    current_hour: Any
    energy_prediction_db: Any
    fill_up_price_limits: Any
    grid_prices: Any
    now: Any
    solar_over_production: Any
    sorted_by_cheapest_price: Any
    trip_date_time: Any
    trip_homecoming_date_time: Any
    trip_range: Any
    work_overview: Any
    totalCost: Any
    totalkWh: Any
    charger_status: Any = None
    day_after: Any = None
    day_before: Any = None
    days_need_between_recommended_full_charge: Any = None
    days_since_last_fully_charged: Any = None
    ignored_reference_battery_level: Any = None
    last_charging: Any = None
    solar_unit: Any = None
    sub_func_name: Any = None
    ultra_cheap_max_battery_level: Any = None
    unused_solar_cost: Any = None
    unused_solar_kwh: Any = None
    very_cheap_max_battery_level: Any = None
