"""Power values, local energy, solar prediction, prices and the hourly databases (engine mixin).

Port of the corresponding ev.py functions. Window values come from :mod:`history`
(buffers), the weather forecast and the Solcast attributes from the job snapshot.
"""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from .const import (
    POWERWALL_CHARGING_TRIGGER,
    POWERWALL_DISCHARGING_TRIGGER,
    SOLAR_PRODUCTION_TRIGGER,
    WEATHER_CONDITION_DICT,
)
from .helpers import (
    average,
    day_of_week_text,
    days_between,
    get_closest_key,
    get_specific_values,
    in_between,
    limit_dict_size,
    reset_datetime,
    reset_time_to_hour,
    time_window_gaussian_weight,
    to_datetime,
)

_LOGGER = logging.getLogger(__name__)

SOLCAST_ENDINGS = ["today", "tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7"]

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


def weather_values() -> list:
    output = []
    for condition in WEATHER_CONDITION_DICT.values():
        if condition not in output:
            output.append(condition)
    return output


def strip_forecast_suffix(entity_id: str, endings: list):
    parts = entity_id.split("_")
    if parts[-1] in endings:
        return "_".join(parts[:-1])
    if len(parts) >= 2 and parts[-2] == "day" and parts[-1].isdigit():
        return "_".join(parts[:-2])
    return None


class EnergyMixin:
    """Power measurements, solar prediction, prices and the databases filled every hour."""

    # ------------------------------------------------------------------ #
    # Power values (W, averaged over a window)
    # ------------------------------------------------------------------ #

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

    def local_energy_available(self, period=None, timeFrom=0, timeTo=None, solar_only=False, without_all_exclusion=False, include_local_energy_distribution=False):  # noqa: N803
        if not self.is_solar_configured():
            return (0.0, 0.0, 0.0, 0.0) if include_local_energy_distribution else 0.0

        watts_available_from_local_energy = 0.0
        watts_available_from_local_energy_solar_only = 0.0
        try:
            now = self.now()
            if timeTo is not None:
                from_time_stamp = now - datetime.timedelta(minutes=timeFrom)
                to_time_stamp = now - datetime.timedelta(minutes=timeTo)
            else:
                if period is None:
                    period = self.config["solar"]["solarpower_use_before_minutes"]
                period = max(period, self.config["cron_interval"])
                to_time_stamp = now
                from_time_stamp = now - datetime.timedelta(minutes=period)

            values = self.power_values(from_time_stamp=from_time_stamp, to_time_stamp=to_time_stamp)
            powerwall_discharging_consumption = values["powerwall_discharging_consumption"]
            ev_used_consumption = values["ev_used_consumption"]
            solar_production = values["solar_production"]
            total_local_energy = solar_production + powerwall_discharging_consumption
            house_power_consumption = values["power_consumption_without_all_exclusion"] if without_all_exclusion else values["power_consumption_without_ignored"]

            watts_available_from_local_energy = max(total_local_energy - house_power_consumption, 0.0)
            watts_available_from_local_energy_solar_only = max(solar_production - house_power_consumption, 0.0)
            watts_available_from_local_energy = round(min(watts_available_from_local_energy, self.config["solar"]["inverter_discharging_power_limit"]), 2)

            if include_local_energy_distribution:
                solar_watts_of_local_energy = 0.0
                powerwall_watts_of_local_energy = 0.0
                watts_from_local_energy = min(watts_available_from_local_energy, ev_used_consumption)
                if watts_from_local_energy > 0.0:
                    solar_watts_of_local_energy = (solar_production / total_local_energy) * watts_from_local_energy if total_local_energy > 0.0 else 0.0
                    powerwall_watts_of_local_energy = (powerwall_discharging_consumption / total_local_energy) * watts_from_local_energy if total_local_energy > 0.0 else 0.0
                return (
                    watts_available_from_local_energy_solar_only if solar_only else watts_available_from_local_energy,
                    watts_from_local_energy,
                    solar_watts_of_local_energy,
                    powerwall_watts_of_local_energy,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating local energy available: %s %s", err, type(err))
            self.persistent_notification(f"Error calculating local energy available: {err} {type(err)}", title=f"{self.title} error", notification_id=f"{self.name}_local_energy_available")
            if include_local_energy_distribution:
                return watts_available_from_local_energy, 0.0, 0.0, 0.0
        return watts_available_from_local_energy_solar_only if solar_only else watts_available_from_local_energy

    def max_local_energy_available_remaining_period(self):
        if not self.is_solar_configured():
            return 0.0, 0.0
        config = self.config
        solar = config["solar"]
        cron_interval = config["cron_interval"]
        now = self.now()
        current_hour = reset_time_to_hour(now)
        period = now.minute

        if solar["solarpower_use_before_minutes"] > 0:
            period = solar["solarpower_use_before_minutes"] - cron_interval
            if solar["solarpower_use_before_minutes"] <= 60:
                usage_period = solar["solarpower_use_before_minutes"]
                period = 0 if usage_period <= 0 else now.minute % usage_period
        elif solar["solarpower_use_before_minutes"] == 0:
            period = cron_interval

        watt_available = 0.0
        watts_available_solar_only = 0.0
        try:
            values = self.power_values(period=period)
            predicted_solar_power = self.local_energy_available(timeFrom=0, timeTo=max(period, cron_interval, 10), without_all_exclusion=True)
            predicted_solar_power_period_watt_result = self.local_energy_available(timeFrom=0, timeTo=period, without_all_exclusion=False, solar_only=True)

            predicted_solar_power = max(predicted_solar_power, 0.0)
            predicted_solar_power_period = f"0-{period}"
            predicted_solar_power_period_watt = 0.0
            if period >= cron_interval:
                predicted_solar_power_period_watt = max(predicted_solar_power_period_watt_result, 0.0)

            solar_min_amp = solar["charging_single_phase_min_amp"] if float(config["charger"]["charging_phases"]) > 1.0 else solar["charging_three_phase_min_amp"] * 3.0
            solar_threadhold = config["charger"]["power_voltage"] * solar_min_amp / 5
            allow_grid_charging_above_solar_available = solar["allow_grid_charging_above_solar_available"] if predicted_solar_power >= solar_threadhold else 0.0

            ignored_consumption = values["ignored_consumption"]
            power_consumption = values["power_consumption"]
            power_consumption_without_ignored = values["power_consumption_without_ignored"]
            power_consumption_without_ignored_ev = values["power_consumption_without_ignored_ev"]
            power_consumption_without_all_exclusion = values["power_consumption_without_all_exclusion"]
            powerwall_charging_consumption = values["powerwall_charging_consumption"]
            powerwall_discharging_consumption = values["powerwall_discharging_consumption"]
            ev_used_consumption = values["ev_used_consumption"]
            solar_production = values["solar_production"]
            total_local_energy = solar_production + powerwall_discharging_consumption

            watts_available_from_local_energy = round(max(total_local_energy - power_consumption_without_all_exclusion, 0.0))
            watts_available_from_local_energy_solar_only = round(max(solar_production - power_consumption_without_all_exclusion, 0.0), 2)
            powerwall_discharging_available = round(max(watts_available_from_local_energy - watts_available_from_local_energy_solar_only, 0.0))

            powerwall_force_power = 0.0
            if self.is_powerwall_configured():
                discharge_above_needed = self.get_powerwall_discharge_above_needed()
                powerwall_battery_level = self.get_powerwall_battery_level()
                powerwall_reserved_battery_level = max(self.get_ev_charge_after_powerwall_battery_level(), 0.0)
                powerwall_charging_power = self.powerwall_max_charging_power(period=period)
                powerwall_charging_timestamps = self.local_energy_prediction(powerwall_charging_timestamps=True)

                powerwall_forced_stop_charging = False
                if current_hour in powerwall_charging_timestamps:
                    if powerwall_battery_level < powerwall_reserved_battery_level or (self.powerwall_max_charging_power(period=cron_interval) > POWERWALL_CHARGING_TRIGGER and period != 59):
                        powerwall_force_power -= max(powerwall_charging_power, solar["powerwall_charging_power_limit"]) if powerwall_charging_power > POWERWALL_CHARGING_TRIGGER else solar["powerwall_charging_power_limit"]
                else:
                    if powerwall_battery_level < powerwall_reserved_battery_level and powerwall_charging_power > POWERWALL_CHARGING_TRIGGER and self.ready_to_charge():
                        _LOGGER.info("Forcing powerwall to stop charging power with %sW, because current_hour %s is not in powerwall_charging_timestamps", powerwall_charging_power, current_hour)
                        powerwall_force_power += min(watts_available_from_local_energy_solar_only, powerwall_charging_consumption)
                        powerwall_forced_stop_charging = True

                if discharge_above_needed and powerwall_battery_level > powerwall_reserved_battery_level:
                    if powerwall_discharging_available < solar["powerwall_discharging_power"] / 2.0:
                        powerwall_force_power += max(solar["powerwall_discharging_power"] - powerwall_discharging_available, 0.0)

                if (
                    watts_available_from_local_energy_solar_only > SOLAR_PRODUCTION_TRIGGER
                    and powerwall_battery_level < powerwall_reserved_battery_level
                    and powerwall_charging_power == 0.0
                    and watts_available_from_local_energy > 0.0
                    and not self.current_hour_in_charge_hours()
                ):
                    self.powerwall_charging_text = self.t(
                        "ui.max_local_energy_available_remaining_period.powerwall_not_charging_but_should",
                        powerwall_battery_level=powerwall_battery_level,
                        powerwall_reserved_battery_level=powerwall_reserved_battery_level,
                    )
                elif powerwall_charging_consumption > POWERWALL_CHARGING_TRIGGER and powerwall_discharging_available < POWERWALL_DISCHARGING_TRIGGER:
                    key = "powerwall_force_stop" if powerwall_forced_stop_charging else "powerwall_charging"
                    self.powerwall_charging_text = self.t(f"ui.max_local_energy_available_remaining_period.{key}", powerwall_charging_consumption=int(powerwall_charging_consumption))
                elif powerwall_discharging_available > POWERWALL_DISCHARGING_TRIGGER and powerwall_battery_level > powerwall_reserved_battery_level:
                    self.powerwall_charging_text = self.t(
                        "ui.max_local_energy_available_remaining_period.powerwall_discharging", powerwall_discharging_available=int(powerwall_discharging_available)
                    )
                else:
                    self.powerwall_charging_text = ""

            multiple = 1.0
            min_cron_intervals = 3
            intervals_use_before = solar["solarpower_use_before_minutes"] / cron_interval
            if solar["max_to_current_hour"]:
                multiple = 1 + (now.minute / 60)
            elif intervals_use_before > min_cron_intervals and solar["solarpower_use_before_minutes"] > cron_interval:
                max_multiple = 1 - (cron_interval / solar["solarpower_use_before_minutes"])
                multiple = time_window_gaussian_weight(period, solar["solarpower_use_before_minutes"] / 2.0, max_multiple, sigma_ratio=0.3)
                multiple += 1.0

            predicted_solar_power = round(predicted_solar_power, 2)
            predicted_solar_power_period_watt = round(predicted_solar_power_period_watt, 2)
            multiple = round(max(multiple, 1.0), 3)
            extra_watt = round(max((predicted_solar_power_period_watt + min(powerwall_force_power, 0.0)) * multiple, 0.0), 2)
            total_available = max(
                sum(((predicted_solar_power + min(powerwall_force_power, 0.0)), allow_grid_charging_above_solar_available, extra_watt, max(powerwall_force_power, 0.0))), 0.0
            )
            limit = solar["inverter_discharging_power_limit"]
            watt_available = min(total_available, limit)
            watts_available_solar_only = min(sum((predicted_solar_power, allow_grid_charging_above_solar_available, extra_watt)), limit)

            self.max_local_energy_available_history[now.isoformat()] = {
                "1. predicted_solar_power": predicted_solar_power,
                "2. allow_grid_charging_above_solar_available": allow_grid_charging_above_solar_available,
                "3. predicted_solar_power_period": predicted_solar_power_period,
                "4. predicted_solar_power_period_watt": predicted_solar_power_period_watt,
                "5. powerwall_force_power": powerwall_force_power,
                "6. multiple": multiple,
                "7. extra_watt (4. + min(5., 0.0)) * 6.)": extra_watt,
                "8. total_available (1. + min(5., 0.0) + 2. + max(5., 0.0) + 7.)": total_available,
                f"9. watt_available (min(8., {limit}))": watt_available,
                f"10. watts_available_solar_only (min(1. + 2. + 7., {limit}))": watts_available_solar_only,
            }
            self.max_local_energy_available_history = limit_dict_size(self.max_local_energy_available_history, 10)

            key = "solar_over_production_current_hour"
            self.set_state(key, watts_available_from_local_energy_solar_only)
            self.set_attr(key, "from_the_last", f"{period} minutes")
            self.set_attr(key, "ignored_consumption", ignored_consumption)
            self.set_attr(key, "power_consumption", power_consumption)
            self.set_attr(key, "power_consumption_without_ignored", power_consumption_without_ignored)
            self.set_attr(key, "power_consumption_without_ignored_ev", power_consumption_without_ignored_ev)
            self.set_attr(key, "power_consumption_without_all_exclusion", power_consumption_without_all_exclusion)
            self.set_attr(key, "ev_used_consumption", ev_used_consumption)
            self.set_attr(key, "solar_production", solar_production)
            if self.is_powerwall_configured():
                self.set_attr(key, "powerwall_charging_consumption", powerwall_charging_consumption)
                self.set_attr(key, "powerwall_discharging_consumption", powerwall_discharging_consumption)
                self.set_attr(key, "powerwall_discharging_available", powerwall_discharging_available)
                self.set_attr(key, "total_local_energy", total_local_energy)
            self.set_attr(key, "local_energy_available", watts_available_from_local_energy)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating max local energy available remaining hour: %s %s", err, type(err))
            self.persistent_notification(
                f"Error calculating max local energy available remaining hour: {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_max_local_energy_available_remaining_period",
            )
        return watt_available, watts_available_solar_only

    def inverter_available(self, error="") -> bool:
        if not self.is_solar_configured():
            return False
        entity_id = self.config["home"]["entity_ids"]["power_consumption_entity_id"]
        if not self.is_entity_configured(entity_id) or self.is_entity_available(entity_id):
            return True
        _LOGGER.error("Inverter not available (%s = %s): %s", entity_id, self.get_state(entity_id), error)
        return False

    # ------------------------------------------------------------------ #
    # Weather forecast
    # ------------------------------------------------------------------ #

    def get_forecast_dict(self) -> dict:
        """weather.get_forecasts result (collected by the coordinator for the job)."""
        forecast = self._snap.forecast_dict if self._snap else None
        return forecast if forecast is not None else {"hourly": {}, "daily": {}}

    def _forecast_index(self, forecast_dict: dict) -> tuple[dict, dict]:
        """First forecast per hour (hourly) and per day (daily), like the list scans of get_forecast."""
        cache = self._job_cache.setdefault("forecast_index", {})
        key = id(forecast_dict)
        if key not in cache:
            hourly: dict = {}
            daily: dict = {}
            for data in forecast_dict.get("hourly") or []:
                if data is None:
                    continue
                try:
                    hourly.setdefault(reset_time_to_hour(to_datetime(data["datetime"])), data)
                except Exception:  # noqa: BLE001
                    break
            for data in forecast_dict.get("daily") or []:
                if data is None:
                    continue
                try:
                    daily.setdefault(to_datetime(data["datetime"]).replace(hour=0, minute=0, second=0, tzinfo=None), data)
                except Exception:  # noqa: BLE001
                    break
            cache[key] = (hourly, daily, forecast_dict)
        return cache[key][0], cache[key][1]

    def get_forecast(self, forecast_dict=None, date=None):
        if date is None:
            date = self.start_of_day()
        forecast = None
        try:
            hourly, daily = self._forecast_index(forecast_dict)
            if "hourly" in forecast_dict:
                forecast = hourly.get(reset_time_to_hour(date))
            if forecast is None and "daily" in forecast_dict:
                forecast = daily.get(date.replace(hour=0, minute=0, second=0, tzinfo=None))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant get forecast for date %s %s", date, err)
        return forecast

    @staticmethod
    def forecast_score(data) -> float:
        try:
            if "cloud_coverage" not in data or "uv_index" not in data or "temperature" not in data:
                if "condition" in data and "datetime" in data:
                    return WEATHER_CONDITION_DICT[data["condition"]]
                raise KeyError("Missing required keys in data")
            normalized_cloud_coverage = (100 - data["cloud_coverage"]) / 100
            normalized_uv_index = min(data["uv_index"] / 11, 1)
            temperature = data["temperature"]
            normalized_temperature = temperature / 20 if temperature <= 20 else 20 / temperature
            if data["cloud_coverage"] >= 90:
                normalized_cloud_coverage *= 0.2
                normalized_uv_index *= 0.1
                normalized_temperature *= 0.5
            score = (normalized_cloud_coverage * 0.5 + normalized_uv_index * 0.3 + normalized_temperature * 0.2) * 100
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant calculate forecast score, using 50.0: %s\n%s", err, data)
            score = 50.0
        return score

    # ------------------------------------------------------------------ #
    # Power values database (consumption per hour)
    # ------------------------------------------------------------------ #

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
            self.power_values_db[hour][key].insert(0, [self.now(), value])
            self.power_values_db[hour][key] = self.power_values_db[hour][key][:keep]
        self.mark_dirty("power_values")

    # ------------------------------------------------------------------ #
    # Solar production available database (per hour and weather score)
    # ------------------------------------------------------------------ #

    @staticmethod
    def db_cloud_coverage_to_score(database: dict) -> dict:
        return {hour: {100 - cloud_coverage: data for cloud_coverage, data in database[hour].items()} for hour in database}

    @staticmethod
    def transform_database(database: dict, step_size=20) -> dict:
        new_database: dict = {}
        for hour, cloud_data in database.items():
            new_database[hour] = {}
            for cloud_coverage, entries in cloud_data.items():
                score_group = (cloud_coverage // step_size) * step_size
                upper_group = score_group + step_size
                if score_group not in new_database[hour]:
                    new_database[hour][score_group] = []
                if upper_group not in new_database[hour] and upper_group <= 100:
                    new_database[hour][upper_group] = []
                sorted_entries = sorted(entries, key=lambda x: x[1])
                split_index = len(sorted_entries) // 2
                new_database[hour][score_group].extend(sorted_entries[:split_index])
                if upper_group <= 100:
                    new_database[hour][upper_group].extend(sorted_entries[split_index:])
        return new_database

    def load_solar_available_db(self, database) -> None:
        if not self.is_solar_configured():
            return
        version = 1.0
        database = deepcopy(database) if isinstance(database, dict) else {}
        if "version" in database:
            version = float(database.pop("version"))
        self.solar_available_db = database
        for h in range(24):
            if h not in self.solar_available_db:
                self.solar_available_db[h] = {}
            for value in weather_values():
                self.solar_available_db[h].setdefault(value, [])
        if version <= 1.0:
            _LOGGER.info("Transforming solar database from version %s to 2.0", version)
            self.solar_available_db = self.transform_database(self.db_cloud_coverage_to_score(self.solar_available_db))
        self.mark_dirty("solar_available")

    def solar_available_append_to_db(self, power) -> None:
        if not self.is_solar_configured():
            return
        if not self.inverter_available(f"solar_available_append_to_db({power})"):
            return
        if len(self.solar_available_db) == 0:
            self.load_solar_available_db(None)
        hour = self.now().hour
        forecast = self.get_forecast(self.get_forecast_dict())
        cloudiness = self.forecast_score(forecast)
        cloudiness_score = get_closest_key(cloudiness, self.solar_available_db[hour], return_key=True)
        rows = self.solar_available_db[hour][cloudiness_score]
        rows.insert(0, [self.now(), power])
        self.solar_available_db[hour][cloudiness_score] = rows[: self.config["database"]["solar_available_db_data_to_save"]]
        _LOGGER.debug("Solar available %sW stored for hour %s, weather score %s", power, hour, cloudiness_score)
        self.mark_dirty("solar_available")

    # ------------------------------------------------------------------ #
    # Solar prediction
    # ------------------------------------------------------------------ #

    def get_solar_kwh_forecast(self) -> dict:
        """(kWh available after the house consumption, sell price) per hour from Solcast."""
        forecast_entity = self.config["solar"]["entity_ids"]["forecast_entity_id"]
        if not forecast_entity:
            return {}
        forecast: dict = {}
        hour_prices = self.get_sell_prices()
        if self.get_integration(forecast_entity) != "solcast_solar":
            return forecast
        base_entity_id = strip_forecast_suffix(forecast_entity, SOLCAST_ENDINGS)
        solcast = self._snap.solcast if self._snap else {}
        for ending in SOLCAST_ENDINGS:
            entity_id = f"{base_entity_id}_{ending}"
            try:
                for data in solcast.get(entity_id) or {}:
                    date = data["period_start"].replace(tzinfo=None)
                    if date not in hour_prices:
                        continue
                    watt = round(data["pv_estimate"] * 1000.0, 0)
                    consumption = self.get_forecast_value(self.power_values_db[date.hour].get("power_consumption_without_all_exclusion", [0.0]))
                    available_kwh = round(max(watt - consumption, 0.0) / 1000.0, 3)
                    forecast[date] = (available_kwh, self.price_provider.calculate_sell_price(date, hour_prices[date]))
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in forecast_task for %s: %s %s", entity_id, err, type(err))
        return forecast

    def local_energy_prediction(self, powerwall_charging_timestamps=False):  # noqa: C901
        """kWh solar surplus (and sell price) per day for the next 7 days.

        With powerwall_charging_timestamps=True: the hours where the home battery is
        expected to charge from solar instead.
        """
        config = self.config
        sell_price = float(self.number_state("solar_sell_fixed_price", error_state=config["solar"]["production_price"])) if self.is_solar_configured() else 0.0
        stop_prediction_before = 3
        days = 7
        today = self.start_of_day()
        output: dict = {"avg": 0, "today": 0}
        output_sell: dict = {"avg": 0, "today": 0}

        if not self.is_solar_configured() or not self.inverter_available("Inverter not available)"):
            if powerwall_charging_timestamps:
                return []
            return output, output_sell

        try:
            sun_events = self.get_sun_events()
            sunrise = sun_events["sunrise"].hour
            sunset = sun_events["sunset"].hour
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant get sunrise/sunset: %s", err)
            return output, output_sell

        trip_planned = self.is_trip_planned()
        start_trip = self.get_trip_date_time() if self.get_trip_date_time() != reset_datetime() and trip_planned else None
        end_trip = self.get_trip_homecoming_date_time() if self.get_trip_homecoming_date_time() != reset_datetime() and trip_planned else None
        trip_last_charging = None
        if start_trip:
            start_trip = reset_time_to_hour(start_trip)
            end_trip = reset_time_to_hour(end_trip)
            trip_last_charging = start_trip - datetime.timedelta(hours=stop_prediction_before)

        using_grid_price = float(self.number_state("solar_sell_fixed_price", error_state=config["solar"]["production_price"])) == -1.0

        solar_prediction_timestamps_dict: dict = {}
        powerwall_charging_timestamps_dict: dict = {}

        def get_database_kwh(cloudiness, date) -> list:
            hour = date.hour
            day_of_week = date.weekday()
            try:
                power_list = get_closest_key(cloudiness, self.solar_available_db[hour])
                power_one_down_list = []
                power_one_up_list = []
                if type(power_list) is list:
                    forecast_value = self.get_forecast_value(power_list) if power_list else 0.0
                    if len(power_list) <= 6 or forecast_value <= 1000.0:
                        if cloudiness >= 20:
                            power_one_down_list = get_closest_key(cloudiness - 20, self.solar_available_db[hour])
                        if cloudiness <= 80:
                            power_one_up_list = get_closest_key(cloudiness + 20, self.solar_available_db[hour])
                    power_list = [forecast_value] if power_list else []
                    power_one_down_list = [self.get_forecast_value(power_one_down_list)] if power_one_down_list else []
                    power_one_up_list = [self.get_forecast_value(power_one_up_list)] if power_one_up_list else []
                    avg_power = max(average(power_list + power_list + power_list + power_one_down_list + power_one_up_list), 0.0)
                    avg_sell_price = sell_price
                    if avg_sell_price == -1.0:
                        avg_sell_price = self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][hour][day_of_week])
                    return [avg_power / 1000, avg_sell_price]
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Cant get cloudiness: %s, hour: %s, day_of_week: %s. %s", cloudiness, hour, day_of_week, err)
            return [0.0, 0.0]

        def process_forecast(loop_datetime, is_away, current_hour_factor, cloudiness, solar_forecast_from_integration,
                             total_db, total_db_sell, total_forecast, total_forecast_sell, total_avg, total_avg_sell, powerwall_kwh_needed):
            loop_kwh = []
            loop_sell = []
            if is_away and not self.is_powerwall_configured():
                return powerwall_kwh_needed
            if cloudiness is not None:
                power_factor = 0.5 if loop_datetime in solar_forecast_from_integration else 1.0
                power_factor *= current_hour_factor
                power_cost = get_database_kwh(cloudiness, loop_datetime)
                loop_kwh.append(power_cost[0] * power_factor)
                loop_sell.append(power_cost[1] * power_factor)
                total_db.append(round(power_cost[0], 3))
                total_db_sell.append(round(power_cost[1], 3))
            if loop_datetime in solar_forecast_from_integration:
                power_factor = 1.5 if cloudiness is not None else 1.0
                power_factor *= current_hour_factor
                loop_kwh.append(solar_forecast_from_integration[loop_datetime][0] * power_factor)
                loop_sell.append(solar_forecast_from_integration[loop_datetime][1] * power_factor)
                total_forecast.append(round(solar_forecast_from_integration[loop_datetime][0], 3))
                total_forecast_sell.append(round(solar_forecast_from_integration[loop_datetime][1], 3))
            if loop_kwh:
                loop_kwh = round(average(loop_kwh), 3)
                loop_sell = round(average(loop_sell), 3)
                if loop_kwh > 0.0:
                    if powerwall_kwh_needed > 0.0:
                        now_hour = self.current_hour()
                        diff = powerwall_kwh_needed - loop_kwh
                        charging_kwh = loop_kwh if diff >= 0.0 else powerwall_kwh_needed
                        powerwall_kwh_needed -= charging_kwh
                        if in_between(loop_datetime, now_hour, now_hour + datetime.timedelta(hours=25)):
                            powerwall_charging_timestamps_dict[loop_datetime] = round(charging_kwh, 3)
                        if diff < 0.0:
                            loop_kwh = loop_kwh - abs(diff)
                    loop_kwh = round(loop_kwh, 3)
                    if powerwall_kwh_needed <= 0.0:
                        if loop_datetime not in solar_prediction_timestamps_dict:
                            solar_prediction_timestamps_dict[loop_datetime] = loop_kwh
                        else:
                            solar_prediction_timestamps_dict[loop_datetime] += loop_kwh
                        total_avg.append(loop_kwh)
                        total_avg_sell.append(loop_sell)
            return powerwall_kwh_needed

        def day_prediction_task(day, forecast_dict, solar_forecast_from_integration):
            date = today + datetime.timedelta(days=day)
            output[date] = 0.0
            output_sell[date] = 0.0
            day_name = day_of_week_text(self.time_plus_days(day))
            total_database, total_database_sell, total_forecast, total_forecast_sell, total, total_sell = [], [], [], [], [], []
            total_away_database, total_away_database_sell, total_away_forecast, total_away_forecast_sell, total_away, total_away_sell = [], [], [], [], [], []
            powerwall_kwh_needed = 0.0
            expensive_hours = [hour.hour for hour in self.get_expensive_hours(day=day)]
            current_hour = self.now().hour
            from_hour = sunrise if day > 0 else max(sunrise, current_hour)
            to_hour = sunset
            work_last_charging = None
            end_work = None
            try:
                if self.workplan_charging_enabled() and self.switch_state(f"workday_{day_name}") == "on":
                    work_last_charging = date.replace(hour=self.setting(f"workday_departure_{day_name}").hour) - datetime.timedelta(hours=stop_prediction_before)
                    end_work = date.replace(hour=self.setting(f"workday_homecoming_{day_name}").hour)
                    if day == 0 and in_between(self.now(), work_last_charging, end_work) and self.ready_to_charge():
                        work_last_charging = None
                        end_work = None
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Cant get workday_homecoming_%s datetime, using from sunrise: %s", day_name, err)

            if forecast_dict:
                if from_hour <= to_hour:
                    if self.is_powerwall_configured():
                        powerwall_battery_level = self.get_powerwall_battery_level() if day == 0 else 0.0
                        powerwall_reserved_battery_level = self.get_ev_charge_after_powerwall_battery_level()
                        powerwall_percentage_needed = max(powerwall_reserved_battery_level - powerwall_battery_level, 0.0)
                        powerwall_kwh_needed = powerwall_percentage_needed * config["solar"]["powerwall_battery_size"] / 100
                    home_hours = []
                    for hour in range(from_hour, to_hour + 1):
                        loop_datetime = date.replace(hour=hour)
                        if config["solar"]["enable_selling_during_expensive_hours"] and hour in expensive_hours and using_grid_price:
                            continue
                        forecast = self.get_forecast(forecast_dict, loop_datetime)
                        cloudiness = self.forecast_score(forecast) if forecast is not None else None
                        current_hour_factor = abs((self.now().minute / 60.0) - 1) if day == 0 and hour == current_hour else 1.0
                        is_away = (work_last_charging and in_between(loop_datetime, work_last_charging, end_work)) or (
                            trip_last_charging and in_between(loop_datetime, trip_last_charging, end_trip)
                        )
                        if is_away:
                            powerwall_kwh_needed = process_forecast(
                                loop_datetime, is_away, current_hour_factor, cloudiness, solar_forecast_from_integration,
                                total_away_database, total_away_database_sell, total_away_forecast, total_away_forecast_sell,
                                total_away, total_away_sell, powerwall_kwh_needed,
                            )
                        else:
                            home_hours.append(hour)
                    for hour in home_hours:
                        loop_datetime = date.replace(hour=hour)
                        forecast = self.get_forecast(forecast_dict, loop_datetime)
                        cloudiness = self.forecast_score(forecast) if forecast is not None else None
                        current_hour_factor = 1.0 if from_hour != current_hour else abs((self.now().minute / 60.0) - 1)
                        powerwall_kwh_needed = process_forecast(
                            loop_datetime, False, current_hour_factor, cloudiness, solar_forecast_from_integration,
                            total_database, total_database_sell, total_forecast, total_forecast_sell,
                            total, total_sell, powerwall_kwh_needed,
                        )
                total = round(sum(total), 3)
                total_sell = round(average(total_sell), 3)
                total_away = round(sum(total_away), 3)
                total_away_sell = round(average(total_away_sell), 3)
                if not powerwall_charging_timestamps:
                    day_db = self.local_energy_prediction_db["solar_prediction"].setdefault(day, {})
                    day_db["home"] = {
                        "total_database": total_database,
                        "total_database_sell": total_database_sell,
                        "total_forecast": total_forecast,
                        "total_forecast_sell": total_forecast_sell,
                        "total": total,
                        "total_sell": total_sell,
                    }
                    day_db["away"] = {
                        "total_database": total_away_database,
                        "total_database_sell": total_away_database_sell,
                        "total_forecast": total_away_forecast,
                        "total_forecast_sell": total_away_forecast_sell,
                        "total": total_away,
                        "total_sell": total_away_sell,
                    }
                if day == 0:
                    output["today"] = total
                    output_sell["today"] = total_sell
            else:
                if day == 0:
                    output["today"] = sum(total)
                    output_sell["today"] = average(total_sell)
                total = None
                total_sell = None
            output[date] = total
            output_sell[date] = total_sell

        forecast_dict = self.get_forecast_dict()
        solar_forecast_from_integration = self._job_cache.get("solar_kwh_forecast")
        if solar_forecast_from_integration is None:
            solar_forecast_from_integration = self._job_cache["solar_kwh_forecast"] = self.get_solar_kwh_forecast()
        for day in range(days + 1):
            # the PyScript version ran the days as tasks; an exception only ended that day
            try:
                day_prediction_task(day, forecast_dict, solar_forecast_from_integration)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in day_prediction_task for day %s: %s %s", day, err, type(err))

        if solar_prediction_timestamps_dict:
            self.local_energy_prediction_db["solar_prediction_timestamps"] = solar_prediction_timestamps_dict

        if powerwall_charging_timestamps:
            if self.get_powerwall_battery_level() < self.get_ev_charge_after_powerwall_battery_level() - 1.0:
                charging_planned_today = any(days_between(timestamp, today) == 0 for timestamp in powerwall_charging_timestamps_dict)
                if not charging_planned_today:
                    powerwall_charging_timestamps_dict[self.current_hour()] = self.percentage_to_kwh(
                        self.get_ev_charge_after_powerwall_battery_level() - self.get_powerwall_battery_level()
                    )
            self.local_energy_prediction_db["powerwall_charging_timestamps"] = powerwall_charging_timestamps_dict
            return powerwall_charging_timestamps_dict.keys()

        avg = [output[key] for key in output if key not in ("avg", "today") and output[key] is not None]
        output["avg"] = average(avg)
        avg_sell = [
            output_sell[key] for key in output_sell
            if key not in ("avg", "today") and output_sell[key] is not None and output[key] != 0.0
        ]
        output_sell["avg"] = average(avg_sell)
        for date in output:
            if output[date] is None:
                output[date] = output["avg"]
        for date in output_sell:
            if output_sell[date] is None:
                output_sell[date] = output_sell["avg"]
        return output, output_sell

    # ------------------------------------------------------------------ #
    # Charged by solar, CO2, kWh price
    # ------------------------------------------------------------------ #

    def kwh_charged_by_solar(self) -> None:
        if not self.is_solar_configured():
            return
        try:
            now = self.now()
            past = now - datetime.timedelta(minutes=60)
            ev_watt = round(abs(float(self.history.get_average_value(self.config["charger"]["entity_ids"]["power_consumtion_entity_id"], past, now, convert_to="W", error_state=0.0))), 3)
            solar_watt = round(max(self.local_energy_available(period=60, without_all_exclusion=True, solar_only=True), 0.0), 3)
            if not ev_watt:
                return
            solar_watt = min(solar_watt, ev_watt)
            ev_solar_kwh = round(solar_watt / 1000, 3)
            solar_kwh = self.own_state("kwh_charged_by_solar", float_type=True, error_state=None)
            if solar_kwh is None:
                message = "Cant get state for kwh_charged_by_solar"
                _LOGGER.error(message)
                self.persistent_notification(message, title=f"{self.title} error", notification_id=f"{self.name}_kwh_charged_by_solar_set_state_error")
                return
            self.set_state("kwh_charged_by_solar", round(float(solar_kwh) + ev_solar_kwh, 2))
            self.mark_dirty("runtime")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating kwh charged by solar: %s %s", err, type(err))
            self.persistent_notification(f"Error calculating kwh charged by solar: {err} {type(err)}", title=f"{self.title} error", notification_id=f"{self.name}_kwh_charged_by_solar_error")

    def solar_charged_percentage(self) -> None:
        if not self.is_solar_configured():
            return
        total_ev_kwh = self.get_state(self.config["charger"]["entity_ids"]["lifetime_kwh_meter_entity_id"], float_type=True, error_state=None)
        total_solar_ev_kwh = self.own_state("kwh_charged_by_solar", float_type=True, error_state=None)
        if total_ev_kwh is None or total_solar_ev_kwh is None:
            _LOGGER.warning("total_ev_kwh or total_solar_ev_kwh is None")
            return
        try:
            if float(total_ev_kwh) == 0.0:
                self.set_state("solar_charged_percentage", 0.0)
                return
            self.set_state("solar_charged_percentage", round(((float(total_solar_ev_kwh) / float(total_ev_kwh)) * 100.0), 1))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant set solar_charged_percentage: %s", err)

    def calc_co2_emitted(self, period=None, added_kwh=None):
        if added_kwh is None:
            return 0.0
        co2_entity = self.config["charger"]["entity_ids"]["co2_entity_id"]
        if not self.is_entity_available(co2_entity):
            return None
        if not co2_entity.startswith("sensor.") or not self.entity_exists(co2_entity):
            return None
        try:
            minutes = self.now().minute if period is None else period
            now = self.now()
            past = now - datetime.timedelta(minutes=minutes)
            ev_kwh = round(abs(float(self.history.get_average_value(self.config["charger"]["entity_ids"]["power_consumtion_entity_id"], past, now, convert_to="W", error_state=0.0))) / 1000.0, 3)
            solar_kwh_available = round(max(self.local_energy_available(period=minutes, without_all_exclusion=True, solar_only=True), 0.0) / 1000.0, 3)
            ev_solar_kwh = round(solar_kwh_available, 3)
            ev_grid_kwh = round(max(ev_kwh - solar_kwh_available, 0.0), 3)
            grid_co2_emitted = 0.0
            grid_co2_kwh = float(self.get_state(co2_entity, float_type=True, error_state=0.0))
            try:
                solar_co2_emitted = (ev_solar_kwh / ev_kwh) * (10 / 1000.0)
                grid_co2_emitted = (ev_grid_kwh / ev_kwh) * (grid_co2_kwh / 1000.0)
                grid_co2_emitted = round((solar_co2_emitted + grid_co2_emitted) * added_kwh, 3)
            except Exception:  # noqa: BLE001
                pass
            co2_emitted = self.own_state("co2_emitted", float_type=True, error_state=None)
            if co2_emitted is not None and grid_co2_emitted > 0.0:
                co2_emitted = round(float(co2_emitted) + grid_co2_emitted, 3)
                _LOGGER.info("Setting co2_emitted to %s increased by %s", co2_emitted, grid_co2_emitted)
                self.set_state("co2_emitted", co2_emitted)
                self.mark_dirty("runtime")
                return co2_emitted
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating co2 emitted: %s %s", err, type(err))
            self.persistent_notification(f"Error calculating co2 emitted: {err} {type(err)}", title=f"{self.title} error", notification_id=f"{self.name}_calc_co2_emitted_error")
        return 0.0

    def get_solar_sell_price(self, set_entity_attr=False, get_avg_offline_sell_price=False):  # noqa: C901
        if not self.is_solar_configured():
            return 0.0
        day_of_week = self.now().weekday()
        valuta_kwh = self.t("ui.common.valuta_kwh")
        production_price = self.config["solar"]["production_price"]
        try:
            sell_price = float(self.number_state("solar_sell_fixed_price", error_state=production_price))
            entity_price = sell_price
            if get_avg_offline_sell_price:
                if sell_price != -1.0:
                    return sell_price
                sun_events = self.get_sun_events()
                return average([
                    self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][hour][day_of_week])
                    for hour in range(sun_events["sunrise"].hour, sun_events["sunset"].hour)
                ])

            sell_data = self.price_provider.get_sell_price(self.now())
            solar_sell_price = sell_data["price"]
            details = sell_data.get("details", {})
            if sell_price == -1.0:
                sell_price = round(solar_sell_price, 3)

            if set_entity_attr:
                price = details.get("price", 0.0)
                transmissions_nettarif = details.get("transmissions_nettarif", 0.0)
                systemtarif = details.get("systemtarif", 0.0)
                elafgift = details.get("elafgift", 0.0)
                tariffs = details.get("tariffs", 0.0)
                surcharge = details.get("surcharge", 0.0)
                tariff_sum = details.get("tariff_sum", 0.0)
                raw_price = details.get("raw_price", price)
                sell_tariffs = details.get("sell_tariffs", solar_sell_price - raw_price)
                key = "kwh_cost_price"
                for item in (
                    "price", "price_timestamp", "transmissions_nettarif", "systemtarif", "elafgift", "tariffs", "surcharge", "tariff_sum",
                    "raw_price", "sell_tariffs_overview", "transmissions_nettarif_", "systemtarif_", "sell_fee", "sell_tariffs",
                    "solar_sell_price", "fixed_sell_price",
                ):
                    self.del_attr(key, item)
                self.set_attr(key, "price", f"{price:.3f} {valuta_kwh}")
                if "price_timestamp" in details:
                    self.set_attr(key, "price_timestamp", str(details["price_timestamp"]))
                self.set_attr(key, "transmissions_nettarif", f"{transmissions_nettarif:.3f} {valuta_kwh}")
                self.set_attr(key, "systemtarif", f"{systemtarif:.3f} {valuta_kwh}")
                self.set_attr(key, "elafgift", f"{elafgift:.3f} {valuta_kwh}")
                self.set_attr(key, "tariffs", f"{tariffs:.3f} {valuta_kwh}")
                self.set_attr(key, "surcharge", f"{surcharge:.3f} {valuta_kwh}")
                self.set_attr(key, "tariff_sum", f"{tariff_sum:.3f} {valuta_kwh}")
                self.set_attr(key, "raw_price", f"{raw_price:.3f} {valuta_kwh}")
                self.set_attr(key, "sell_tariffs_overview", "")
                self.set_attr(key, "transmissions_nettarif_", f"{transmissions_nettarif:.3f} {valuta_kwh}")
                self.set_attr(key, "systemtarif_", f"{systemtarif:.3f} {valuta_kwh}")
                if "sell_fee" in details:
                    self.set_attr(key, "sell_fee", f"{details['sell_fee']:.3f} {valuta_kwh}")
                self.set_attr(key, "sell_tariffs", f"{sell_tariffs:.3f} {valuta_kwh}")
                self.set_attr(key, "solar_sell_price", f"{solar_sell_price:.3f} {valuta_kwh}")

                self.local_energy_prices["solar_kwh_price"] = {
                    "price": round(price, 3),
                    "transmissions_nettarif": round(transmissions_nettarif, 3),
                    "systemtarif": round(systemtarif, 3),
                    "elafgift": round(elafgift, 3),
                    "tariffs": round(tariffs, 3),
                    "surcharge": round(surcharge, 3),
                    "tariff_sum": round(tariff_sum, 3),
                    "raw_price": round(raw_price, 3),
                    "sell_tariffs_overview": "",
                    "transmissions_nettarif_": round(transmissions_nettarif, 3),
                    "systemtarif_": round(systemtarif, 3),
                    "sell_tariffs": round(sell_tariffs, 3),
                    "solar_sell_price": round(solar_sell_price, 3),
                }
                if "price_timestamp" in details:
                    self.local_energy_prices["solar_kwh_price"]["price_timestamp"] = details["price_timestamp"]
                if "sell_fee" in details:
                    self.local_energy_prices["solar_kwh_price"]["sell_fee"] = round(details["sell_fee"], 3)
                if entity_price >= 0.0:
                    self.set_attr(key, "fixed_sell_price", f"{sell_price:.3f} {valuta_kwh}")
                    self.local_energy_prices["solar_kwh_price"]["fixed_sell_price"] = sell_price
        except Exception as err:  # noqa: BLE001
            sell_price = None
            using_text = "default"
            try:
                sell_price = float(self.number_state("solar_sell_fixed_price", error_state=production_price))
                if sell_price == -1.0:
                    sell_price = self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][self.now().hour][day_of_week])
                    using_text = "database forecast"
            except Exception:  # noqa: BLE001
                pass
            if sell_price is None:
                sell_price = max(production_price, 0.0)
            _LOGGER.error("Cant get solar sell price using %s %s: %s %s", using_text, sell_price, err, type(err))
        return sell_price

    def get_current_hour_price(self) -> float:
        value = self.own_state("kwh_cost_price", float_type=True, error_state=None)
        if value is None:
            _LOGGER.warning("Cant get current hour price, using default 0.0")
            return 0.0
        return value

    def get_powerwall_kwh_price(self, kwh=None) -> float:
        if not self.is_powerwall_configured():
            return 0.0

        def is_valid(data: dict, keys: list) -> bool:
            return all(key in data and isinstance(data[key], list) and len(data[key]) > 0 for key in keys)

        if kwh is None:
            kwh = self.config["solar"]["powerwall_battery_size"]
        if kwh <= 0.0:
            return self.get_solar_sell_price()

        def get_data_timestamp(data, timestamp):
            for item in data:
                if reset_time_to_hour(item[0]) == timestamp:
                    return item[1]
            return 0.0

        timestamp = self.now()
        powerwall_kwh = []
        powerwall_total_cost = []
        kwh_price_list = []
        try:
            for hour in range(1, 24):
                loop_timestamp = reset_time_to_hour(timestamp) - datetime.timedelta(hours=hour)
                data = self.power_values_db.setdefault(loop_timestamp.hour, {})
                if not is_valid(data, ["power_consumption_without_all_exclusion", "powerwall_charging_consumption", "powerwall_discharging_consumption", "solar_production"]):
                    continue
                consumption = get_data_timestamp(data["power_consumption_without_all_exclusion"], loop_timestamp) / 1000.0
                charging = get_data_timestamp(data["powerwall_charging_consumption"], loop_timestamp) / 1000.0
                solar_production = get_data_timestamp(data["solar_production"], loop_timestamp) / 1000.0
                solar_available_production = max(solar_production - consumption, 0.0)
                if charging > 0.0 and sum(powerwall_kwh) < kwh:
                    history_key = "history_sell" if solar_available_production > 0 else "history"
                    # the hour offset (not the hour of day) indexes the price history, like ev.py
                    kwh_price = self.get_forecast_value(self.kwh_avg_prices_db[history_key][hour][loop_timestamp.weekday()])
                    kwh_price_list.append(round(kwh_price, 3))
                    powerwall_kwh.append(round(charging, 3))
                    powerwall_total_cost.append(round(charging * kwh_price, 3))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error getting powerwall kWh price: %s %s", err, type(err))
            return self.get_solar_sell_price()

        if powerwall_kwh:
            self.local_energy_prices["powerwall_kwh_price"] = {
                "powerwall_kwh": powerwall_kwh,
                "powerwall_total_cost": powerwall_total_cost,
                "kwh_price_list": kwh_price_list,
                "kr_per_kwh": round(average(powerwall_total_cost), 3) if sum(powerwall_kwh) > 0.0 else 0.0,
            }
        total_kwh = sum(powerwall_kwh)
        total_cost = sum(powerwall_total_cost)
        return total_cost / total_kwh if total_kwh > 0.0 else 0.0

    def calc_kwh_price(self, period=60, update_entities=False, solar_period_current_hour=False) -> float:
        ev_total_price_kwh = 0.0
        valuta = self.t("ui.common.valuta")
        valuta_kwh = self.t("ui.common.valuta_kwh")
        try:
            minute = self.now().minute
            minutes = minute if solar_period_current_hour else period
            minutes = 60 if not in_between(minute, 1, 58) else minutes
            now = self.now()
            past = now - datetime.timedelta(minutes=minutes)

            values = self.power_values(from_time_stamp=past, to_time_stamp=now)
            ev_used_consumption = values["ev_used_consumption"]
            _available, watts_from_local_energy, solar_watts_of_local_energy, powerwall_watts_of_local_energy = self.local_energy_available(
                period=minutes, include_local_energy_distribution=True, without_all_exclusion=True
            )
            trigger = self.solar_charging_trigger_on if self.is_solar_configured() else self.max_watt_charging
            min_watt = trigger / 2 if in_between(minute, 1, 58) else 0.0
            if (ev_used_consumption == 0.0 and not in_between(minute, 1, 58)) or ev_used_consumption < min_watt:
                ev_used_consumption = trigger
            else:
                watts_from_local_energy = round(min(watts_from_local_energy, ev_used_consumption), 3)
            ev_grid_watt = round(max(ev_used_consumption - watts_from_local_energy, 0.0), 3)

            refund = self.price_provider.refund
            grid_kwh_price = self.price_provider.get_buy_price() - refund
            solar_kwh_price = self.get_solar_sell_price(set_entity_attr=update_entities)
            powerwall_kwh_price = self.get_powerwall_kwh_price()

            try:
                ev_grid_share = min(round(ev_grid_watt / ev_used_consumption, 2), 100.0)
                ev_grid_cost = round(ev_grid_share * grid_kwh_price, 5)
            except Exception:  # noqa: BLE001
                ev_grid_share = ev_grid_cost = 0.0
            try:
                ev_solar_share = min(round(solar_watts_of_local_energy / ev_used_consumption, 2), 100.0)
                ev_solar_cost = round(ev_solar_share * solar_kwh_price, 5)
            except Exception:  # noqa: BLE001
                ev_solar_share = ev_solar_cost = 0.0
            try:
                ev_powerwall_share = min(round(powerwall_watts_of_local_energy / ev_used_consumption, 2), 100.0)
                ev_powerwall_cost = round(ev_powerwall_share * powerwall_kwh_price, 5)
            except Exception:  # noqa: BLE001
                ev_powerwall_share = ev_powerwall_cost = 0.0

            ev_total_price_kwh = round(ev_grid_cost + ev_solar_cost + ev_powerwall_cost, 3)

            if update_entities:
                key = "kwh_cost_price"
                _LOGGER.debug("kWh cost price %s", ev_total_price_kwh)
                self.set_state(key, ev_total_price_kwh)
                if not self.is_solar_configured():
                    raw_price = self.price_provider.get_buy_price()
                    self.set_attr(key, "raw_price", f"{raw_price:.2f} {valuta_kwh}")
                    self.set_attr(key, "refund", f"{refund:.2f} {valuta_kwh}")
                    self.set_attr(key, "price", f"{raw_price - refund:.2f} {valuta_kwh}")
                else:
                    self.set_attr(key, "local_energy_and_grid_ratio", "")
                    self.set_attr(key, "solar_share", f"{ev_solar_share * 100:.0f}%")
                    self.set_attr(key, "solar_cost", f"{ev_solar_cost:.2f} {valuta}")
                    self.set_attr(key, "solar_kwh_price", f"{solar_kwh_price:.2f} {valuta_kwh}")
                    if self.is_powerwall_configured():
                        self.set_attr(key, "powerwall_share", f"{ev_powerwall_share * 100:.0f}%")
                        self.set_attr(key, "powerwall_cost", f"{ev_powerwall_cost:.2f} kr")
                        self.set_attr(key, "powerwall_kwh_price", f"{powerwall_kwh_price:.2f} {valuta_kwh}")
                    self.set_attr(key, "grid_share", f"{ev_grid_share * 100:.0f}%")
                    self.set_attr(key, "grid_cost", f"{ev_grid_cost:.2f} {valuta}")
                    self.set_attr(key, "grid_kwh_price", f"{grid_kwh_price:.2f} {valuta_kwh}")
                    self.set_attr(key, "ev_kwh_price", f"{ev_total_price_kwh:.2f} {valuta_kwh}")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating kWh price: %s %s", err, type(err))
            self.persistent_notification(f"Error calculating kWh price: {err} {type(err)}", title=f"{self.title} error", notification_id=f"{self.name}_calc_kwh_price_error")
        return ev_total_price_kwh

    def calc_local_energy_kwh(self, period=60, ev_kwh=None, solar_period_current_hour=False):
        if not self.is_solar_configured():
            return 0.0, 0.0, 0.0
        minutes = self.now().minute if solar_period_current_hour else period
        if ev_kwh is None:
            now = self.now()
            past = now - datetime.timedelta(minutes=minutes)
            ev_kwh = round(abs(float(self.history.get_average_value(self.config["charger"]["entity_ids"]["power_consumtion_entity_id"], past, now, convert_to="kW", error_state=0.0))), 3)
        ev_watt = ev_kwh * 1000.0
        _available, watts_from_local_energy, solar_watts, powerwall_watts = self.local_energy_available(
            period=minutes, include_local_energy_distribution=True, without_all_exclusion=True
        )
        if watts_from_local_energy > ev_watt:
            miscalculated_ratio = watts_from_local_energy / ev_watt if ev_watt > 0.0 else 0.0
            solar_watts = solar_watts * miscalculated_ratio
            powerwall_watts = powerwall_watts * miscalculated_ratio
            watts_from_local_energy = ev_watt
        solar_kwh_available = round(max(watts_from_local_energy, 0.0) / 1000, 3)
        return round(min(solar_kwh_available, ev_kwh), 3), round(solar_watts / 1000, 3), round(powerwall_watts / 1000, 3)

    # ------------------------------------------------------------------ #
    # kWh price history database
    # ------------------------------------------------------------------ #

    def load_kwh_prices(self, database) -> None:
        version = 1.0
        force_save = False
        database = deepcopy(database) if isinstance(database, dict) else {}
        if "version" in database:
            version = float(database.pop("version"))
        self.kwh_avg_prices_db = database

        for name in ("history", "history_sell"):
            if name not in self.kwh_avg_prices_db:
                version = 2.0
                db = self.kwh_avg_prices_db.setdefault(name, {})
                for h in range(24):
                    db.setdefault(h, {})
                    for d in range(7):
                        db[h].setdefault(d, [])
                force_save = True
        for name in ("max", "mean", "min"):
            if name not in self.kwh_avg_prices_db:
                self.kwh_avg_prices_db[name] = []
                force_save = True
        if version <= 1.0:
            _LOGGER.info("Transforming price database from version %s to 2.0", version)
            old_db = deepcopy(self.kwh_avg_prices_db)
            for name in ("history", "history_sell"):
                self.kwh_avg_prices_db[name] = {h: {d: old_db[name][h].copy() for d in range(7)} for h in range(24)}
            force_save = True
        if force_save:
            self.append_kwh_prices()
        self.mark_dirty("kwh_avg_prices")
        self.set_low_forecast_price()

    def append_kwh_prices(self) -> None:
        if len(self.kwh_avg_prices_db) == 0:
            self.load_kwh_prices(None)
        last_save = self.kwh_avg_prices_db.get("last_save", None)
        if isinstance(last_save, datetime.datetime) and last_save.date() == self.now().date():
            _LOGGER.info("KWH avg prices already updated today, skipping append")
            return
        try:
            grid_prices = self.get_grid_prices()
            sell_prices = self.get_sell_prices() if self.is_solar_configured() else {}
            today = self.now().date()
            day_of_week = self.now().weekday()
            max_length = self.config["database"]["kwh_avg_prices_db_data_to_save"]
            refund = self.price_provider.refund

            today_grid_prices = {timestamp.hour: price for timestamp, price in grid_prices.items() if timestamp.date() == today}
            today_sell_prices = {}
            if self.is_solar_configured():
                today_sell_prices = {timestamp.hour: price for timestamp, price in sell_prices.items() if timestamp.date() == today}

            if len(today_grid_prices) < 23:
                _LOGGER.warning("Missing some hours in today's power prices, not saving to database. Added hours: %s", len(today_grid_prices))
                return
            if self.is_solar_configured() and len(today_sell_prices) < 23:
                _LOGGER.warning("Missing some hours in today's sell prices, not saving to database. Added hours: %s", len(today_sell_prices))
                return

            prices = [price - refund for price in today_grid_prices.values()]
            db = self.kwh_avg_prices_db
            db["max"].insert(0, max(prices))
            db["mean"].insert(0, round(average(prices), 3))
            db["min"].insert(0, min(prices))
            db["max"] = db["max"][:max_length]
            db["mean"] = db["mean"][:max_length]
            db["min"] = db["min"][:max_length]

            added = 0
            for h in range(24):
                if h not in today_grid_prices:
                    continue
                db["history"][h][day_of_week].insert(0, today_grid_prices[h] - refund)
                db["history"][h][day_of_week] = db["history"][h][day_of_week][:max_length]
                if self.is_solar_configured() and h in today_sell_prices:
                    db["history_sell"][h][day_of_week].insert(0, round(today_sell_prices[h], 3))
                    db["history_sell"][h][day_of_week] = db["history_sell"][h][day_of_week][:max_length]
                added += 1
            if added < 23:
                _LOGGER.warning("Missing some hours in today's power prices, not saving to database. Added hours: %s", added)
                return
            db["last_save"] = self.now()
            _LOGGER.info("Price history updated with %s hours", added)
            self.mark_dirty("kwh_avg_prices")
            self.set_low_forecast_price()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Can't append KWH avg prices: %s %s", err, type(err))

    def set_low_forecast_price(self) -> None:
        price = round(self.get_forecast_value(self.kwh_avg_prices_db["min"]), 3)
        self.set_attr("charge_very_cheap_battery_level", "low_forecast_price", round(price, 2))
        self.set_attr("charge_ultra_cheap_battery_level", "low_forecast_price", round((price * 0.75), 2))
