"""Grid prices and the forecast."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ..const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ..utils.formatting import get_list_values
from ..utils.helpers import (
    average,
    calculate_ema,
    calculate_trend,
    days_between,
    minutes_between,
    reset_time_to_hour,
    reverse_list,
    start_of_day,
)

_LOGGER = logging.getLogger(__name__)


class Pricing:
    """Grid prices and the forecast."""

    def update_grid_prices(self, initial_run=False) -> None:
        try:
            if self.price_provider is None:
                raise ValueError("No supported price provider configured")
            self.price_provider.update_prices()
            self.get_grid_prices(update_prices=True)
            self.get_sell_prices(update_prices=True)
            if not initial_run:
                self.append_kwh_prices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in update_grid_prices: %s %s", err, type(err))
            self.persistent_notification(f"Error in update_grid_prices: {err} {type(err)}", f"{self.title} error", f"{self.name}_update_grid_prices_error")

    def get_hour_prices(self, update_prices=False, sell_prices=False) -> dict:
        """Prices per period. Without online prices, offline prices from the price history."""
        now = self.now()
        database = self.sell_price_cache if sell_prices else self.grid_price_cache
        local_database_type = "history_sell" if sell_prices else "history"
        hour_prices: dict = {}
        # forecasts corrected with their measured bias per day ahead (the analysis); a change recalculates the prices
        corrections = self.price_forecast_corrections()
        try:
            if "prices" in database and database.get("using_offline_prices") is False and update_prices is False and database.get("corrections") == corrections:
                hour_prices = deepcopy(database["prices"])
            else:
                provider = self.price_provider
                if provider is None:
                    raise ValueError("No price provider")
                if sell_prices:
                    hour_prices = provider.get_sell_prices()
                    sources = provider.sell_price_sources
                else:
                    hour_prices = provider.get_buy_prices()
                    sources = provider.buy_price_sources
                if not hour_prices:
                    raise ValueError(f"No {'sell' if sell_prices else 'grid'} prices returned from {type(provider).__name__}")
                hour_prices = deepcopy(hour_prices)
                sources = deepcopy(sources)
                if corrections:
                    current_hour = self.current_hour()
                    hour_prices = {
                        timestamp: round(price + corrections.get(days_between(current_hour, timestamp), 0.0), 2)
                        if str(sources.get(timestamp, "")).endswith(":forecast") else price
                        for timestamp, price in hour_prices.items()
                    }
                # hours of the plan (today + 7 days) the provider has no price for are filled from the price history
                missing_hours = self._database_prices(local_database_type, hour_prices, 8)
                if missing_hours:
                    _LOGGER.debug("No %s price for %s hours, using the price history", "sell" if sell_prices else "grid", len(missing_hours))
                    hour_prices = dict(sorted({**hour_prices, **missing_hours}.items()))
                    sources.update({timestamp: "database" for timestamp in missing_hours})
                database["missing_hours"] = missing_hours
                database["last_update"] = now
                database["prices"] = deepcopy(hour_prices)
                database["sources"] = sources
                database["corrections"] = corrections
                database["using_offline_prices"] = False
        except Exception as err:  # noqa: BLE001
            if "last_update" in database and minutes_between(database["last_update"], now) <= 120:
                hour_prices = deepcopy(database["prices"])
                _LOGGER.warning("Can't get online %s prices, using last successful: %s", "sell" if sell_prices else "grid", err)
            else:
                _LOGGER.warning("Can't get online %s prices, using database: %s", "sell" if sell_prices else "grid", err)
                database["last_update"] = now
                database["using_offline_prices"] = True
                try:
                    if local_database_type not in self.kwh_avg_prices_db and self.kwh_avg_prices_db:
                        raise KeyError(f"Missing {local_database_type} in KWH_AVG_PRICES_DB")
                    missing_hours = self._database_prices(local_database_type, {}, 7, from_start_of_day=True)
                    if not missing_hours:
                        raise KeyError(f"No prices in {local_database_type} of KWH_AVG_PRICES_DB")
                    hour_prices = dict(missing_hours)
                    database["sources"] = {timestamp: "offline" for timestamp in missing_hours}
                    database["missing_hours"] = missing_hours
                    database["prices"] = deepcopy(hour_prices)
                except Exception as ex:  # noqa: BLE001
                    message = f"Can't get offline prices: {ex} {type(ex)}"
                    _LOGGER.error(message)
                    self.persistent_notification(message, f"{self.title} error", f"{self.name}_get_hour_prices_offline_prices_error")
                    raise ValueError(f"Offline prices error: {ex} {type(ex)}") from ex
        return hour_prices

    def _database_prices(self, history_key: str, prices: dict, days: int, from_start_of_day: bool = False) -> dict:
        """Prices from the price history for the hours from now (or midnight) until the end of days that have none in prices.

        The forecast of the same hour and weekday, plus the same small daily adder as the
        provider forecasts. Hours without any history are left out.
        """
        if len(self.kwh_avg_prices_db) == 0:
            self.load_kwh_prices(None)
        history = self.kwh_avg_prices_db.get(history_key, {})
        current_hour = self.current_hour()
        end = start_of_day(self.now()) + datetime.timedelta(days=days)
        priced_hours = {reset_time_to_hour(timestamp) for timestamp in prices}
        missing = {}
        timestamp = start_of_day(self.now()) if from_start_of_day else current_hour
        while timestamp < end:
            values = history.get(timestamp.hour, {}).get(timestamp.weekday(), [])
            if timestamp not in priced_hours and values:
                adder = days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
                missing[timestamp] = round(self.get_forecast_value(values) + adder, 2)
            timestamp += datetime.timedelta(hours=1)
        return missing

    def sell_value(self, price: float) -> float:
        """What a sold kWh is worth at this hourly sell price.

        With a fixed solar sell price (a production agreement that settles the difference to the
        spot price) it is the fixed price minus the hourly price - negative when the hourly price
        is above the fixed price. Without one (-1) it is the hourly price itself.
        """
        fixed = float(self.config["solar"]["production_price"])
        return price if fixed == -1.0 else fixed - price

    def get_grid_prices(self, update_prices=False) -> dict:
        return self.get_hour_prices(update_prices=update_prices)

    def get_sell_prices(self, update_prices=False) -> dict:
        return self.get_hour_prices(update_prices=update_prices, sell_prices=True)

    def get_expensive_hours(self, day=0) -> dict:
        count = 0
        expensive = {}
        day_timestamp = self.now().date() + datetime.timedelta(days=day)
        for timestamp, price in sorted(self.get_grid_prices().items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
            if timestamp.date() == day_timestamp:
                if count < 4:
                    expensive[timestamp] = price
                else:
                    break
                count += 1
        return expensive

    def get_forecast_type(self) -> str:
        forecast_type = str(self.setting("forecast_type") or "ema").split(" ")[0].lower()
        self.forecast_type = forecast_type if forecast_type in ("average", "ema", "trend") else "ema"
        return self.forecast_type

    def get_forecast_value(self, data=None, forecast_type: str | None = None) -> float:
        """The forecast of the selected type, or of forecast_type (the forecast accuracy analysis)."""
        forecast_type = forecast_type or self.forecast_type
        if not data or not isinstance(data, list) or len(data) == 0:
            return 0.0
        if forecast_type == "ema":
            return round(calculate_ema(reverse_list(get_list_values(data))), 2)
        if forecast_type == "average":
            return round(average(get_list_values(data)), 2)
        if forecast_type == "trend":
            return round(calculate_trend(reverse_list(get_list_values(data))), 2)
        return 0.0
