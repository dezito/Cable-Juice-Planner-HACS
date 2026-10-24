"""Charging economy per month (engine mixin): what the charging cost compared with other ways.

A connection window runs from plugging in to unplugging at home. The energy charged in it
(CJP's charging history, solar and home battery valued like in the plan) is compared with
the same kWh charged

- at the window's average price,
- like most smart chargers ("daily charging", alternate_charging_planner): in the cheapest
  hours until the next departure, without moving it to a cheaper night or using solar,
- at the lowest possible price: the cheapest hours of the whole window,

always at the max charging power and with the hourly prices from the price sensor's
long-term statistics. The score is how close the achieved price came to the lowest,
measured from the average: (average - achieved) / (average - lowest), 100 % = the cheapest
hours. (Against daily charging it is often undefined: a window from the evening to the
departure gives daily = lowest.)

Per month also the energy per source (grid, solar, home battery, public) and the price per
km (all charging incl. public divided by the odometer's distance).
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

from .const import WEEKDAYS

_LOGGER = logging.getLogger(__name__)

ECONOMY_MONTHS = 13
ECONOMY_DB_VERSION = 2
ECONOMY_PRICE_DAYS = 400  # hourly prices kept for the comparisons
ECONOMY_WINDOW_SETTLE = 2 * 3600  # s after unplugging before a window is calculated (the last hour is recalculated)


def fill_cheapest(prices: dict[float, float], start: float, end: float, kwh: float, kw: float) -> float | None:
    """Cost of kwh charged at kw in the cheapest hours of [start, end) (hour start -> price).

    More energy than fits is priced at the average of the hours. None without prices.
    """
    slots = []
    hour = start - start % 3600
    while hour < end:
        overlap = (min(hour + 3600, end) - max(hour, start)) / 3600
        if overlap > 0 and hour in prices:
            slots.append((prices[hour], kw * overlap))
        hour += 3600
    if not slots:
        return None
    cost, left = 0.0, kwh
    for price, capacity in sorted(slots):
        used = min(capacity, left)
        cost += used * price
        left -= used
        if left <= 1e-9:
            break
    if left > 1e-9:
        cost += left * sum(p for p, _ in slots) / len(slots)
    return cost


def average_price(prices: dict[float, float], start: float, end: float) -> float | None:
    hour = start - start % 3600
    values = []
    while hour < end:
        if hour in prices:
            values.append(prices[hour])
        hour += 3600
    return sum(values) / len(values) if values else None


class EconomyMixin:
    """Connection windows (database economy) and the monthly economy sensor."""

    def load_economy_db(self, database) -> None:
        self.economy_db = database if isinstance(database, dict) and isinstance(database.get("windows"), list) else {"windows": []}
        if self.economy_db.get("version") != ECONOMY_DB_VERSION:
            # version 2: daily charging no longer counts hours without a price as free: calculate the windows again
            for window in self.economy_db["windows"]:
                for key in ("kwh", "achieved", "average", "daily", "lowest"):
                    window.pop(key, None)
            self.economy_db["version"] = ECONOMY_DB_VERSION

    # connection windows ------------------------------------------------ #

    def economy_plugged_in(self) -> None:
        """charger_status_changed: plugged in at home."""
        windows = self.economy_db["windows"]
        if windows and windows[-1].get("end") is None:
            return
        windows.append({"start": self._economy_ts(self.now()), "end": None})
        self.mark_dirty("economy")

    def economy_unplugged(self) -> None:
        windows = self.economy_db["windows"]
        if windows and windows[-1].get("end") is None:
            windows[-1]["end"] = self._economy_ts(self.now())
            self.mark_dirty("economy")

    def _economy_ts(self, moment: datetime.datetime) -> float:
        return moment.replace(tzinfo=self._snap.tz).timestamp() if moment.tzinfo is None else moment.timestamp()

    def _economy_local(self, ts: float) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(ts, self._snap.tz).replace(tzinfo=None)

    def _next_departure(self, start: float, end: float) -> float:
        """The first workday departure (work plan) after start, else the end of the window."""
        if not self.workplan_charging_enabled():
            return end
        moment = self._economy_local(start)
        for day in range(8):
            date = (moment + datetime.timedelta(days=day)).date()
            day_text = WEEKDAYS[date.weekday()]
            departure_time = self.setting(f"workday_departure_{day_text}")
            if self.switch_state(f"workday_{day_text}") != "on" or not isinstance(departure_time, datetime.time):
                continue
            departure = self._economy_ts(datetime.datetime.combine(date, departure_time))
            if departure > start:
                return min(departure, end)
        return end

    def economy_mark_backfilled(self) -> None:
        """The connection windows of the recorder's history were read once."""
        self.economy_db["backfilled"] = True
        self.mark_dirty("economy")

    def economy_store_prices(self) -> None:
        """hour_end: keep the plan's grid prices of the hours that have passed (incl. fees, the
        prices the achieved cost was charged at), so the comparison does not depend on the
        recorder's history of the price sensor."""
        stored = self.economy_db.setdefault("prices", {})
        now = self.now()
        for moment, price in (self.get_grid_prices() or {}).items():
            if isinstance(moment, datetime.datetime) and moment <= now and isinstance(price, (int, float)):
                stored[self._economy_ts(moment.replace(minute=0, second=0, microsecond=0))] = round(float(price), 4)
        oldest = self._economy_ts(now) - ECONOMY_PRICE_DAYS * 86400
        for ts in [ts for ts in stored if ts < oldest]:
            del stored[ts]
        self.mark_dirty("economy")

    # calculation ------------------------------------------------------- #

    def economy_update(self, data: dict) -> None:
        """data from the coordinator: hourly prices {ts: kr/kWh}, odometer km per month {"YYYY-MM": km}
        and (the first time) connection windows from the recorder [(start, end)]."""
        windows = self.economy_db["windows"]
        for start, end in data.get("windows") or []:
            if not any(w["start"] < end and start < (w["end"] or start + 1) for w in windows):
                windows.append({"start": start, "end": end})
        windows.sort(key=lambda w: w["start"])
        # the price sensor's history (statistics or raw), the plan's own prices win
        prices = {float(ts): float(price) for ts, price in (data.get("prices") or {}).items()}
        prices.update({float(ts): float(price) for ts, price in self.economy_db.get("prices", {}).items()})
        kw = int(self.config["charger"]["charging_phases"]) * float(self.config["charger"]["charging_max_amp"]) * float(self.config["charger"]["power_voltage"]) / 1000
        now = self._economy_ts(self.now())
        sessions = self._economy_sessions()

        for window in windows:
            if "achieved" in window or window["end"] is None or now - window["end"] < ECONOMY_WINDOW_SETTLE:
                continue
            inside = [s for s in sessions if window["start"] - 600 <= s["ts"] <= window["end"] and not s["public"]]
            kwh = sum(s["kwh"] for s in inside)
            if kwh <= 0.0:
                window["achieved"] = 0.0
                window["kwh"] = 0.0
                continue
            lowest = fill_cheapest(prices, window["start"], window["end"], kwh, kw)
            average = average_price(prices, window["start"], window["end"])
            if lowest is None or average is None:
                continue  # prices not (yet) in the statistics
            departure = self._next_departure(window["start"], window["end"])
            daily_first = min(kwh, kw * max(departure - window["start"], 0) / 3600)
            daily_rest = kwh - daily_first

            def part(start, end, part_kwh):
                # a part without hourly prices at the window's average (never for free)
                if part_kwh <= 1e-9:
                    return 0.0
                cost = fill_cheapest(prices, start, end, part_kwh, kw)
                return cost if cost is not None else part_kwh * average

            # the lowest price is a subset of the same hours: daily can never be cheaper
            daily = max(part(window["start"], departure, daily_first) + part(departure, window["end"], daily_rest), lowest)
            window.update(
                kwh=round(kwh, 3), achieved=round(sum(s["cost"] for s in inside), 3),
                average=round(average * kwh, 3), daily=round(daily, 3), lowest=round(lowest, 3),
            )
        # windows older than the charging history kept are of no use
        oldest = min((s["ts"] for s in sessions), default=now) - 86400
        self.economy_db["windows"] = [w for w in windows if (w["end"] or now) >= oldest]
        self.mark_dirty("economy")
        self.publish_economy(data.get("odometer_months") or {})

    def _economy_sessions(self) -> list[dict]:
        sessions = []
        for key, value in self.charging_history_db.items():
            if not isinstance(key, datetime.datetime) or not isinstance(value, dict):
                continue
            public = "public_charging" in str(value.get("charging_type", "")) or bool(value.get("kWh_from_public"))
            kwh = float(value.get("kWh") or 0.0)
            local = float(value.get("kWh_from_local_energy") or 0.0)
            sessions.append({
                "ts": self._economy_ts(key), "month": key.strftime("%Y-%m"), "public": public,
                "kwh": kwh, "cost": float(value.get("cost") or 0.0),
                "grid": 0.0 if public else max(float(value.get("kWh_from_grid", kwh - local) or 0.0), 0.0),
                "solar": float(value.get("solar_kwh_of_local_energy") or 0.0),
                "powerwall": float(value.get("powerwall_kwh_of_local_energy") or 0.0),
                "public_kwh": float(value.get("kWh_from_public") or (kwh if public else 0.0)),
            })
        return sessions

    def publish_economy(self, odometer_months: dict[str, float] | None = None) -> None:
        if odometer_months is not None:
            self.economy_db["odometer_months"] = {**self.economy_db.get("odometer_months", {}), **odometer_months}
        odometer_months = self.economy_db.get("odometer_months", {})
        months: dict[str, dict[str, Any]] = {}

        def month(key: str) -> dict:
            return months.setdefault(key, {
                "month": key, "kwh": 0.0, "cost": 0.0, "grid": 0.0, "solar": 0.0, "powerwall": 0.0, "public": 0.0, "public_cost": 0.0,
                "compared_kwh": 0.0, "achieved": 0.0, "average": 0.0, "daily": 0.0, "lowest": 0.0, "windows": 0,
            })

        for s in self._economy_sessions():
            m = month(s["month"])
            m["kwh"] += s["kwh"]
            m["cost"] += s["cost"]
            m["grid"] += s["grid"]
            m["solar"] += s["solar"]
            m["powerwall"] += s["powerwall"]
            m["public"] += s["public_kwh"]
            if s["public"]:
                m["public_cost"] += s["cost"]
        for window in self.economy_db["windows"]:
            if not window.get("kwh") or "lowest" not in window:
                continue
            m = month(self._economy_local(window["start"]).strftime("%Y-%m"))
            m["compared_kwh"] += window["kwh"]
            m["windows"] += 1
            for key in ("achieved", "average", "daily", "lowest"):
                m[key] += window[key]

        result = []
        for key in sorted(months)[-ECONOMY_MONTHS:]:
            m = months[key]
            km = odometer_months.get(key)
            compared = m["compared_kwh"]
            row = {k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()}
            row["km"] = round(km, 1) if km else None
            row["price_per_km"] = round(m["cost"] / km, 3) if km else None
            row["unit_price"] = round(m["cost"] / m["kwh"], 3) if m["kwh"] else None
            for ref in ("achieved", "average", "daily", "lowest"):
                row[f"{ref}_unit"] = round(m[ref] / compared, 3) if compared else None
            row["saving_daily"] = round(m["daily"] - m["achieved"], 2) if compared else None
            possible = m["average"] - m["lowest"]
            row["score"] = round(min(max((m["average"] - m["achieved"]) / possible, -1.0), 1.0), 3) if compared and possible > 0.01 else None
            result.append(row)

        current = result[-1] if result else {}
        self.set_state("economy", current.get("saving_daily"))
        self.set_attr("economy", "months", result)
        totals = {key: round(sum(r[key] or 0.0 for r in result), 2) for key in ("kwh", "cost", "achieved", "daily", "lowest", "average", "compared_kwh")}
        totals["saving_daily"] = round(totals["daily"] - totals["achieved"], 2)
        possible = totals["average"] - totals["lowest"]
        totals["score"] = round(min(max((totals["average"] - totals["achieved"]) / possible, -1.0), 1.0), 3) if possible > 0.01 else None
        self.set_attr("economy", "total", totals)
        self.set_attr("economy", "open_window", bool(self.economy_db["windows"] and self.economy_db["windows"][-1].get("end") is None))
