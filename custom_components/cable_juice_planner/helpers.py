"""Pure helpers ported from the PyScript modules ``utils`` and ``mytime``.

The behaviour (including the error handling and the odd corner cases) is kept 1:1,
because the planner results depend on it - e.g. ``in_between`` returns False when the
arguments have different types, and the time differences ignore seconds unless
``precise`` is set. Functions that used ``getTime()`` as default take the time as an
argument. All datetimes are naive local time.
"""
from __future__ import annotations

import calendar
import datetime
import logging
import math
from collections.abc import Mapping, Sequence

from dateutil import parser as dateutil_parser

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# mytime
# --------------------------------------------------------------------------- #

def months_between(start, end, precise=False, error_value=0) -> int:
    try:
        if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
            raise TypeError("not datetime")
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
        if not precise:
            start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months = (end.year - start.year) * 12 + (end.month - start.month)
        if end.day < start.day:
            months -= 1
        return months
    except Exception:  # noqa: BLE001
        _LOGGER.error("Error in monthsBetween with start %s (%s) and end %s (%s)", start, type(start), end, type(end))
        return error_value


def days_between(start, end, precise=False, error_value=0) -> int:
    try:
        if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
            raise TypeError("not datetime")
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
        if not precise:
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return (end - start).days
    except Exception:  # noqa: BLE001
        _LOGGER.error("Error in daysBetween with start %s (%s) and end %s (%s)", start, type(start), end, type(end))
        return error_value


def hours_between(start, end, precise=False, error_value=0) -> int:
    try:
        if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
            raise TypeError("not datetime")
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
        if not precise:
            start = start.replace(minute=0, second=0, microsecond=0)
            end = end.replace(minute=0, second=0, microsecond=0)
        return int((end - start).total_seconds() // 3600)
    except Exception:  # noqa: BLE001
        _LOGGER.error("Error in hoursBetween with start %s (%s) and end %s (%s)", start, type(start), end, type(end))
        return error_value


def minutes_between(start, end, precise=False, error_value=0) -> int:
    try:
        if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
            raise TypeError("not datetime")
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
        if not precise:
            start = start.replace(second=0, microsecond=0)
            end = end.replace(second=0, microsecond=0)
        return int((end - start).total_seconds() // 60)
    except Exception:  # noqa: BLE001
        _LOGGER.error("Error in minutesBetween with start %s (%s) and end %s (%s)", start, type(start), end, type(end))
        return error_value


def start_of_day(date: datetime.datetime) -> datetime.datetime:
    return datetime.datetime(date.year, date.month, date.day, 0, 0, 0, 0)


def end_of_day(date: datetime.datetime) -> datetime.datetime:
    return datetime.datetime(date.year, date.month, date.day, 23, 59, 59, 0)


def month_first_day(date: datetime.datetime) -> datetime.datetime:
    return datetime.datetime(year=date.year, month=date.month, day=1)


def day_of_week_text(date: datetime.datetime) -> str:
    """'monday' ... 'sunday' (used as translation key and settings suffix)."""
    return calendar.day_name[date.replace(tzinfo=None).weekday()].lower()


def date_to_string(date, format: str = "%m/%d/%Y %H:%M:%S") -> str:  # noqa: A002 - keyword used by ev.py
    """strftime of a datetime or of every datetime in a list, joined with ', '."""
    if not isinstance(date, (list, tuple)):
        date = [date]
    output = []
    for item in date:
        if not isinstance(item, datetime.datetime):
            raise TypeError(f"Error in date_to_string with date {date} ({type(date)}) is not datetime")
        output.append(item.strftime(format))
    return ", ".join(output)


def to_datetime(date):
    """ISO string -> naive datetime, anything else is returned unchanged."""
    try:
        return dateutil_parser.isoparse(date).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return date


def parse_datetime(value):
    """dateutil.parser.parse like hass_manager.get_state did for input_datetime."""
    try:
        return dateutil_parser.parse(value)
    except Exception:  # noqa: BLE001
        return value


def reset_datetime() -> datetime.datetime:
    """The 'not set' value of the PyScript input_datetime helpers."""
    return datetime.datetime(1900, 1, 1)


def reset_time_to_hour(date: datetime.datetime) -> datetime.datetime:
    if not isinstance(date, datetime.datetime):
        raise TypeError(f"Error in reset_time_to_hour with date {date} ({type(date)}) is not datetime")
    return date.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def add_months(current_date: datetime.datetime, months_to_add: int) -> datetime.datetime:
    return datetime.datetime(
        current_date.year + (current_date.month + months_to_add - 1) // 12,
        (current_date.month + months_to_add - 1) % 12 + 1,
        current_date.day,
        current_date.hour,
        current_date.minute,
        current_date.second,
    )


# --------------------------------------------------------------------------- #
# utils
# --------------------------------------------------------------------------- #

def in_between(check, start, end) -> bool:
    """start <= check < end. Datetimes wrap around midnight; mixed types are False."""
    try:
        types = {type(check), type(start), type(end)}
        if len(types) != 1:
            raise TypeError(f"in_between args must all be same type, got: {types}")
        kind = types.pop()
        if kind is datetime.datetime:
            if start <= end:
                return start <= check < end
            return start <= check or check < end
        if kind in (int, float):
            return start <= check < end
        raise TypeError(f"in_between only supports datetime, int, float (got {kind})")
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Error in in_between(check=%s, start=%s, end=%s): %s", check, start, end, err)
        return False


def round_up(n, decimals=0) -> float:
    n = max(n, 0.0)
    multiplier = 10**decimals
    return float(math.ceil(n * multiplier) / multiplier)


def average(data) -> float:
    if not isinstance(data, list):
        data = [data]
    try:
        return 0.0 if len(data) == 0 else sum(data) / len(data)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("data is not a list: %s %s", data, err)
        return 0.0


def calculate_ema(data, span=5) -> float:
    if not data:
        return 0.0
    try:
        alpha = 2 / (span + 1)
        ema = data[0]
        for value in data:
            ema = (value * alpha) + (ema * (1 - alpha))
        return ema
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Error calculating EMA for data %s with span %s: %s", data, span, err)
        return 0.0


def calculate_trend(data) -> float:
    try:
        n = len(data)
        sum_x = 0
        sum_y = 0
        sum_x_squared = 0
        sum_xy = 0
        for i in range(n):
            sum_x += i
            sum_x_squared += i**2
        for i, j in zip(range(n), data):
            sum_y += j
            sum_xy += i * j
        denominator = n * sum_x_squared - sum_x**2
        if denominator == 0:
            return data[-1]
        slope = (n * sum_xy - sum_x * sum_y) / denominator
        intercept = (sum_y - slope * sum_x) / n
        return intercept + slope * n
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Error calculating trend for data %s: %s", data, err)
        return 0.0


def reverse_list(lst):
    if not isinstance(lst, list):
        _LOGGER.error("Error reversing list %s: Expected a list, got %s", lst, type(lst))
        return lst
    return lst[::-1]


def get_specific_values(values, positive_only=False, negative_only=False) -> list:
    if positive_only is False and negative_only is False:
        raise ValueError("At least one of positive_only or negative_only must be True")
    if positive_only is True and negative_only is True:
        raise ValueError("Only one of positive_only or negative_only can be True")
    try:
        result = []
        for value in values:
            try:
                value = float(value)
            except Exception:  # noqa: BLE001
                continue
            if (negative_only and value > 0.0) or (positive_only and value < 0.0):
                result.append(0.0)
                continue
            result.append(value)
        return [0.0] if result == [] else result
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Error getting specific values from %s: %s", values, err)
        return [0.0]


def get_closest_key(input_value, collection, return_key=False, max_allowed=None):
    """Closest dict key (its value, or the key) / list item. First item wins ties."""

    def find_closest(items, value, limit=None):
        valid = [item for item in items if limit is None or float(item) <= limit]
        if not valid:
            return None
        closest = valid[0]
        closest_diff = abs(float(closest) - value)
        for item in valid:
            diff = abs(float(item) - value)
            if diff < closest_diff:
                closest = item
                closest_diff = diff
        return closest

    try:
        input_value = float(input_value)
        if max_allowed is not None:
            max_allowed = float(max_allowed)
        if isinstance(collection, dict):
            closest_key = find_closest(list(collection.keys()), input_value, max_allowed)
            return closest_key if return_key else collection.get(closest_key)
        if isinstance(collection, (list, range, tuple)):
            return find_closest(collection, input_value, max_allowed)
        raise TypeError("Collection must be a dict or list")
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Can't find closest match for %s in %s - %s", input_value, collection, err)
        return []


def get_dict_value_with_path(d, path: str, default=None):
    current = d
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def set_dict_value_with_path(d: dict, path: str, value) -> dict:
    keys = path.split(".")
    current = d
    for key in keys[:-1]:
        if not isinstance(current.get(key), dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
    return d


def delete_dict_key_with_path(d: dict, path: str) -> dict:
    keys = path.split(".")
    current = d
    for key in keys[:-1]:
        if not isinstance(current, dict) or key not in current:
            return d
        current = current[key]
    if isinstance(current, dict):
        current.pop(keys[-1], None)
    return d


def limit_dict_size(dct: dict, size: int) -> dict:
    """Keep the newest (last inserted) size items."""
    return dict(list(dct.items())[-size:])


def dicts_equal(a, b) -> bool:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(dicts_equal(a[k], b[k]) for k in a)
    if isinstance(a, Sequence) and not isinstance(a, (str, bytes)) and isinstance(b, Sequence) and not isinstance(b, (str, bytes)):
        return len(a) == len(b) and all(dicts_equal(x, y) for x, y in zip(a, b))
    return a == b


def check_next_24_hours_diff(dict1: dict, dict2: dict, now: datetime.datetime) -> bool:
    """True if the datetime keys within the next 24 hours differ."""
    end_time = now + datetime.timedelta(hours=24)
    keys1 = {k for k in dict1 if isinstance(k, datetime.datetime) and now <= k < end_time}
    keys2 = {k for k in dict2 if isinstance(k, datetime.datetime) and now <= k < end_time}
    return keys1 != keys2


def time_window_minutes_left(minute: int, total_minutes: int) -> int:
    return total_minutes - (minute % total_minutes) if (minute % total_minutes) != 0 else total_minutes


def time_window_gaussian_weight(minute, total_minutes, max_value=1.0, sigma_ratio=0.2) -> float:
    try:
        minutes = time_window_minutes_left(minute, total_minutes)
        center = total_minutes / 2
        sigma = total_minutes * sigma_ratio
        exponent = -((minutes - center) ** 2) / (2 * sigma**2)
        return max_value * math.exp(exponent)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Error calculating Gaussian weight: %s", err)
        return 0.0


def float_or_none(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
