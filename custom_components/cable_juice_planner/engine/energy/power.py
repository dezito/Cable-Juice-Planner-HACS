"""Power measurements and the power database."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ...utils.helpers import average, get_specific_values

_LOGGER = logging.getLogger(__name__)


POWER_VALUE_KEYS = (
    "power_consumption",
    "ignored_consumption",
    "powerwall_charging_consumption",
    "powerwall_discharging_consumption",
    "ev_used_consumption",
    "solar_production",
    "power_consumption_without_ignored",
    "power_consumption_without_ignored_ev",
    "power_consumption_without_all_exclusion",
)


class EnergyPower:
    """Power measurements and the power database."""

    def power_from_ignored(self, from_time_stamp, to_time_stamp) -> float:
        total = 0.0
        try:
            for entity_id in self.config["home"]["entity_ids"]["ignore_consumption_from_entity_ids"] or []:
                if entity_id == "" or entity_id is None:
                    continue
                try:
                    total += float(self.history.get_average_value(entity_id, from_time_stamp, to_time_stamp, convert_to="W", error_state=0.0))
                except Exception:  # noqa: BLE001
                    pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get ignore values from %s to %s: %s", from_time_stamp, to_time_stamp, err)
        return round(total, 2)

    def powerwall_max_charging_power(self, period=60) -> float:
        if not self.is_powerwall_configured():
            return 0.0
        to_time_stamp = self.now()
        from_time_stamp = to_time_stamp - datetime.timedelta(minutes=period)
        max_power = self.config["solar"]["powerwall_charging_power_limit"]
        entity_id = self.config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"]
        try:
            values = self.history.get_values(entity_id, from_time_stamp, to_time_stamp, float_type=True, convert_to="W", error_state=None)
            if values is None or len(values) == 0:
                raise ValueError(f"No powerwall values for {entity_id} found from {from_time_stamp} to {to_time_stamp}")
            if self.config["home"]["invert_powerwall_watt_flow_entity_id"]:
                max_power = abs(max(get_specific_values(values, positive_only=True)))
            else:
                max_power = abs(min(get_specific_values(values, negative_only=True)))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall values for %s from %s to %s: %s", entity_id, from_time_stamp, to_time_stamp, err)
        return max_power

    def get_powerwall_values(self, from_timestamp, to_timestamp) -> list:
        entity_id = self.config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"]
        return self.history.get_values(entity_id, from_timestamp, to_timestamp, float_type=True, convert_to="W", error_state=[0.0])

    def charge_from_powerwall(self, powerwall_values) -> float:
        try:
            if self.config["home"]["invert_powerwall_watt_flow_entity_id"]:
                return abs(round(average(get_specific_values(powerwall_values, positive_only=True)), 0))
            return abs(round(average(get_specific_values(powerwall_values, negative_only=True)), 0))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall charging consumption from values: %s", err)
            return 0.0

    def discharge_from_powerwall(self, powerwall_values) -> float:
        try:
            if self.config["home"]["invert_powerwall_watt_flow_entity_id"]:
                return abs(round(average(get_specific_values(powerwall_values, negative_only=True)), 0))
            return abs(round(average(get_specific_values(powerwall_values, positive_only=True)), 0))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall discharging consumption from values: %s", err)
            return 0.0

    def power_values(self, from_time_stamp=None, to_time_stamp=None, period=None) -> dict:
        """Average power (W) of consumption, battery, EV and solar in a window. Memoised per job."""
        if period is not None:
            to_time_stamp = self.now()
            from_time_stamp = to_time_stamp - datetime.timedelta(minutes=period)
        cache = self._job_cache.setdefault("power_values", {})
        key = (from_time_stamp, to_time_stamp)
        if key not in cache:
            cache[key] = self._calculate_power_values(from_time_stamp, to_time_stamp, period)
        return dict(cache[key])

    def _calculate_power_values(self, from_time_stamp, to_time_stamp, period) -> dict:
        home = self.config["home"]
        history = self.history
        result = dict.fromkeys(POWER_VALUE_KEYS, 0.0)
        try:
            power_consumption = abs(round(float(history.get_average_value(home["entity_ids"]["power_consumption_entity_id"], from_time_stamp, to_time_stamp, convert_to="W", error_state=0.0)), 2))
            ignored_consumption = abs(self.power_from_ignored(from_time_stamp, to_time_stamp))
            powerwall_values = self.get_powerwall_values(from_time_stamp, to_time_stamp)
            powerwall_charging_consumption = self.charge_from_powerwall(powerwall_values)
            powerwall_discharging_consumption = self.discharge_from_powerwall(powerwall_values)
            ev_used_consumption = abs(round(float(history.get_average_value(self.config["charger"]["entity_ids"]["power_consumtion_entity_id"], from_time_stamp, to_time_stamp, convert_to="W", error_state=0.0)), 2))
            solar_production = abs(round(float(history.get_average_value(self.config["solar"]["entity_ids"]["production_entity_id"], from_time_stamp, to_time_stamp, convert_to="W", error_state=0.0)), 2))

            if not home["power_consumption_entity_id_include_powerwall_discharging"]:
                power_consumption += powerwall_discharging_consumption
            if home["power_consumption_entity_id_include_powerwall_charging"]:
                power_consumption -= powerwall_charging_consumption

            power_consumption_without_ignored = round(power_consumption - ignored_consumption, 2)
            power_consumption_without_ignored_ev = round(power_consumption_without_ignored - ev_used_consumption, 2)
            result.update(
                power_consumption=power_consumption,
                ignored_consumption=ignored_consumption,
                powerwall_charging_consumption=powerwall_charging_consumption,
                powerwall_discharging_consumption=powerwall_discharging_consumption,
                ev_used_consumption=ev_used_consumption,
                solar_production=solar_production,
                power_consumption_without_ignored=power_consumption_without_ignored,
                power_consumption_without_ignored_ev=power_consumption_without_ignored_ev,
                power_consumption_without_all_exclusion=max(round(power_consumption_without_ignored_ev - powerwall_charging_consumption, 2), 0.0),
            )
        except Exception as err:  # noqa: BLE001
            message = f"Failed to get power values from {from_time_stamp} to {to_time_stamp} or period {period}: {err} {type(err)}"
            _LOGGER.error(message)
            self.persistent_notification(message, title=f"{self.title} error", notification_id=f"{self.name}_power_values")
        return result

    def load_power_values_db(self, database) -> None:
        database =deepcopy(database) if isinstance(database, dict) else {}
        database.pop("version", None)
        self.power_values_db = database
        for h in range(24):
            if h not in self.power_values_db:
                self.power_values_db[h] = {}
        self.mark_dirty("power_values")

    def power_values_to_db(self, data) -> None:
        # Recorded whenever the house consumption entity works; ev.py also required solar
        # and a Solcast forecast, which left the history empty without solar.
        entity_id = self.config["home"]["entity_ids"]["power_consumption_entity_id"]
        if not data or not self.is_entity_configured(entity_id):
            return
        if not self.is_entity_available(entity_id):
            _LOGGER.error("Power consumption not available (%s = %s): power_values_to_db(%s)", entity_id, self.get_state(entity_id), data)
            return
        if len(self.power_values_db) == 0:
            self.load_power_values_db(None)
        hour = self.now().hour
        keep = self.config["database"]["power_values_db_data_to_save"]
        for key, value in data.items():
            if key not in self.power_values_db[hour]:
                self.power_values_db[hour][key] = []
            if key == "power_consumption_without_all_exclusion":
                self.analysis_consumption_forecast(self.power_values_db[hour][key], value)
            self.power_values_db[hour][key].insert(0, [self.now(), value])
            self.power_values_db[hour][key] = self.power_values_db[hour][key][:keep]
        self.mark_dirty("power_values")

    def inverter_available(self, error="") -> bool:
        if not self.is_solar_configured():
            return False
        entity_id = self.config["home"]["entity_ids"]["power_consumption_entity_id"]
        if not self.is_entity_configured(entity_id) or self.is_entity_available(entity_id):
            return True
        _LOGGER.error("Inverter not available (%s = %s): %s", entity_id, self.get_state(entity_id), error)
        return False
