"""Analysis (engine part): driving against the outdoor temperature, ready at departure and
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
- Forecast accuracy: before an hour's house consumption / a drive's km/kWh is added to its
  history, what each forecast type (EMA, trend, average) predicted for it from that history.
  Per day the hours, the measured kWh and per type the sum of |error| and error
  (consumption_days); per drive the measured km/kWh and the forecasts (efficiency_drives).
"""
from __future__ import annotations

from .forecast import AnalysisForecast
from .helpers import find_drives
from .publish import AnalysisPublish
from .storage import AnalysisStorage


class Analysis(AnalysisStorage, AnalysisForecast, AnalysisPublish):
    """Database analysis and the analysis sensor."""

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
