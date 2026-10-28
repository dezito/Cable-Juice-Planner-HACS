"""Constants and pure functions of the analysis."""
from __future__ import annotations

import datetime

from ...utils.helpers import reset_time_to_hour


ANALYSIS_DRIVES = 400  # drives kept


ANALYSIS_DAYS = 62  # departures and planned days kept


ANALYSIS_TEMPERATURE_BIN = 5.0  # °C


ANALYSIS_MIN_KM = 5.0


ANALYSIS_MIN_PERCENT = 2.0


CHARGING_KINDS = ("planned", "local_energy", "manual", "low_battery")  # the rest counts as "other"


PRICE_FORECAST_DAYS = 7  # days ahead a price forecast is recorded for


PRICE_HOURS_DAYS = 7  # days the hourly forecasts and real prices are kept


PRICE_CHART_DAYS = 2  # past days published for the hourly price chart


PRICE_CHART_LEADS = tuple(range(1, PRICE_FORECAST_DAYS + 1))  # days ahead shown in the charts


PRICE_DAY_MIN_HOURS = 20  # forecast hours needed to judge a day


PRICE_RANK_HOURS = 4  # the cheapest / most expensive hours of a day the forecast should find


PRICE_CORRECTION_DAYS = 30  # days the bias of the price forecast is measured over


PRICE_CORRECTION_MIN_DAYS = 7  # days needed per days ahead before the forecast is corrected


PRICE_CORRECTION_MAX = 0.5  # currency/kWh the forecast is corrected by at most


FORECAST_TYPES = ("ema", "trend", "average")


FORECAST_DRIVES = 30  # drives the km/kWh forecast is judged over (FORECAST_DRIVES_RECENT: the short column)


FORECAST_DRIVES_RECENT = 10


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _num(value) -> float:
    return float(value) if _is_num(value) else 0.0


def _step_value(series: list[tuple[float, float]], ts: float) -> float | None:
    value = None
    for t, v in series:
        if t > ts:
            break
        value = v
    return value


def find_drives(status: list[tuple[float, bool]], odometer: list[tuple[float, float]], soc: list[tuple[float, float]]) -> list[dict]:
    """Drives between unplugging and plugging in again. status: (timestamp, plugged in)."""
    drives = []
    unplugged = None
    for ts, plugged in status:
        if not plugged and unplugged is None:
            unplugged = ts
        elif plugged and unplugged is not None:
            km_a, km_b = _step_value(odometer, unplugged), _step_value(odometer, ts)
            soc_a, soc_b = _step_value(soc, unplugged), _step_value(soc, ts)
            if None not in (km_a, km_b, soc_a, soc_b) and km_b - km_a >= ANALYSIS_MIN_KM and soc_a - soc_b >= ANALYSIS_MIN_PERCENT:
                drives.append({"start": unplugged, "end": ts, "km": round(km_b - km_a, 1), "used": round(soc_a - soc_b, 1), "temp": None})
            unplugged = None
    return drives


def temperature_bins(points: list[dict]) -> list[dict]:
    bins: dict[float, list[dict]] = {}
    for p in points:
        low = (p["temp"] // ANALYSIS_TEMPERATURE_BIN) * ANALYSIS_TEMPERATURE_BIN
        bins.setdefault(low, []).append(p)
    return [
        {"from": low, "to": low + ANALYSIS_TEMPERATURE_BIN, "drives": len(group),
         # weighted by distance: km / kWh over all drives of the bin
         "km_per_kwh": round(sum(p["km"] for p in group) / sum(p["kwh"] for p in group), 2)}
        for low, group in sorted(bins.items())
    ]


def linear_fit(points: list[dict]) -> dict | None:
    """km/kWh = a + b * temperature, weighted by distance."""
    if len(points) < 5 or len({round(p["temp"]) for p in points}) < 3:
        return None
    w = [p["km"] for p in points]
    x = [p["temp"] for p in points]
    y = [p["km_per_kwh"] for p in points]
    sw = sum(w)
    mx = sum(wi * xi for wi, xi in zip(w, x)) / sw
    my = sum(wi * yi for wi, yi in zip(w, y)) / sw
    sxx = sum(wi * (xi - mx) ** 2 for wi, xi in zip(w, x))
    if sxx <= 0:
        return None
    b = sum(wi * (xi - mx) * (yi - my) for wi, xi, yi in zip(w, x, y)) / sxx
    a = my - b * mx
    at = lambda temp: round(a + b * temp, 2)  # noqa: E731
    cold = (1 - (a + b * 0) / (a + b * 20)) if a + b * 20 > 0 else None
    return {"per_degree": round(b, 3), "at_minus_10": at(-10), "at_0": at(0), "at_10": at(10), "at_20": at(20), "cold_cost": round(cold, 3) if cold is not None else None}


def hourly_prices(prices: dict) -> dict[datetime.datetime, float]:
    """Prices per hour (15 minute prices averaged)."""
    sums: dict[datetime.datetime, list[float]] = {}
    for timestamp, price in prices.items():
        if isinstance(timestamp, datetime.datetime) and _is_num(price):
            bucket = sums.setdefault(reset_time_to_hour(timestamp), [0.0, 0.0])
            bucket[0] += price
            bucket[1] += 1
    return {hour: total / count for hour, (total, count) in sums.items()}


def evaluate_price_day(rows: list[dict]) -> dict[str, dict]:
    """The forecast of a day's hours against the real prices, per days ahead.

    rows: per hour {"real": price, "<days ahead>": forecast}. n hours, abs (sum |error|),
    bias (sum error, > 0: forecast too high), max (worst hour); cheap/dear: how many of the
    PRICE_RANK_HOURS cheapest/most expensive hours by the forecast really were, cheap_miss/
    dear_miss: currency/kWh lost on average by picking those hours instead of the real ones.
    """
    result: dict[str, dict] = {}
    rows = [row for row in rows if _is_num(row.get("real"))]
    for lead in range(1, PRICE_FORECAST_DAYS + 1):
        pairs = [(row[str(lead)], row["real"]) for row in rows if _is_num(row.get(str(lead)))]
        if len(pairs) < PRICE_DAY_MIN_HOURS:
            continue
        errors = [forecast - real for forecast, real in pairs]
        count = min(PRICE_RANK_HOURS, len(pairs))

        def ranked(by_forecast: bool, reverse: bool) -> list[int]:
            return sorted(range(len(pairs)), key=lambda i: pairs[i][0 if by_forecast else 1], reverse=reverse)[:count]

        def real_sum(indexes: list[int]) -> float:
            return sum(pairs[i][1] for i in indexes)

        cheap, cheapest = ranked(True, False), ranked(False, False)
        dear, dearest = ranked(True, True), ranked(False, True)
        result[str(lead)] = {
            "n": len(pairs),
            "abs": round(sum(abs(error) for error in errors), 3),
            "bias": round(sum(errors), 3),
            "max": round(max(abs(error) for error in errors), 3),
            "cheap": len(set(cheap) & set(cheapest)),
            "cheap_miss": round((real_sum(cheap) - real_sum(cheapest)) / count, 3),
            "dear": len(set(dear) & set(dearest)),
            "dear_miss": round((real_sum(dearest) - real_sum(dear)) / count, 3),
        }
    return result
