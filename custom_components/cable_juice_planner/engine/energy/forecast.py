"""Weather and solar forecast."""
from __future__ import annotations

import datetime
import logging

from ...const import WEATHER_CONDITION_DICT
from ...utils.helpers import (
    average,
    day_of_week_text,
    days_between,
    get_closest_key,
    in_between,
    reset_datetime,
    reset_time_to_hour,
    to_datetime,
)

_LOGGER = logging.getLogger(__name__)


SOLCAST_ENDINGS = ["today", "tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7"]


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


class EnergyForecast:
    """Weather and solar forecast."""

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
                    # hour_prices are sell prices already (ev.py calculated the sell price of them a second time)
                    forecast[date] = (available_kwh, self.sell_value(hour_prices[date]))
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in forecast_task for %s: %s %s", entity_id, err, type(err))
        return forecast

    def local_energy_prediction(self, powerwall_charging_timestamps=False):  # noqa: C901
        """kWh solar surplus (and sell price) per day for the next 7 days.

        With powerwall_charging_timestamps=True: the hours where the home battery is
        expected to charge from solar instead.
        """
        config = self.config
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

        using_grid_price = float(config["solar"]["production_price"]) == -1.0

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
                    avg_sell_price = self.sell_value(self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][hour][day_of_week]))
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
