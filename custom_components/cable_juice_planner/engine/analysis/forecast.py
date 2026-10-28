"""Price, consumption and efficiency forecasts."""
from __future__ import annotations

import datetime
import logging
from .helpers import ANALYSIS_DAYS, FORECAST_DRIVES, FORECAST_DRIVES_RECENT, FORECAST_TYPES, PRICE_CHART_DAYS, PRICE_CHART_LEADS, PRICE_CORRECTION_DAYS, PRICE_CORRECTION_MAX, PRICE_CORRECTION_MIN_DAYS, PRICE_FORECAST_DAYS, PRICE_RANK_HOURS, _is_num, _num

_LOGGER = logging.getLogger(__name__)


class AnalysisForecast:
    """Price, consumption and efficiency forecasts."""

    def _price_correction_on(self) -> bool:
        return self.switch_state("price_forecast_correction", "on") == "on"

    def price_forecast_corrections(self) -> dict[int, float]:
        """Currency/kWh added to the price forecast per days ahead: minus its bias the last 30 days.

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

    def analysis_consumption_forecast(self, history: list, watt: float) -> None:
        """power_values_to_db, before the hour is added: each type's forecast of it (W = Wh in an hour)."""
        try:
            if not history or not _is_num(watt):
                return
            days = self.analysis_db["consumption_days"]
            day = days.setdefault(self.now().date().isoformat(), {"hours": 0, "use": 0.0, **{kind: [0.0, 0.0] for kind in FORECAST_TYPES}})
            day["hours"] += 1
            day["use"] = round(day["use"] + watt / 1000.0, 3)
            for kind in FORECAST_TYPES:
                error = (self.get_forecast_value(history, kind) - watt) / 1000.0
                day[kind] = [round(day[kind][0] + abs(error), 3), round(day[kind][1] + error, 3)]
            for key in sorted(days)[:-ANALYSIS_DAYS]:
                del days[key]
            self.mark_dirty("analysis")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recording the consumption forecast failed")

    def analysis_efficiency_forecast(self, history: list, km_per_kwh: float) -> None:
        """drive_efficiency, before the drive is added: each type's forecast of its km/kWh."""
        try:
            if not history or not _is_num(km_per_kwh):
                return
            drives = self.analysis_db["efficiency_drives"]
            drives.append({"date": self.now().date().isoformat(), "actual": round(km_per_kwh, 2), **{kind: self.get_forecast_value(history, kind) for kind in FORECAST_TYPES}})
            del drives[:-FORECAST_DRIVES]
            self.mark_dirty("analysis")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recording the km/kWh forecast failed")

    def _forecast_attributes(self) -> dict:
        today = self.now().date()
        closed = {date: day for date, day in self.analysis_db["consumption_days"].items() if date < today.isoformat()}

        def consumption_errors(days: int) -> dict | None:
            first = (today - datetime.timedelta(days=days)).isoformat()
            selected = [day for date, day in closed.items() if date >= first]
            hours = sum(_num(day.get("hours")) for day in selected)
            use = sum(_num(day.get("use")) for day in selected)
            if not hours:
                return None
            # the measured kWh per hour, and per type what it forecast on average (measured + bias)
            result = {"hours": int(hours), "actual": round(use / hours, 3)}
            for kind in FORECAST_TYPES:
                absolute = sum(_num((day.get(kind) or [0, 0])[0]) for day in selected)
                bias = sum(_num((day.get(kind) or [0, 0])[1]) for day in selected)
                result[kind] = {
                    "mae": round(absolute / hours, 3),
                    "bias": round(bias / hours, 3),
                    "relative": round(absolute / use, 3) if use > 0 else None,
                    "forecast": round((use + bias) / hours, 3),
                }
            return result

        def drive_errors(count: int) -> dict | None:
            selected = [drive for drive in self.analysis_db["efficiency_drives"][-count:] if _is_num(drive.get("actual"))]
            if not selected:
                return None
            actual = sum(drive["actual"] for drive in selected)
            result = {"drives": len(selected), "actual": round(actual / len(selected), 2)}
            for kind in FORECAST_TYPES:
                errors = [_num(drive.get(kind)) - drive["actual"] for drive in selected]
                result[kind] = {
                    "mae": round(sum(abs(error) for error in errors) / len(errors), 2),
                    "bias": round(sum(errors) / len(errors), 2),
                    "relative": round(sum(abs(error) for error in errors) / actual, 3) if actual > 0 else None,
                    "forecast": round(sum(_num(drive.get(kind)) for drive in selected) / len(selected), 2),
                }
            return result

        def best(errors: dict | None) -> str | None:
            return min(FORECAST_TYPES, key=lambda kind: errors[kind]["mae"]) if errors else None

        consumption_30 = consumption_errors(30)
        drives_30 = drive_errors(FORECAST_DRIVES)
        return {
            "selected": self.get_forecast_type(),
            "consumption": {"last_7": consumption_errors(7), "last_30": consumption_30, "best": best(consumption_30)},
            "drives": {"recent": drive_errors(FORECAST_DRIVES_RECENT), "all": drives_30, "best": best(drives_30)},
        }
