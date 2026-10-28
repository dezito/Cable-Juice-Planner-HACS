"""Energy prices and the price database."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ...utils.helpers import average, in_between, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


class EnergyPrice:
    """Energy prices and the price database."""

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
                    if history_key == "history_sell":
                        kwh_price = self.sell_value(kwh_price)
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
                        self.set_attr(key, "powerwall_cost", f"{ev_powerwall_cost:.2f} {valuta}")
                        self.set_attr(key, "powerwall_kwh_price", f"{powerwall_kwh_price:.2f} {valuta_kwh}")
                    self.set_attr(key, "grid_share", f"{ev_grid_share * 100:.0f}%")
                    self.set_attr(key, "grid_cost", f"{ev_grid_cost:.2f} {valuta}")
                    self.set_attr(key, "grid_kwh_price", f"{grid_kwh_price:.2f} {valuta_kwh}")
                    self.set_attr(key, "ev_kwh_price", f"{ev_total_price_kwh:.2f} {valuta_kwh}")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating kWh price: %s %s", err, type(err))
            self.persistent_notification(f"Error calculating kWh price: {err} {type(err)}", title=f"{self.title} error", notification_id=f"{self.name}_calc_kwh_price_error")
        return ev_total_price_kwh

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
        if force_save and self.price_provider is not None:
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
            # only the provider's prices - the hours filled from the history must not be stored again
            grid_prices = self.price_provider.get_buy_prices()
            sell_prices = self.price_provider.get_sell_prices() if self.is_solar_configured() else {}
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
