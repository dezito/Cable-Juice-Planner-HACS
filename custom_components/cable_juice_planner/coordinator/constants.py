"""Timings and keys shared by the coordinator modules."""
import datetime

from ..const import CHARGER_CHARGING_STATUS, CHARGER_COMPLETED_STATUS, CHARGER_READY_STATUS

STATISTICS_REFRESH_SECONDS = 300
SETTINGS_DEBOUNCE_SECONDS = 5
CHARGING_LOSS_STATISTICS_DAYS = 730  # the most long-term statistics read for the charging loss
CHARGING_LOSS_STATISTICS_CHUNK_DAYS = 30
# charger states with the car plugged in (a connection window for the economy)
CONNECTED_STATUS = (*CHARGER_READY_STATUS, *CHARGER_CHARGING_STATUS, *CHARGER_COMPLETED_STATUS)
FORECAST_TIMEOUT = 30
PUBLIC_CHARGING_COST_DELAY = 60
BATTERY_LEVEL_WAIT = datetime.timedelta(minutes=30)

# Settings with their own trigger (as the PyScript state triggers)
RULE_KEYS = ("fill_up", "workplan_charging")
CHARGE_TRIGGER_KEYS = ("allow_manual_charging_now", "allow_manual_charging_solar", "calculate_charging_loss")
TRIP_KEYS = ("trip_charge_procent", "trip_range_needed", "trip_date_time", "trip_homecoming_date_time")
