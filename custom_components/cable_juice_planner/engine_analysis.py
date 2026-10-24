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
"""
from __future__ import annotations

import datetime
import logging

_LOGGER = logging.getLogger(__name__)

ANALYSIS_DRIVES = 400  # drives kept
ANALYSIS_DAYS = 62  # departures and planned days kept
ANALYSIS_TEMPERATURE_BIN = 5.0  # °C
ANALYSIS_MIN_KM = 5.0
ANALYSIS_MIN_PERCENT = 2.0
CHARGING_KINDS = ("planned", "local_energy", "manual", "low_battery")  # the rest counts as "other"


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


class AnalysisMixin:
    """Database analysis and the analysis sensor."""

    def load_analysis_db(self, database) -> None:
        self.analysis_db = database if isinstance(database, dict) and isinstance(database.get("drives"), list) else {"drives": [], "departures": [], "planned_days": {}}
        self.analysis_db.setdefault("departures", [])
        self.analysis_db.setdefault("planned_days", {})

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
        }
        for key, value in attrs.items():
            self.set_attr("analysis", key, value)

    def analysis_backfilled(self) -> bool:
        return bool(self.analysis_db.get("backfilled"))
