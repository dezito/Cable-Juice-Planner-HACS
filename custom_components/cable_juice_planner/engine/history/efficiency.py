"""Drive efficiency and range."""
from __future__ import annotations

import datetime
import logging

from ...const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    ENTITY_UNAVAILABLE_STATES,
    EV_PLUGGED_STATES,
    EV_UNPLUGGED_STATES,
)
from ...utils.formatting import get_list_values
from ...utils.helpers import (
    average,
    calculate_ema,
    calculate_trend,
    minutes_between,
    reverse_list,
    to_datetime,
)

_LOGGER = logging.getLogger(__name__)


EFFICIENCY_ADJUSTMENT = 1.15


EFFICIENCY_FACTORS = {
    month: factor * EFFICIENCY_ADJUSTMENT
    for month, factor in {1: 0.85, 2: 0.87, 3: 0.90, 4: 0.95, 5: 1.00, 6: 1.02, 7: 1.03, 8: 1.02, 9: 1.00, 10: 0.95, 11: 0.90, 12: 0.87}.items()
}


HEATING_STATES = ("preheat_start", "preheat_cancel")


FULL_CHARGE_BATTERY_LEVEL = 99.0


FULL_CHARGE_99_WAIT_MINUTES = 30


class HistoryEfficiency:
    """Drive efficiency and range."""

    def set_state_drive_efficiency(self) -> None:
        if not self.is_ev_configured():
            return
        key = "drive_efficiency"
        try:
            values = get_list_values(self.drive_efficiency_db)
            if not values:
                _LOGGER.warning("DRIVE_EFFICIENCY_DB is empty or contains no valid float values.")
                return
            self.set_state(key, values[0])
            self.set_attr(key, "ema", float(calculate_ema(reverse_list(values))))
            self.set_attr(key, "average", float(average(values)))
            self.set_attr(key, "trend", float(calculate_trend(reverse_list(values))))
            for item in list(self.own_attr(key, error_state={})):
                if "date" in item:
                    self.del_attr(key, item)
            for i, item in enumerate(self.drive_efficiency_db):
                try:
                    self.set_attr(key, f"date_{item[0]}", round(item[1], 2))
                except Exception:  # noqa: BLE001
                    self.set_attr(key, f"date_{i}", round(item, 2))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant set drive efficiency: %s %s", err, type(err))

    def set_state_km_kwh_efficiency(self) -> None:
        if not self.is_ev_configured():
            return
        key = "km_per_kwh"
        distance_kwh = self.t("ui.common.distance_kwh")
        wh_distance = self.t("ui.common.wh_distance")
        try:
            values = get_list_values(self.km_kwh_efficiency_db)
            self.set_state(key, round(values[0] if values else 3.0, 2))
            if values:
                for name, value in (
                    ("ema", float(calculate_ema(reverse_list(values)))),
                    ("average", float(average(values))),
                    ("trend", float(calculate_trend(reverse_list(values)))),
                ):
                    km_kwh = round(value, 2)
                    self.set_attr(key, name, f"{km_kwh:.2f} {distance_kwh} - {round(1000 / km_kwh, 2):.2f} {wh_distance}")
            for item in list(self.own_attr(key, error_state={})):
                if "date" in item:
                    self.del_attr(key, item)
            for i, item in enumerate(self.km_kwh_efficiency_db):
                try:
                    km_kwh = round(item[1], 2)
                    self.set_attr(key, f"date_{item[0]}", f"{km_kwh:.2f} {distance_kwh} - {round(1000 / km_kwh, 2):.2f} {wh_distance}")
                except (IndexError, TypeError):
                    km_kwh = round(item, 2)
                    self.set_attr(key, f"date_{i}", f"{km_kwh:.2f} {distance_kwh} - {round(1000 / km_kwh, 2):.2f} {wh_distance}")
            self.set_estimated_range()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant set %s efficiency: %s %s", distance_kwh, err, type(err))
            self.persistent_notification(f"Cant set km/kwh efficiency: {err} {type(err)}", f"{self.title} warning", f"{self.name}_set_state_km_kwh_efficiency")

    def set_estimated_range(self) -> None:
        if not self.is_ev_configured():
            return
        try:
            if not self.km_kwh_efficiency_db:
                _LOGGER.warning("KM_KWH_EFFICIENCY_DB is empty or contains no valid float values.")
                return
            range_per_percentage = self.km_kwh_to_km_percentage(float(self.get_forecast_value(self.km_kwh_efficiency_db)))
            if range_per_percentage <= 0:
                _LOGGER.warning("Calculated range_per_percentage is not valid (<= 0). Skipping update.")
                return
            range_total = round(range_per_percentage * 100.0, 2)
            self.set_setting_limit("trip_range_needed", round(range_total + 100.0, 0))
            self.set_state("estimated_range", round(range_per_percentage * self.battery_level(), 2))
            self.set_attr("estimated_range", "total", range_total)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant set estimated range: %s %s", err, type(err))
            self.persistent_notification(f"Cant set estimated range: {err} {type(err)}", f"{self.title} warning", f"{self.name}_set_estimated_range")

    def set_last_drive_efficiency_attributes(self, kilometers, used_kwh, used_battery, cost, efficiency, distance_per_kwh, wh_km) -> None:
        if not self.is_ev_configured():
            return
        distance_type = self.t("ui.common.distance_type")
        for key in ("drive_efficiency", "km_per_kwh", "estimated_range"):
            self.set_attr(key, "last_drive_distance", f"{round(kilometers, 1)}{distance_type}")
            self.set_attr(key, "last_drive_used", f"{round(used_kwh, 2)}kWh ({round(used_battery, 1)}%)")
            self.set_attr(key, "last_drive_cost", f"{round(cost, 2)}{self.t('ui.common.valuta')}")
            self.set_attr(key, "last_drive_efficiency", f"{round(efficiency, 1)}% {round(distance_per_kwh, 2)} {self.t('ui.common.distance_kwh')} ({wh_km} {self.t('ui.common.wh_distance')})")

    def peak_battery_level_after_last_plug(self) -> float:
        max_battery_level = 0.0
        try:
            if not self.is_charger_configured():
                return 0.0
            to_time_stamp = self.now() - datetime.timedelta(minutes=1)
            from_time_stamp = to_time_stamp - datetime.timedelta(days=7)
            status_dict = self.history.get_values(self.config["charger"]["entity_ids"]["status_entity_id"], from_time_stamp, to_time_stamp, include_timestamps=True, error_state={})
            last_connected = None
            for ts in sorted(status_dict.keys(), reverse=True):
                value = status_dict[ts]
                if value in CHARGER_NOT_READY_STATUS:
                    break
                if value in CHARGER_READY_STATUS:
                    last_connected = ts
            if last_connected is None:
                return 0.0
            ev_entity_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"] if self.is_ev_configured() else self.own_entity_id("number", "battery_level")
            max_battery_level = max(self.history.get_values(ev_entity_id, last_connected, to_time_stamp, error_state=[]), default=0.0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("peak_battery_level_after_last_plug(): %s %s", err, type(err))
        return max_battery_level

    def is_battery_fully_charged(self) -> None:
        battery_level = self.battery_level()
        if battery_level < FULL_CHARGE_BATTERY_LEVEL:
            if self.full_charge_99_since is not None:
                self.full_charge_99_since = None
                self.mark_dirty("runtime")
            return

        if battery_level >= 100.0:
            self.set_state("last_full_charge", self.now())
            self.full_charge_99_since = None
            self.mark_dirty("runtime")
            return

        charger_status = self.get_state(
            self.config["charger"]["entity_ids"]["status_entity_id"],
            error_state="unavailable",
        )
        if charger_status in ENTITY_UNAVAILABLE_STATES:
            if self.full_charge_99_since is not None:
                self.full_charge_99_since = None
                self.mark_dirty("runtime")
            return
        if charger_status in CHARGER_CHARGING_STATUS:
            if self.full_charge_99_since is not None:
                self.full_charge_99_since = None
                self.mark_dirty("runtime")
            return

        if self.full_charge_99_since is None:
            self.full_charge_99_since = self.now()
            self.mark_dirty("runtime")
            return

        if minutes_between(self.full_charge_99_since, self.now()) >= FULL_CHARGE_99_WAIT_MINUTES:
            self.set_state("last_full_charge", self.now())
            self.full_charge_99_since = None
            self.mark_dirty("runtime")

    def drive_efficiency_save_car_stats(self, bootup=False) -> None:
        def set_last_odometer():
            odometer_value = float(self.get_state(self.config["ev_car"]["entity_ids"]["odometer_entity_id"], float_type=True, error_state="unknown"))
            self.set_state("drive_efficiency_last_odometer", odometer_value)
            self.mark_dirty("runtime")

        def set_last_battery_level():
            key = "drive_efficiency_last_battery_level"
            self.set_state(key, max(self.battery_level(), self.peak_battery_level_after_last_plug()))
            for item in list(self.own_attr(key, error_state={})):
                if item != "description":
                    self.del_attr(key, item)
            self.mark_dirty("runtime")
            expenses = self.battery_level_expenses
            names = ("battery_level_expenses_cost", "battery_level_expenses_kwh", "battery_level_expenses_percentage", "battery_level_expenses_solar_percentage")
            if not all(name in expenses for name in names):
                return
            for name in (*names, "battery_level_expenses_unit"):
                self.set_attr(key, name, expenses[name])

        def set_last_drive_efficiency_data():
            if self.last_drive_efficiency_data:
                return
            key = "drive_efficiency_last_battery_level"
            if "last_drive_efficiency_data" not in self.own_attr(key, error_state={}):
                if not self.testing:
                    return
                self.last_drive_efficiency_data = {
                    "timestamp": self.now(), "distance": 100.0, "usedkWh": 20.0, "usedBattery": 30.0,
                    "cost": 15.0, "efficiency": 90.0, "distancePerkWh": 5.0, "wh_km": 175.0,
                }
                self.set_attr(key, "last_drive_efficiency_data", self.last_drive_efficiency_data)
            attributes = self.own_attr(key)["last_drive_efficiency_data"]
            for name in ("timestamp", "distance", "usedkWh", "usedBattery", "cost", "efficiency", "distancePerkWh", "wh_km", "reported"):
                if name not in attributes:
                    return
            _LOGGER.info("Setting last drive efficiency data")
            self.last_drive_efficiency_data = {
                "timestamp": to_datetime(attributes["timestamp"]),
                "distance": float(attributes["distance"]),
                "usedkWh": float(attributes["usedkWh"]),
                "usedBattery": float(attributes["usedBattery"]),
                "cost": float(attributes["cost"]),
                "efficiency": float(attributes["efficiency"]),
                "distancePerkWh": float(attributes["distancePerkWh"]),
                "wh_km": float(attributes["wh_km"]),
                "reported": attributes["reported"],
            }

        if bootup:
            if self.is_ev_configured() and self.sensor_states.get("drive_efficiency_last_odometer") in ENTITY_UNAVAILABLE_STATES:
                _LOGGER.info("Setting last odometer")
                set_last_odometer()
            if self.sensor_states.get("drive_efficiency_last_battery_level") in ENTITY_UNAVAILABLE_STATES:
                _LOGGER.info("Setting last battery level")
                set_last_battery_level()
            set_last_drive_efficiency_data()
        else:
            if "reported" in self.last_drive_efficiency_data:
                self.last_drive_efficiency_data["reported"] = False
            if self.is_ev_configured():
                set_last_odometer()
            set_last_battery_level()

    def drive_efficiency(self, state=None) -> None:  # noqa: C901
        state = str(state).lower()
        states = (*CHARGER_READY_STATUS, *CHARGER_NOT_READY_STATUS, *EV_PLUGGED_STATES, *EV_UNPLUGGED_STATES, *HEATING_STATES)
        if state not in states:
            _LOGGER.warning("Ignoring state not in %s: %s", states, state)
            return
        t = self.t
        try:
            if state == "preheat_start":
                self.drive_efficiency_save_car_stats()
                self.preheating = True
                return
            if state == "preheat_cancel":
                self.preheating = False
                return
            if state in EV_UNPLUGGED_STATES + CHARGER_NOT_READY_STATUS:
                if self.battery_level() <= 0.0:
                    return
                if not self.preheating:
                    self.drive_efficiency_save_car_stats()
                    if self.is_ev_configured():
                        self.analysis_unplugged()
                self.preheating = False
                return
            if state not in EV_PLUGGED_STATES + CHARGER_READY_STATUS:
                return
            if "reported" in self.last_drive_efficiency_data and self.last_drive_efficiency_data["reported"]:
                return

            if not self.is_ev_configured():
                distance_per_kwh = self.km_percentage_to_km_kwh(self.distance_per_percentage())
                efficiency = 100.0
                last_battery_level = float(self.own_state("drive_efficiency_last_battery_level", float_type=True, error_state=self.battery_level()))
                if last_battery_level == 0.0:
                    last_battery_level = self.get_max_recommended_charge_limit_battery_level() - self.battery_level()
                    _LOGGER.warning("Last battery level is 0.0, recalculating from current battery level: %s", last_battery_level)
                self.current_battery_level_expenses()
                used_battery = last_battery_level - self.battery_level()
                used_kwh = self.percentage_to_kwh(used_battery)
                kilometers = round(self.get_estimated_total_range() * (used_battery / 100.0), 1)
                # entered level above the saved one (charged elsewhere or no unplug seen): no drive to report
                if used_kwh <= 0.0 or used_battery <= 0.0:
                    _LOGGER.warning("Used kWh (%s) or Used Battery (%s) is 0.0 or less, ignoring drive", used_kwh, used_battery)
                    return
            else:
                odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
                if self.sensor_states.get("drive_efficiency_last_odometer") in ENTITY_UNAVAILABLE_STATES:
                    raise ValueError("drive_efficiency_last_odometer is not available, ignoring drive")
                if self.sensor_states.get("drive_efficiency_last_battery_level") in ENTITY_UNAVAILABLE_STATES:
                    raise ValueError("drive_efficiency_last_battery_level is not available, ignoring drive")
                if not self.is_entity_available(odometer_entity):
                    raise ValueError(f"{odometer_entity} is not available, ignoring drive")
                current_odometer = float(self.get_state(odometer_entity, float_type=True))
                last_odometer = float(self.own_state("drive_efficiency_last_odometer", float_type=True, error_state=current_odometer))
                last_battery_level = float(self.own_state("drive_efficiency_last_battery_level", float_type=True, error_state=self.battery_level()))
                used_battery = last_battery_level - self.battery_level()
                kilometers = current_odometer - last_odometer
                used_kwh = self.percentage_to_kwh(used_battery)
                if used_kwh == 0.0 or used_battery == 0.0:
                    _LOGGER.warning("Used kWh or Used Battery is 0.0, ignoring drive")
                    return
                distance_per_kwh = kilometers / used_kwh
                distance_per_percentage = kilometers / used_battery
                cars_distance_per_percentage = round(self.battery_range() / self.battery_level(), 2)
                efficiency = abs(round((distance_per_percentage / cars_distance_per_percentage) * 100.0, 2))
                if kilometers <= 5.0 or used_battery <= 2.0:
                    _LOGGER.warning("%s <= 5.0 or %s usedBattery <= 2.0, ignoring drive", kilometers, used_battery)
                    return
                if efficiency > 150.0:
                    _LOGGER.warning("Efficiency too high: %s%%. Ignoring. UsedBattery: %s, usedkWh: %s, kilometers: %s", efficiency, used_battery, used_kwh, kilometers)
                    return
                self.drive_efficiency_db.insert(0, [self.now(), efficiency])
                self.drive_efficiency_db = self.drive_efficiency_db[: self.config["database"]["drive_efficiency_db_data_to_save"]]
                self.save_drive_efficiency()
                self.analysis_drive(kilometers, used_battery)
                self.analysis_efficiency_forecast(self.km_kwh_efficiency_db, distance_per_kwh)
                self.km_kwh_efficiency_db.insert(0, [self.now(), distance_per_kwh])
                self.km_kwh_efficiency_db = self.km_kwh_efficiency_db[: self.config["database"]["km_kwh_efficiency_db_data_to_save"]]
                self.save_km_kwh_efficiency()

            cost = 0.0
            cost_per_distance = 0.0
            if self.battery_level_expenses.get("battery_level_expenses_unit") is not None:
                cost = round(self.battery_level_expenses["battery_level_expenses_unit"] * used_kwh, 2)
                cost_per_distance = round(cost / kilometers, 2) if kilometers > 0 else 0.0
            wh_km = round(1000 / distance_per_kwh, 2)
            self.last_drive_efficiency_data = {
                "timestamp": self.now(),
                "distance": kilometers,
                "usedkWh": used_kwh,
                "usedBattery": used_battery,
                "cost": cost,
                "efficiency": efficiency,
                "distancePerkWh": distance_per_kwh,
                "wh_km": wh_km,
                "reported": True,
            }
            self.set_attr("drive_efficiency_last_battery_level", "last_drive_efficiency_data", dict(self.last_drive_efficiency_data))
            self.mark_dirty("runtime")
            _LOGGER.info("Drive: %.1f km, %.2f kWh (%.1f%%), %.1f%% efficiency, %.2f km/kWh", kilometers, used_kwh, used_battery, efficiency, distance_per_kwh)

            if self.config["notification"]["efficiency_on_cable_plug_in"]:
                distance_type = t("ui.common.distance_type")
                lines = [
                    f"{t('ui.drive_efficiency.distance')}: {round(kilometers, 1)}{distance_type}",
                    f"{t('ui.drive_efficiency.used')}: {round(used_kwh, 2)}kWh ({round(used_battery, 1)}%)",
                    f"{t('ui.common.price')}: {cost:.2f}{t('ui.common.valuta')} ({cost_per_distance:.2f} {t('ui.common.valuta')}/{distance_type})",
                    f"{t('ui.common.drive_efficiency')}: {round(efficiency, 1)}%",
                    f"{round(distance_per_kwh, 2)} {distance_type}/kWh ({wh_km} Wh/{distance_type})",
                ]
                indent = " " * 24
                message = "\n" + "".join(f"{indent}{line}\n" for line in lines) + indent
                self.my_notify(message=message, title=f"{self.title} {t('ui.drive_efficiency.title')}", always=True)
            self.set_last_drive_efficiency_attributes(kilometers, used_kwh, used_battery, cost, efficiency, distance_per_kwh, wh_km)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in drive_efficiency: %s %s", err, type(err))
            self.persistent_notification(f"Error in drive_efficiency:\n{err} {type(err)}", f"{self.title} warning", f"{self.name}_drive_efficiency")
