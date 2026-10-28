"""Loading and recording of drives, plans and prices."""
from __future__ import annotations

import datetime
import logging

from ...const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ...utils.helpers import days_between, reset_time_to_hour
from .helpers import ANALYSIS_DAYS, ANALYSIS_DRIVES, ANALYSIS_MIN_KM, ANALYSIS_MIN_PERCENT, PRICE_FORECAST_DAYS, PRICE_HOURS_DAYS, _is_num, evaluate_price_day, hourly_prices

_LOGGER = logging.getLogger(__name__)


class AnalysisStorage:
    """Loading and recording of drives, plans and prices."""

    def load_analysis_db(self, database) -> None:
        self.analysis_db = database if isinstance(database, dict) and isinstance(database.get("drives"), list) else {"drives": [], "departures": [], "planned_days": {}}
        self.analysis_db.setdefault("departures", [])
        self.analysis_db.setdefault("planned_days", {})
        self.analysis_db.setdefault("price_hours", {})
        self.analysis_db.setdefault("price_days", {})
        self.analysis_db.setdefault("consumption_days", {})
        self.analysis_db.setdefault("efficiency_drives", [])

    def _analysis_ts(self, moment: datetime.datetime | None = None) -> float:
        moment = moment or self.now()
        return moment.replace(tzinfo=self._snap.tz).timestamp() if moment.tzinfo is None else moment.timestamp()

    def _analysis_local(self, ts: float) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(ts, self._snap.tz).replace(tzinfo=None)

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

    def analysis_backfilled(self) -> bool:
        return bool(self.analysis_db.get("backfilled"))
