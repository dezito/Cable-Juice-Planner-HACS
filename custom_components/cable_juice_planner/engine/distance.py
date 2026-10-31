"""Learned driving distance per weekday (engine part).

The odometer is read every evening (hour_end at 23:59). The distance of a day is the
difference to the reading the evening before. From the last weeks:

- workday_distance_needed_<day>: the median of that weekday, so one long trip does not
  move it;
- typical_daily_distance: the 75th percentile of all days, a buffer above the normal day.

The suggestions are always shown as the ``learned_distance`` attribute of the number
entities. With switch.<prefix>_learn_driving_distance on, the plan uses the higher of the
entered number and the suggestion once there are enough days; the numbers are never changed,
so a higher distance entered for a single day still counts. Extra driving in a week is
covered by min_daily_battery_level.
"""
from __future__ import annotations

import datetime
import logging
import math

from ..const import WEEKDAYS

_LOGGER = logging.getLogger(__name__)

DISTANCE_WEEKS = 8
DISTANCE_MIN_WEEKDAY_SAMPLES = 3
DISTANCE_MIN_DAYS = 14
DISTANCE_PERCENTILE = 0.75
DISTANCE_STEP = 5.0  # the step of the distance number entities
DISTANCE_MAX = 500.0


def _round_up_to_step(distance: float) -> float:
    return min(math.ceil(max(distance, 0.0) / DISTANCE_STEP) * DISTANCE_STEP, DISTANCE_MAX)


def _median(values: list[float]) -> float:
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _percentile(values: list[float], fraction: float) -> float:
    """Linear interpolation between the closest ranks."""
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


class Distance:
    """Daily odometer readings and the distance suggestions made from them."""

    def learn_driving_distance_enabled(self):
        return True if self.switch_state("learn_driving_distance") == "on" else None

    def record_daily_odometer(self) -> None:
        """hour_end: the odometer at the end of the day."""
        if not self.is_ev_configured() or self.now().hour != 23:
            return
        odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
        if not self.is_entity_available(odometer_entity):
            return
        try:
            odometer = float(self.get_state(odometer_entity, float_type=True, error_state=None))
        except (TypeError, ValueError) as err:
            _LOGGER.warning("Cant read the odometer for the learned driving distance: %s", err)
            return
        today = self.now().date()
        self.daily_odometer[today.isoformat()] = odometer
        oldest = (today - datetime.timedelta(weeks=DISTANCE_WEEKS, days=1)).isoformat()
        self.daily_odometer = {date: value for date, value in self.daily_odometer.items() if date >= oldest}
        self.mark_dirty("runtime")
        self.update_learned_distances()

    def daily_distances(self) -> dict[datetime.date, float]:
        """Distance per day, only for days with a reading the evening before."""
        readings = {datetime.date.fromisoformat(date): value for date, value in self.daily_odometer.items()}
        distances = {}
        for date, value in readings.items():
            previous = readings.get(date - datetime.timedelta(days=1))
            if previous is not None and value >= previous:
                distances[date] = value - previous
        return distances

    def with_learned_distance(self, key: str, entered: float) -> float:
        """The entered distance of a number, with learning on the learned one when that is higher."""
        learned = self.learned_distances.get(key) if self.learn_driving_distance_enabled() else None
        return entered if learned is None else max(entered, learned)

    def update_learned_distances(self) -> None:
        if not self.is_ev_configured():
            return
        distances = self.daily_distances()
        learned = {}  # only the suggestions with enough days, used by with_learned_distance

        for day in WEEKDAYS:
            samples = [distance for date, distance in distances.items() if WEEKDAYS[date.weekday()] == day]
            key = f"workday_distance_needed_{day}"
            suggestion = _round_up_to_step(_median(samples)) if samples else None
            self.set_attr(key, "learned_distance", suggestion)
            self.set_attr(key, "learned_days", len(samples))
            if suggestion is not None and len(samples) >= DISTANCE_MIN_WEEKDAY_SAMPLES:
                learned[key] = suggestion

        samples = list(distances.values())
        suggestion = _round_up_to_step(_percentile(samples, DISTANCE_PERCENTILE)) if samples else None
        self.set_attr("typical_daily_distance", "learned_distance", suggestion)
        self.set_attr("typical_daily_distance", "learned_days", len(samples))
        if suggestion is not None and len(samples) >= DISTANCE_MIN_DAYS:
            learned["typical_daily_distance"] = suggestion
        self.learned_distances = learned
