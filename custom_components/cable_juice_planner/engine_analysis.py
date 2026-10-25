"""Analysis (engine mixin): driving against the outdoor temperature, ready at departure and
planned against actually charged.

- Drives: from unplugging to plugging in again (odometer and battery level), from the
  recorder's raw history the first time and recorded from then on. The outdoor temperature
  is the mean of the hourly statistics while the car was away. km/kWh uses the battery
  size like the rest of CJP.
- Departures: at every work plan departure the battery level is saved with the plan's
  target before work and the requirement (the day's driving + the daily minimum). Only from
  now on: the plans of the past are not kept.
- Planned per day: every evening the kWh planned for the next day; compared with what was
  charged that day per kind of charging (CJP's charging history).
- Price forecast: every hour the buy price forecast of the price integration per hour and
  days ahead (without CJP's daily adder) and the real prices (price_hours, kept a week).
  When all real prices of a day are known, the forecast error per days ahead and how well it
  found the day's cheapest hours (price_days). The bias of the last 30 days corrects the
  forecast prices the plan uses (switch price_forecast_correction).
"""
from __future__ import annotations

import datetime
import logging

from .const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from .helpers import days_between, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)

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
PRICE_CORRECTION_MAX = 0.5  # kr/kWh the forecast is corrected by at most


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
    dear_miss: kr/kWh lost on average by picking those hours instead of the real ones.
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


class AnalysisMixin:
    """Database analysis and the analysis sensor."""

    def load_analysis_db(self, database) -> None:
        self.analysis_db = database if isinstance(database, dict) and isinstance(database.get("drives"), list) else {"drives": [], "departures": [], "planned_days": {}}
        self.analysis_db.setdefault("departures", [])
        self.analysis_db.setdefault("planned_days", {})
        self.analysis_db.setdefault("price_hours", {})
        self.analysis_db.setdefault("price_days", {})

    def _analysis_ts(self, moment: datetime.datetime | None = None) -> float:
        moment = moment or self.now()
        return moment.replace(tzinfo=self._snap.tz).timestamp() if moment.tzinfo is None else moment.timestamp()

    def _analysis_local(self, ts: float) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(ts, self._snap.tz).replace(tzinfo=None)

    # drives ------------------------------------------------------------ #

    def analysis_unplugged(self) -> None:
        """drive_efficiency_save_car_stats: the car leaves, a drive may start."""
        self.analysis_db["drive_start"] = self._analysis_ts()
        self.mark_dirty("analysis")

    def analysis_drive(self, km: float, used: float) -> None:
        """drive_efficiency: a drive was accepted when plugging in."""
        start = self.analysis_db.pop("drive_start", None)
        if start is None or km < ANALYSIS_MIN_KM or used < ANALYSIS_MIN_PERCENT:
            return
        self._add_drives([{"start": start, "end": self._analysis_ts(), "km": round(km, 1), "used": round(used, 1), "temp": None}])

    def _add_drives(self, drives: list[dict]) -> None:
        known = self.analysis_db["drives"]
        for drive in drives:
            if not any(d["start"] < drive["end"] and drive["start"] < d["end"] for d in known):
                known.append(drive)
        known.sort(key=lambda d: d["start"])
        del known[:-ANALYSIS_DRIVES]
        self.mark_dirty("analysis")

    # departures -------------------------------------------------------- #

    def analysis_check_departure(self) -> None:
        """Every cron: remember the plan's target before today's departure, save it after."""
        plan = getattr(self, "charging_plan", {}) or {}
        today = plan.get(0) if isinstance(plan, dict) else None
        now = self.now()
        pending = self.analysis_db.get("pending_departure")
        if today and today.get("workday") and today.get("work_goto") and now < today["work_goto"]:
            self.analysis_db["pending_departure"] = {
                "date": now.date().isoformat(), "departure": self._analysis_ts(today["work_goto"]),
                "target": round(float(today.get("battery_level_before_work_sum", 0.0)), 1),
                "required": round(float(today.get("work_battery_level_needed", 0.0)) + self.get_min_daily_battery_level(), 1),
            }
            return
        if not pending or self._analysis_ts() < pending["departure"]:
            return
        # the level the car left with: saved when unplugged shortly before, else the level now
        level = self.battery_level()
        start = self.analysis_db.get("drive_start")
        last_level = self.sensor_states.get("drive_efficiency_last_battery_level")
        if start and pending["departure"] - 3 * 3600 <= start <= pending["departure"] + 900 and isinstance(last_level, (int, float)):
            level = float(last_level)
        self.analysis_db["departures"].append({**pending, "level": round(level, 1), "met": level >= pending["required"] - 0.5, "target_met": level >= pending["target"] - 1.0})
        self.analysis_db["departures"] = self.analysis_db["departures"][-ANALYSIS_DAYS:]
        self.analysis_db.pop("pending_departure", None)
        self.mark_dirty("analysis")
        self.publish_analysis()

    # planned per day --------------------------------------------------- #

    def analysis_record_planned(self) -> None:
        """hour_end at 23: the kWh planned for tomorrow."""
        if self.now().hour != 23:
            return
        tomorrow = (self.now() + datetime.timedelta(days=1)).date()
        hours = getattr(self, "charge_hours", {}) or {}
        kwh = sum(v.get("kWh", 0.0) for k, v in hours.items() if isinstance(k, datetime.datetime) and k.date() == tomorrow and isinstance(v, dict))
        days = self.analysis_db["planned_days"]
        days[tomorrow.isoformat()] = round(kwh, 2)
        for key in sorted(days)[:-ANALYSIS_DAYS]:
            del days[key]
        self.mark_dirty("analysis")

    # price forecast ---------------------------------------------------- #

    def analysis_record_prices(self) -> None:
        """hour_end: the buy price forecast per days ahead, the real prices and the days they are all known for."""
        try:
            self._analysis_record_prices()
            self.publish_analysis()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recording the price forecast failed")

    def _analysis_record_prices(self) -> None:
        provider = self.price_provider
        if provider is None:
            return
        current_hour = reset_time_to_hour(self.now())
        hours = self.analysis_db["price_hours"]
        prices = provider.get_buy_prices()
        sources = provider.buy_price_sources
        forecast = {ts: price for ts, price in prices.items() if str(sources.get(ts, "")).endswith(":forecast")}
        for hour, price in hourly_prices(forecast).items():
            lead = days_between(current_hour, hour)
            if 1 <= lead <= PRICE_FORECAST_DAYS:
                # the providers add lead / divider to forecasts to prefer known prices
                hours.setdefault(hour.strftime("%Y-%m-%dT%H"), {})[str(lead)] = round(price - lead / PRICE_ADDER_DAY_BETWEEN_DIVIDER, 3)
        for hour, price in hourly_prices(provider.get_buy_real_prices()).items():
            hours.setdefault(hour.strftime("%Y-%m-%dT%H"), {})["real"] = round(price, 3)

        price_days = self.analysis_db["price_days"]
        by_date: dict[str, list[dict]] = {}
        for key in sorted(hours):
            by_date.setdefault(key[:10], []).append(hours[key])
        for date, rows in by_date.items():
            # a whole day (23 hours on the day summer time starts)
            if date not in price_days and sum(1 for row in rows if _is_num(row.get("real"))) >= 23:
                price_days[date] = evaluate_price_day(rows)
        oldest = (current_hour.date() - datetime.timedelta(days=PRICE_HOURS_DAYS)).isoformat()
        for key in [key for key in hours if key[:10] < oldest]:
            del hours[key]
        oldest = (current_hour.date() - datetime.timedelta(days=ANALYSIS_DAYS)).isoformat()
        for key in [key for key in price_days if key < oldest]:
            del price_days[key]
        self.mark_dirty("analysis")

    def _price_correction_on(self) -> bool:
        return self.switch_state("price_forecast_correction", "on") == "on"

    def price_forecast_corrections(self) -> dict[int, float]:
        """kr/kWh added to the price forecast per days ahead: minus its bias the last 30 days.

        Only for days ahead with PRICE_CORRECTION_MIN_DAYS judged days, at most
        PRICE_CORRECTION_MAX either way, and only when the option is on.
        """
        if not self._price_correction_on():
            return {}
        first = (self.now().date() - datetime.timedelta(days=PRICE_CORRECTION_DAYS)).isoformat()
        selected = [day for date, day in self.analysis_db.get("price_days", {}).items() if date >= first and isinstance(day, dict)]
        corrections = {}
        for lead in range(1, PRICE_FORECAST_DAYS + 1):
            entries = [day[str(lead)] for day in selected if isinstance(day.get(str(lead)), dict)]
            hours = sum(_num(entry.get("n")) for entry in entries)
            if len(entries) >= PRICE_CORRECTION_MIN_DAYS and hours:
                bias = sum(_num(entry.get("bias")) for entry in entries) / hours
                corrections[lead] = round(min(max(-bias, -PRICE_CORRECTION_MAX), PRICE_CORRECTION_MAX), 3)
        return corrections

    def _price_forecast_attributes(self) -> dict:
        today = self.now().date()
        price_days = self.analysis_db["price_days"]

        def price_errors(first: datetime.date) -> list[dict]:
            selected = [day for date, day in price_days.items() if date >= first.isoformat()]
            result = []
            for lead in range(1, PRICE_FORECAST_DAYS + 1):
                entries = [day[str(lead)] for day in selected if isinstance(day.get(str(lead)), dict)]
                hours = sum(_num(entry.get("n")) for entry in entries)
                if not hours:
                    continue
                ranked = len(entries) * PRICE_RANK_HOURS
                result.append({
                    "lead": lead,
                    "days": len(entries),
                    "mae": round(sum(_num(entry.get("abs")) for entry in entries) / hours, 3),
                    "bias": round(sum(_num(entry.get("bias")) for entry in entries) / hours, 3),
                    "max": round(max(_num(entry.get("max")) for entry in entries), 3),
                    "cheap": round(sum(_num(entry.get("cheap")) for entry in entries) / ranked, 3),
                    "cheap_miss": round(sum(_num(entry.get("cheap_miss")) for entry in entries) / len(entries), 3),
                    "dear": round(sum(_num(entry.get("dear")) for entry in entries) / ranked, 3),
                    "dear_miss": round(sum(_num(entry.get("dear_miss")) for entry in entries) / len(entries), 3),
                })
            return result

        corrections = self.price_forecast_corrections()
        last_30 = price_errors(today - datetime.timedelta(days=PRICE_CORRECTION_DAYS))
        for entry in last_30:
            entry["correction"] = corrections.get(entry["lead"])

        def corrected(key: str, row: dict) -> float | None:
            """The newest forecast of a coming hour as the plan uses it (with the correction)."""
            lead = (datetime.date.fromisoformat(key[:10]) - today).days
            if not corrections or _is_num(row.get("real")) or not _is_num(row.get(str(lead))):
                return None
            return round(row[str(lead)] + corrections.get(lead, 0.0), 3)

        chart_first = (today - datetime.timedelta(days=PRICE_CHART_DAYS)).isoformat()
        return {
            "rank_hours": PRICE_RANK_HOURS,
            "correction_on": self._price_correction_on(),
            "correction_min_days": PRICE_CORRECTION_MIN_DAYS,
            "last_7": price_errors(today - datetime.timedelta(days=7)),
            "last_30": last_30,
            "days": [
                {"date": date, **{f"d{lead}": round(day[str(lead)]["abs"] / day[str(lead)]["n"], 3) for lead in PRICE_CHART_LEADS if str(lead) in day}}
                for date, day in sorted(price_days.items())[-31:]
                if day
            ],
            "hours": [
                {"t": key, "real": row.get("real"), "corrected": corrected(key, row), **{f"d{lead}": row.get(str(lead)) for lead in PRICE_CHART_LEADS}}
                for key, row in sorted(self.analysis_db["price_hours"].items())
                if key[:10] >= chart_first
            ],
        }

    # update and publish ------------------------------------------------ #

    def analysis_update(self, data: dict) -> None:
        """data from the coordinator: hourly outdoor temperatures {ts: °C} and (the first time)
        the raw history to find the drives of the last days."""
        if data.get("status") is not None:
            self._add_drives(find_drives(data["status"], data.get("odometer") or [], data.get("soc") or []))
        temps = {float(ts): float(v) for ts, v in (data.get("temps") or {}).items()}
        for drive in self.analysis_db["drives"]:
            if drive.get("temp") is None and temps:
                hour = drive["start"] - drive["start"] % 3600
                values = []
                while hour <= drive["end"]:
                    if hour in temps:
                        values.append(temps[hour])
                    hour += 3600
                if values:
                    drive["temp"] = round(sum(values) / len(values), 1)
                    self.mark_dirty("analysis")
        if data.get("backfill"):
            self.analysis_db["backfilled"] = True
            self.mark_dirty("analysis")
        self.publish_analysis()

    def publish_analysis(self) -> None:
        battery_size = float(self.config["ev_car"]["battery_size"])
        points = []
        for d in self.analysis_db["drives"]:
            if d.get("temp") is None:
                continue
            kwh = d["used"] / 100 * battery_size
            points.append({"time": self._analysis_local(d["start"]).isoformat(timespec="minutes"), "temp": d["temp"], "km": d["km"], "kwh": round(kwh, 2), "km_per_kwh": round(d["km"] / kwh, 2)})

        departures = self.analysis_db["departures"]
        recent = departures[-30:]
        met = round(sum(1 for d in recent if d["met"]) / len(recent), 3) if recent else None

        # planned against actual per day (CJP's charging history)
        actual: dict[str, dict[str, float]] = {}
        for key, value in self.charging_history_db.items():
            if not isinstance(key, datetime.datetime) or not isinstance(value, dict) or "public_charging" in str(value.get("charging_type", "")):
                continue
            kinds = value.get("charging_type") or "other"
            kind = next((k for k in CHARGING_KINDS if k in str(kinds)), "other")
            day = actual.setdefault(key.date().isoformat(), {})
            day[kind] = day.get(kind, 0.0) + float(value.get("kWh") or 0.0)
        days = []
        for date, planned in sorted(self.analysis_db["planned_days"].items()):
            if date >= self.now().date().isoformat():
                continue
            # "plan": the kWh planned the evening before; the kinds: what was charged that day
            row = {"date": date, "plan": planned, **{k: round(actual.get(date, {}).get(k, 0.0), 2) for k in (*CHARGING_KINDS, "other")}}
            row["actual"] = round(sum(row[k] for k in (*CHARGING_KINDS, "other")), 2)
            days.append(row)
        planned_total = sum(d["plan"] for d in days)
        unplanned = sum(d["manual"] + d["low_battery"] + d["other"] for d in days)

        self.set_state("analysis", round(met * 100, 1) if met is not None else None)
        attrs = {
            "drives": points[-200:],
            "temperature_bins": temperature_bins(points),
            "temperature_fit": linear_fit(points),
            "departures": [
                {**{k: v for k, v in d.items() if k != "departure"}, "time": self._analysis_local(d["departure"]).strftime("%H:%M")}
                for d in departures[-14:]
            ],
            "departures_met": met,
            "departures_count": len(recent),
            "planned_days": days[-31:],
            "planned_total": round(planned_total, 1),
            "actual_total": round(sum(d["actual"] for d in days), 1),
            "unplanned_total": round(unplanned, 1),
            "price_forecast": self._price_forecast_attributes(),
        }
        for key, value in attrs.items():
            self.set_attr("analysis", key, value)

    def analysis_backfilled(self) -> bool:
        return bool(self.analysis_db.get("backfilled"))
