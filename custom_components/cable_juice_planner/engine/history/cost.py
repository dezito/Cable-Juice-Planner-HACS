"""The cost of the energy in the battery."""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


class HistoryCost:
    """The cost of the energy in the battery."""

    def current_battery_level_expenses(self) -> dict:
        expenses = {
            "battery_level_expenses_kwh": 0.0,
            "battery_level_expenses_percentage": 0.0,
            "battery_level_expenses_solar_percentage": 0.0,
            "battery_level_expenses_cost": 0.0,
            "battery_level_expenses_unit": None,
        }
        self.battery_level_expenses = expenses
        try:
            current_battery_level = self.battery_level()
            db = self.charging_history_db
            if db:
                for key in sorted(db, reverse=True):
                    if round(expenses["battery_level_expenses_percentage"]) >= round(current_battery_level):
                        break
                    entry = db[key]
                    if "cost" not in entry or "kWh" not in entry or "kWh_from_local_energy" not in entry or "percentage" not in entry:
                        continue
                    cost = entry["cost"]
                    kwh = entry["kWh"]
                    percentage = entry["percentage"]
                    solar_percentage = self.kwh_to_percentage(entry["kWh_from_local_energy"], include_charging_loss=True)
                    new_battery_level = percentage + expenses["battery_level_expenses_percentage"]
                    if new_battery_level > current_battery_level and percentage > 0.0:
                        diff = (percentage - (new_battery_level - current_battery_level)) / percentage
                        cost = cost * diff
                        kwh = kwh * diff
                        percentage = percentage * diff
                        solar_percentage = solar_percentage * diff
                    expenses[key] = {
                        "cost": round(cost, 3),
                        "kWh": round(kwh, 3),
                        "percentage": round(percentage, 1),
                        "solar_percentage": round(solar_percentage, 1),
                        "unit": round(cost / kwh, 3) if kwh > 0.0 else 0.0,
                    }
                    expenses["battery_level_expenses_kwh"] = round(expenses["battery_level_expenses_kwh"] + kwh, 3)
                    expenses["battery_level_expenses_percentage"] = round(expenses["battery_level_expenses_percentage"] + percentage, 1)
                    expenses["battery_level_expenses_solar_percentage"] = round(expenses["battery_level_expenses_solar_percentage"] + solar_percentage, 1)
                    expenses["battery_level_expenses_cost"] = round(expenses["battery_level_expenses_cost"] + cost, 3)
                if expenses["battery_level_expenses_kwh"] > 0.0:
                    expenses["battery_level_expenses_unit"] = round(expenses["battery_level_expenses_cost"] / expenses["battery_level_expenses_kwh"], 3)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Error in battery level cost calculation: %s %s", err, type(err))
        return expenses
