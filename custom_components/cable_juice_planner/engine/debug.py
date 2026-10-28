"""Debug information of the engine."""
from __future__ import annotations

import datetime
from pprint import pformat

from ..utils.codec import encode


class Diagnostics:
    """Debug information of the engine."""

    @classmethod
    def _plain(cls, value):
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, datetime.timedelta):
            return str(value)
        if isinstance(value, dict):
            return {cls._plain(k): cls._plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._plain(v) for v in value]
        return value

    def debug_info(self, extra: dict | None = None) -> str:
        extra = extra or {}
        sections = {
            "System Status": (
                {"PREHEATING": self.preheating, "TESTING": self.testing, "PUBLIC_CHARGING_SESSION": self.public_charging_sessions, **extra.get("status", {})},
                None,
            ),
            "System Configuration": (
                {
                    "CHARGER_CONFIGURED": self.charger_configured,
                    "SOLAR_CONFIGURED": self.solar_configured,
                    "POWERWALL_CONFIGURED": self.powerwall_configured,
                    "EV_CONFIGURED": self.ev_configured,
                    "FORECAST_TYPE": self.forecast_type,
                },
                {"CONFIG": self.config},
            ),
            "Worker": (extra.get("worker", {}), None),
            "Database": (
                None,
                {
                    "POWER_VALUES_DB": self.power_values_db,
                    "SOLAR_PRODUCTION_AVAILABLE_DB": self.solar_available_db,
                    "KWH_AVG_PRICES_DB": self.kwh_avg_prices_db,
                    "DRIVE_EFFICIENCY_DB": self.drive_efficiency_db,
                    "KM_KWH_EFFICIENCY_DB": self.km_kwh_efficiency_db,
                },
            ),
            "Charging Plan & Price Logic": (
                {"CURRENT_CHARGING_AMPS": self.current_charging_amps, "CURRENT_CIRCUIT_AMPS": self.current_circuit_amps, "CHARGER_ACTION": self.charger_action},
                {
                    "CHARGING_PLAN": self.charging_plan,
                    "CHARGE_HOURS": self.charge_hours,
                    "LAST_SUCCESSFUL_GRID_PRICES": self.grid_price_cache,
                    "LAST_SUCCESSFUL_SELL_PRICES": self.sell_price_cache,
                },
            ),
            "Charging Expenses": (None, {"BATTERY_LEVEL_EXPENSES": self.battery_level_expenses}),
            "Local Energy Forecast": (
                None,
                {
                    "LOCAL_ENERGY_PRICES": self.local_energy_prices,
                    "LOCAL_ENERGY_PREDICTION_DB": self.local_energy_prediction_db,
                    "MAX_LOCAL_ENERGY_AVAILABLE_HISTORY": self.max_local_energy_available_history,
                },
            ),
            "Charging Loss Metrics": (
                {
                    "CHARGING_LOSS_CAR_BEGIN_KWH": self.charging_loss_car_begin_kwh,
                    "CHARGING_LOSS_CAR_BEGIN_BATTERY_LEVEL": self.charging_loss_car_begin_battery_level,
                    "CHARGING_LOSS_CHARGER_BEGIN_KWH": self.charging_loss_charger_begin_kwh,
                    "CHARGING_LOSS_CHARGING_COMPLETED": self.charging_loss_charging_completed,
                },
                None,
            ),
            "Charging Sessions & History": (
                {
                    "LAST_WAKE_UP_DATETIME": self.last_wake_up_datetime,
                    "LAST_TRIP_CHANGE_DATETIME": self.last_trip_change_datetime,
                    "INTEGRATION_OFFLINE_TIMESTAMP": self.integration_offline_since,
                    "CHARGING_HISTORY_ENDING_BYTE_SIZE": self.charging_history_ending_byte_size,
                },
                {"CURRENT_CHARGING_SESSION": self.current_charging_session, "CHARGING_HISTORY_QUEUE_LAST_RESULT": self.charging_history_last_result},
            ),
            "Runtime Counters & Errors": (
                {
                    "CHARGING_IS_BEGINNING": self.charging_is_beginning,
                    "CHARGING_NO_RULE_COUNT": self.charging_no_rule_count,
                    "ERROR_COUNT": self.error_count,
                    "RESTARTING_CHARGER": self.restarting_charger,
                    "RESTARTING_CHARGER_COUNT": self.restarting_charger_count,
                },
                {"LAST_HASH_RESULTS": self.last_hash_results},
            ),
            "Driving Efficiency": (None, {"LAST_DRIVE_EFFICIENCY_DATA": self.last_drive_efficiency_data}),
        }
        lines = ["<center>\n", f"#### Debug info runtime {self.now()} ####\n", "</center>\n"]
        for section, (table, details) in sections.items():
            lines.append(f"<center>\n\n### {section}\n</center>\n")
            if table:
                lines += ["| Variable | Value |", "|---:|:---|"]
                lines += [f"| {key}: | {self._plain(value)} |" for key, value in table.items()]
            if table and details:
                lines.append("<br>\n")
            for key, value in (details or {}).items():
                lines += ["<details>", f"<summary>{key}: Show dictionary</summary>\n", f"```\n{pformat(self._plain(value))}\n```", "</details>\n"]
                if len(details) > 1:
                    lines.append("<br>\n")
            lines.append("---")
        if self.overview_history:
            lines.append("<center>\n\n### Overview History\n</center>\n")
            for title, overview in sorted(self.overview_history.items(), reverse=True):
                lines += ["<details>", f"<summary>{title}: Show snapshot</summary>\n", *overview, "</details>\n"]
        output = "\n".join(lines)
        self.persistent_notification(output, title=f"{self.title} debug info", notification_id=f"{self.name}_debug_info")
        return output

    def debug_state(self) -> dict:
        """The runtime state for the debug export (management/debug_export.py), encoded like the databases."""
        skip = {"config", "texts", "price_provider"}
        return {key: encode(value) for key, value in vars(self).items() if not key.startswith("_") and key not in skip}
