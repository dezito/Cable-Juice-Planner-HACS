"""Battery levels, range, distance and charging loss."""
from __future__ import annotations

import logging

from ..utils.helpers import round_up

_LOGGER = logging.getLogger(__name__)


class Battery:
    """Battery levels, range, distance and charging loss."""

    def get_min_daily_battery_level(self) -> float:
        return self._number_or_config("min_daily_battery_level", ("ev_car", "min_daily_battery_level"), "daily battery level")

    def get_min_trip_battery_level(self) -> float:
        return self._number_or_config("min_trip_battery_level", ("ev_car", "min_trip_battery_level"), "min trip battery level")

    def get_min_charge_limit_battery_level(self) -> float:
        return self._number_or_config("min_charge_limit_battery_level", ("ev_car", "min_charge_limit_battery_level"), "min charge limit battery level")

    def get_max_recommended_charge_limit_battery_level(self) -> float:
        return self._number_or_config(
            "max_recommended_charge_limit_battery_level", ("ev_car", "max_recommended_charge_limit_battery_level"), "max recommended charge limit battery level"
        )

    def get_very_cheap_grid_charging_max_battery_level(self) -> float:
        return self._number_or_config(
            "very_cheap_grid_charging_max_battery_level", ("ev_car", "very_cheap_grid_charging_max_battery_level"), "very cheap grid charging max battery level"
        )

    def get_ultra_cheap_grid_charging_max_battery_level(self) -> float:
        return self._number_or_config(
            "ultra_cheap_grid_charging_max_battery_level", ("ev_car", "ultra_cheap_grid_charging_max_battery_level"), "ultra cheap grid charging max battery level"
        )

    def get_powerwall_discharge_above_needed(self):
        if not self.is_powerwall_configured():
            return False
        return self.switch_state("powerwall_discharge_above_needed", error_state="off") == "on"

    def get_powerwall_battery_level(self) -> float:
        if not self.is_powerwall_configured():
            return 100.0
        entity_id = self.config["home"]["entity_ids"]["powerwall_battery_level_entity_id"]
        try:
            return float(self.get_state(entity_id, float_type=True, error_state=None))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall battery level from %s, using default value 100.0: %s", entity_id, err)
            return 100.0

    def get_ev_charge_after_powerwall_battery_level(self) -> float:
        if not self.is_powerwall_configured():
            return 100.0
        value = self.number_state("ev_charge_after_powerwall_battery_level")
        if value is None:
            try:
                return float(self.config["solar"]["ev_charge_after_powerwall_battery_level"])
            except Exception:  # noqa: BLE001
                return 100.0
        return float(value)

    def get_completed_battery_level(self) -> float:
        try:
            if not self.is_ev_configured():
                return float(self.number_state("completed_battery_level"))
            return 100.0
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Using default charge completed battery level 100.0: %s", err)
            return 100.0

    def get_estimated_total_range(self) -> float:
        estimated_total_range = 0.0
        try:
            if self.is_ev_configured():
                calc = self.get_forecast_value(self.km_kwh_efficiency_db)
                if calc == 0.0:
                    raise ValueError(f"calc is invalid: {calc}")
                estimated_total_range = self.km_kwh_to_km_percentage(calc) * 100.0
            else:
                try:
                    estimated_total_range = float(self.number_state("estimated_total_range"))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("estimated_total_range is not set to a total range: %s", err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Using default estimated total range 0.0: %s", err)
        return estimated_total_range

    def charging_loss_factor(self, kw: float | None = None) -> float:
        """kWh from the charger = kWh in the car * (1 + this).

        The configured charging loss, or with use_measured_charging_loss the measured one
        (engine/loss.py) once there are measurements: at kw when given and the model knows
        more than one power, else at the max charging power. Calculated once per job and kw.
        """
        if self._snap is not None and self.switch_state("use_measured_charging_loss", error_state="off") == "on":
            cache = self._job_cache.setdefault("charging_loss_factor", {})
            key = None if kw is None else round(kw, 2)
            if key not in cache:
                cache[key] = self.measured_charging_loss_factor(kw)
            if cache[key] is not None:
                return cache[key]
        return abs(self.config["charger"]["charging_loss"])

    def kwh_to_percentage(self, kwh, include_charging_loss=False, kw: float | None = None):
        effective_kwh = kwh
        if include_charging_loss:
            effective_kwh = kwh / (1 + self.charging_loss_factor(kw))
        return effective_kwh / self.config["ev_car"]["battery_size"] * 100

    def percentage_to_kwh(self, percentage, include_charging_loss=False, kw: float | None = None):
        kwh = percentage * self.config["ev_car"]["battery_size"] / 100
        if include_charging_loss:
            kwh = kwh * (1 + self.charging_loss_factor(kw))
        return kwh

    def solar_charging_kw(self) -> float:
        """A typical solar charging power: the middle of the 1 phase solar charging range."""
        solar = self.config["solar"]
        amps = (float(solar["charging_single_phase_min_amp"]) + float(solar["charging_single_phase_max_amp"])) / 2
        return amps * float(self.config["charger"]["power_voltage"]) / 1000

    def km_kwh_to_km_percentage(self, kwh):
        return self.percentage_to_kwh(kwh, include_charging_loss=False)

    def km_percentage_to_km_kwh(self, percentage):
        return self.kwh_to_percentage(percentage, include_charging_loss=False)

    def distance_per_percentage(self) -> float:
        output = 3.0
        if not self.is_ev_configured():
            try:
                output = round(self.get_estimated_total_range() / 100, 2)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(err)
        else:
            try:
                if self.km_kwh_efficiency_db == {}:
                    raise ValueError("No data yet")
                calc = self.get_forecast_value(self.km_kwh_efficiency_db)
                if calc == 0.0:
                    raise ValueError(f"calc is invalid: {calc}")
                output = self.km_kwh_to_km_percentage(calc)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Using default value 3.0: %s", err)
        return output

    def calc_distance_to_battery_level(self, distance):
        return distance / self.distance_per_percentage()

    def calc_battery_level_to_distance(self, battery_level):
        return battery_level * self.distance_per_percentage()

    def battery_level(self) -> float:
        try:
            if not self.is_ev_configured():
                return float(self.number_state("battery_level", error_state=0.0))
            entity_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
            if not self.is_entity_available(entity_id):
                return 0.0
            return float(self.get_state(entity_id, float_type=True, error_state=0.0))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(err)
            return 0.0

    def battery_range(self) -> float:
        distance = 0.0
        if not self.is_ev_configured():
            distance = self.distance_per_percentage() * self.battery_level()
        else:
            entity_id = self.config["ev_car"]["entity_ids"]["estimated_battery_range_entity_id"]
            try:
                if not self.is_entity_available(entity_id):
                    return distance
                distance = float(self.get_state(entity_id, float_type=True, error_state=0.0))
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(err)
        return distance

    def range_to_battery_level(self, extraRange=None, batteryBuffer=None, date=None):  # noqa: N803 - names from ev.py
        min_range_in_battery_level = self.get_min_charge_limit_battery_level()
        extraRange = extraRange if extraRange is not None else self.get_entity_daily_distance(day_text=None, date=date)
        batteryBuffer = batteryBuffer if batteryBuffer is not None else self.get_min_daily_battery_level()
        try:
            if not isinstance(extraRange, (int, float)):
                raise ValueError(f"extraRange should be a number, but got: {extraRange}")
            min_range_in_battery_level = round_up(self.calc_distance_to_battery_level(extraRange)) + batteryBuffer
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating battery level - extraRange: %s, batteryBuffer: %s: %s", extraRange, batteryBuffer, err)
        return min(min_range_in_battery_level, 100.0)

    def kwh_needed_for_charging(self, targetLevel=None, battery=None):  # noqa: N803
        targetLevel = targetLevel if targetLevel is not None else self.get_min_daily_battery_level()
        battery = battery if battery is not None else self.battery_level()
        return max(round(self.percentage_to_kwh(targetLevel - battery, include_charging_loss=True), 2), 0.0)

    def verify_charge_limit(self, limit):
        entity_id = self.config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
        if not self.is_entity_configured(entity_id):
            return limit
        try:
            if self.get_integration(entity_id) in ("kia_uvo", "cupra_we_connect"):
                limit = float(round_up(limit / 10) * 10)
            limit = min(max(limit, self.get_min_charge_limit_battery_level()), 100.0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error verifying charge limit: %s", err)
            limit = self.get_max_recommended_charge_limit_battery_level()
        return limit
