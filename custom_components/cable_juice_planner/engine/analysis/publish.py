"""The analysis published as entity states."""
from __future__ import annotations

import datetime
from .helpers import CHARGING_KINDS, linear_fit, temperature_bins


class AnalysisPublish:
    """The analysis published as entity states."""

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
            "forecast": self._forecast_attributes(),
        }
        for key, value in attrs.items():
            self.set_attr("analysis", key, value)
