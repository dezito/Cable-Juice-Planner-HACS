"""Trips and public charging."""
from __future__ import annotations

import datetime
import logging

from ..utils.helpers import reset_datetime

_LOGGER = logging.getLogger(__name__)


class TripAndPublicCharging:
    """Trips and public charging."""

    def get_trip_date_time(self) -> datetime.datetime:
        return self.datetime_state("trip_date_time", error_state=reset_datetime())

    def get_trip_homecoming_date_time(self) -> datetime.datetime:
        return self.datetime_state("trip_homecoming_date_time", error_state=reset_datetime())

    def get_trip_range(self) -> float:
        value = self.number_state("trip_range_needed")
        if value is None:
            _LOGGER.error("trip_range_needed is not set to a number")
            self.set_setting("trip_range_needed", 0.0)
            return 0.0
        return float(value)

    def get_trip_target_level(self) -> float:
        value = self.number_state("trip_charge_procent")
        if value is None:
            _LOGGER.error("trip_charge_procent is not set to a number")
            self.set_setting("trip_charge_procent", 0.0)
            return 0.0
        return float(value)

    def get_public_charging_session_done(self) -> bool:
        if not self.is_ev_configured():
            return False
        return self.sensor_states.get("public_charging_session_done") == "on"

    def is_trip_planned(self) -> bool:
        for key in ("trip_charge_procent", "trip_range_needed", "trip_date_time", "trip_homecoming_date_time"):
            if not self.has_setting(key):
                return False
        if self.get_trip_range() == 0.0 and self.get_trip_target_level() == 0.0:
            return False
        if self.get_trip_date_time() == reset_datetime() or self.get_trip_homecoming_date_time() == reset_datetime():
            return False
        return True
