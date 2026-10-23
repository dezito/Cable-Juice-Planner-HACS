"""History values from the rolling state buffers and hourly statistics.

The PyScript version asked the recorder for every window (``history.get_values`` /
``get_average_value``, many queries per planning run on the event loop). The
coordinator keeps a buffer of states per entity instead (one recorder load at
startup + state events) and the hourly mean statistics, and this module reproduces
the old semantics exactly:

* short term: the state at the window start (placed at the start) and every state
  inside the window; numeric states are interpolated to 100 points
  (``numpy.linspace`` + ``numpy.interp``, reimplemented with the same float
  operations), non-numeric states are kept with their own timestamp
* long term: hourly mean statistics for the hours of the window, interpolated the same
  way
* average = mean of both point sets

Runs in the worker thread. The rows are (epoch seconds, raw state string).
"""
from __future__ import annotations

import bisect
import datetime
import logging

_LOGGER = logging.getLogger(__name__)

ENTITY_UNAVAILABLE_STATES = (None, "unavailable", "unknown")
NUM_POINTS = 100

_CONVERSION_FACTORS = {
    "mm": {"cm": 0.1, "m": 0.001, "km": 0.000001},
    "cm": {"mm": 10, "m": 0.01, "km": 0.0001},
    "m": {"mm": 1000, "cm": 100, "km": 0.001},
    "km": {"mm": 1000000, "cm": 100000, "m": 1000},
    "kg": {"g": 1000},
    "g": {"kg": 0.001},
    "L": {"ml": 1000},
    "ml": {"L": 0.001},
    "mW": {"mW": 1, "W": 0.001, "kW": 0.000001, "MW": 0.000000001, "GW": 0.000000000001},
    "W": {"mW": 1000, "W": 1, "kW": 0.001, "MW": 0.000001, "GW": 0.000000001},
    "kW": {"mW": 1000000, "W": 1000, "kW": 1, "MW": 0.001, "GW": 0.000001},
    "MW": {"mW": 1000000000, "W": 1000000, "kW": 1000, "MW": 1, "GW": 0.001},
    "GW": {"mW": 1000000000000, "W": 1000000000, "kW": 1000000, "GW": 1},
}


def power_convert(value, unit: str | None, convert_to: str):
    """power_convert.power_convert: unknown units or values are returned unchanged."""
    try:
        from_unit = unit.replace("h", "")
        to_unit = convert_to.replace("h", "")
        if from_unit not in _CONVERSION_FACTORS or to_unit not in _CONVERSION_FACTORS:
            raise ValueError(f"Invalid units: {from_unit} or {to_unit}")
        return value * _CONVERSION_FACTORS[from_unit][to_unit]
    except Exception:  # noqa: BLE001
        return value


def linspace(start: float, stop: float, num: int) -> list[float]:
    """numpy.linspace(start, stop, num) with numpy's float operations."""
    if num == 1:
        return [start]
    step = (stop - start) / (num - 1)
    if step == 0:
        delta = stop - start
        values = [(i / (num - 1)) * delta + start for i in range(num)]
    else:
        values = [float(i) * step + start for i in range(num)]
    values[-1] = stop
    return values


def interp(xs: list[float], xp: list[float], fp: list[float]) -> list[float]:
    """numpy.interp(xs, xp, fp) with numpy's float operations (xp strictly increasing)."""
    count = len(xp)
    if count == 1:
        return [fp[0] for _ in xs]
    slopes = [(fp[i + 1] - fp[i]) / (xp[i + 1] - xp[i]) for i in range(count - 1)]
    first, last = xp[0], xp[-1]
    out = []
    for x in xs:
        if x < first:
            out.append(fp[0])
        elif x > last:
            out.append(fp[-1])
        else:
            j = bisect.bisect_right(xp, x) - 1
            if j >= count - 1:
                out.append(fp[count - 1])
            elif xp[j] == x:
                out.append(fp[j])
            else:
                slope = slopes[j]
                value = slope * (x - xp[j]) + fp[j]
                if value != value:
                    value = slope * (x - xp[j + 1]) + fp[j + 1]
                    if value != value and fp[j] == fp[j + 1]:
                        value = fp[j]
                out.append(value)
    return out


class History:
    """History access for one worker job.

    buffers:    entity_id -> sorted list of (epoch_ts, state string)
    statistics: entity_id -> sorted list of (hour_start_epoch_ts, mean or None)
    units:      entity_id -> unit_of_measurement of the current state
    """

    def __init__(self, buffers, statistics, units, tz, now: datetime.datetime) -> None:
        self.buffers = buffers or {}
        self.statistics = statistics or {}
        self.units = units or {}
        self.tz = tz
        self.now = now
        self._keys: dict[tuple[str, str], list[float]] = {}
        self._cache: dict[tuple, dict] = {}

    # ------------------------------------------------------------------ #

    def _ts(self, naive: datetime.datetime) -> float:
        return naive.replace(tzinfo=self.tz).timestamp()

    def _dt(self, ts: float) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(ts, self.tz).replace(tzinfo=None)

    def _row_keys(self, kind: str, entity_id: str, rows: list) -> list[float]:
        keys = self._keys.get((kind, entity_id))
        if keys is None:
            keys = self._keys[(kind, entity_id)] = [row[0] for row in rows]
        return keys

    def _interpolate(self, sensor_data: list[tuple[float, object]], start_ts: float, end_ts: float) -> dict:
        """history.interpolate_data on (ts, value) rows sorted by ts."""
        result: dict = {}
        try:
            numeric_x: list[float] = []
            numeric_y: list[float] = []
            non_numeric: list[tuple[float, object]] = []
            for ts, value in sensor_data:
                try:
                    numeric_y.append(float(value))
                    numeric_x.append(ts)
                except ValueError:
                    non_numeric.append((ts, value))
            if numeric_x:
                points = linspace(start_ts, end_ts, NUM_POINTS)
                for ts, value in zip(points, interp(points, numeric_x, numeric_y)):
                    result[self._dt(ts)] = value
            for ts, value in non_numeric:
                result[self._dt(ts)] = value
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in interpolate_data: %s", err)
            return {}
        return result

    def _history_data(self, entity_id: str, from_dt: datetime.datetime, to_dt: datetime.datetime) -> dict:
        """history.fetch_history_data from the buffer (start state + states inside)."""
        if not entity_id:
            return {}
        cache_key = ("history", entity_id, from_dt, to_dt)
        if cache_key in self._cache:
            return dict(self._cache[cache_key])
        rows = self.buffers.get(entity_id)
        if not rows:
            return {}
        start_ts, end_ts = self._ts(from_dt), self._ts(to_dt)
        keys = self._row_keys("short", entity_id, rows)
        lo = bisect.bisect_right(keys, start_ts)
        hi = bisect.bisect_left(keys, end_ts)
        data: list[tuple[float, object]] = []
        if lo > 0:
            data.append((start_ts, rows[lo - 1][1]))
        data.extend(row for row in rows[lo:hi] if row[0] > start_ts)
        result = self._interpolate(data, start_ts, end_ts) if data else {}
        self._cache[cache_key] = dict(result)
        return result

    def _statistics_data(self, entity_id: str, from_dt: datetime.datetime, to_dt: datetime.datetime) -> dict:
        """history.fetch_statistics_data (hourly mean)."""
        start = from_dt.replace(minute=0, second=0, microsecond=0)
        end = to_dt.replace(minute=0, second=0, microsecond=0)
        if start == end:
            end = start + datetime.timedelta(hours=1)
        cache_key = ("statistics", entity_id, start, end)
        if cache_key in self._cache:
            return dict(self._cache[cache_key])
        rows = self.statistics.get(entity_id)
        result: dict = {}
        if rows:
            start_ts, end_ts = self._ts(start), self._ts(end)
            keys = self._row_keys("long", entity_id, rows)
            selected = rows[bisect.bisect_left(keys, start_ts):bisect.bisect_left(keys, end_ts)]
            if selected:
                if any(value is None for _, value in selected):
                    _LOGGER.error("Error in fetch_statistics_data for %s: mean is None", entity_id)
                else:
                    result = self._interpolate(selected, start_ts, end_ts)
        self._cache[cache_key] = dict(result)
        return result

    # ------------------------------------------------------------------ #
    # history.py API
    # ------------------------------------------------------------------ #

    def get_values(self, entity_id, from_datetime, to_datetime, float_type=False, convert_to=None, include_timestamps=False, error_state=None):
        from_datetime, to_datetime = min(from_datetime, to_datetime), max(from_datetime, to_datetime)
        states = {}
        try:
            unit = self.units.get(entity_id)
            for ts, value in self._history_data(entity_id, from_datetime, to_datetime).items():
                if value in ENTITY_UNAVAILABLE_STATES:
                    continue
                if float_type is True:
                    try:
                        value = float(value)
                    except Exception:  # noqa: BLE001
                        continue
                states[ts] = value if convert_to is None else power_convert(value, unit, convert_to)
            if states:
                return states if include_timestamps else list(states.values())
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in get_values for %s between %s and %s: %s", entity_id, from_datetime, to_datetime, err)
        return error_state

    def get_longterm_values(self, entity_id, from_datetime, to_datetime, convert_to=None, error_state=None):
        from_datetime, to_datetime = min(from_datetime, to_datetime), max(from_datetime, to_datetime)
        unit = self.units.get(entity_id)
        states = [
            value if convert_to is None else power_convert(value, unit, convert_to)
            for value in self._statistics_data(entity_id, from_datetime, to_datetime).values()
            if value not in ENTITY_UNAVAILABLE_STATES
        ]
        return states or error_state

    def get_average_value(self, entity_id, from_datetime, to_datetime, convert_to=None, error_state=None):
        if not entity_id:
            return error_state
        states = self.get_values(entity_id, from_datetime, to_datetime, float_type=True, convert_to=convert_to)
        longterm = self.get_longterm_values(entity_id, from_datetime, to_datetime, convert_to=convert_to)
        if states is not None and longterm is not None:
            states.extend(longterm)
        elif states is None and longterm is not None:
            states = longterm
        try:
            if states:
                return sum(states) / len(states)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in get_average_value for %s: %s", entity_id, err)
        return error_state

    def get_last_value(self, entity_id, float_type=False, convert_to=None, error_state="unknown"):
        """Last value within the last day (interpolated like the original)."""
        to_datetime = self.now
        states = self.get_values(entity_id, to_datetime - datetime.timedelta(days=1), to_datetime, float_type=float_type, convert_to=convert_to)
        if states and isinstance(states, list):
            return states[-1]
        _LOGGER.warning("No data found for %s the last day, returning %s", entity_id, error_state)
        return error_state

    def last_numeric(self, entity_id):
        for _, value in reversed(self.buffers.get(entity_id) or []):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None


def merge_rows(existing: list, new_rows) -> list:
    """Merge rows (ts, value) keeping sort order and dropping duplicate timestamps."""
    merged = dict(existing)
    for ts, value in new_rows:
        merged[ts] = value
    return sorted(merged.items())
