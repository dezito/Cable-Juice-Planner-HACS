"""Local (solar) energy and the solar database."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ...const import POWERWALL_CHARGING_TRIGGER, POWERWALL_DISCHARGING_TRIGGER, SOLAR_PRODUCTION_TRIGGER
from ...utils.helpers import (
    average,
    get_closest_key,
    limit_dict_size,
    reset_time_to_hour,
    time_window_gaussian_weight,
)
from .forecast import weather_values

_LOGGER = logging.getLogger(__name__)


class EnergySolar:
    """Local (solar) energy and the solar database."""

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
            sell_price = float(production_price)
            entity_price = sell_price
            if get_avg_offline_sell_price:
                sun_events = self.get_sun_events()
                return self.sell_value(average([
                    self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][hour][day_of_week])
                    for hour in range(sun_events["sunrise"].hour, sun_events["sunset"].hour)
                ]))

            sell_data = self.price_provider.get_sell_price(self.now())
            solar_sell_price = sell_data["price"]
            details = sell_data.get("details", {})
            # what the solar kWh would have been sold for (see sell_value)
            sell_price = round(self.sell_value(solar_sell_price), 3)

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
                    self.set_attr(key, "fixed_sell_price", f"{entity_price:.3f} {valuta_kwh}")
                    self.local_energy_prices["solar_kwh_price"]["fixed_sell_price"] = entity_price
        except Exception as err:  # noqa: BLE001
            sell_price = None
            using_text = "default"
            try:
                sell_price = self.sell_value(self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][self.now().hour][day_of_week]))
                using_text = "database forecast"
            except Exception:  # noqa: BLE001
                pass
            if sell_price is None:
                sell_price = max(production_price, 0.0)
            _LOGGER.error("Cant get solar sell price using %s %s: %s %s", using_text, sell_price, err, type(err))
        return sell_price

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
