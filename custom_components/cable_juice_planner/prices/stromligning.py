"""Strømligning price provider."""
from __future__ import annotations

import datetime
import logging

from ..const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ..utils.helpers import days_between, reset_time_to_hour, to_datetime
from .base import BasePriceProvider, _naive

_LOGGER = logging.getLogger(__name__)


class StromligningPriceProvider(BasePriceProvider):
    """Strømligning ('prices' attribute with start/price, tariffs as separate sensors)."""

    source_name = "stromligning"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        get = self.entity_ids.get
        self.buy_current_price = get("stromligning_buy_current_price", "")
        self.buy_tomorrow_available = get("stromligning_buy_tomorrow_available", "")
        self.buy_forecasts = get("stromligning_buy_forecasts", "")
        self.sell_current_price = get("stromligning_sell_current_price", "")
        self.sell_tomorrow_available = get("stromligning_sell_tomorrow_available", "")
        self.sell_forecasts = get("stromligning_sell_forecasts", "")
        self.sell_distribution = get("stromligning_sell_distribution", "")
        self.sell_electricity_tax = get("stromligning_sell_electricity_tax", "")
        self.sell_nettariff = get("stromligning_sell_nettariff", "")
        self.sell_systemtariff = get("stromligning_sell_systemtariff", "")
        self.sell_surcharge = get("stromligning_sell_surcharge", "")
        self._distribution_loaded = False
        self._distribution_prices: dict = {}

    def update_prices(self) -> None:
        self._distribution_loaded = False
        self._distribution_prices = {}
        super().update_prices()

    def get_buy_price_entity_id(self) -> str:
        return self.buy_current_price

    def _load_buy_real_prices(self):
        return self._get_real_prices(self.buy_current_price, self.buy_tomorrow_available)

    def _load_buy_forecast_prices(self):
        return self._get_forecast_prices(self.buy_forecasts, self.buy_tomorrow_available)

    def _load_sell_real_prices(self):
        prices = self._get_real_prices(self.sell_current_price, self.sell_tomorrow_available)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    def _load_sell_forecast_prices(self):
        prices = self._get_forecast_prices(self.sell_forecasts, self.sell_tomorrow_available)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    def _get_real_prices(self, current_entity_id, tomorrow_entity_id) -> dict:
        current_hour = reset_time_to_hour(self.now)
        prices: dict = {}
        self._add_prices_from_entity(prices, current_entity_id, current_hour, forecast=False)
        if self._entity_exists(tomorrow_entity_id) and not self._get_entity_attr(tomorrow_entity_id).get("forecast_data", False):
            self._add_prices_from_entity(prices, tomorrow_entity_id, current_hour, forecast=False)
        return dict(sorted(prices.items()))

    def _get_forecast_prices(self, forecast_entity_id, tomorrow_entity_id=None) -> dict:
        current_hour = reset_time_to_hour(self.now)
        prices: dict = {}
        if self._entity_exists(tomorrow_entity_id) and self._get_entity_attr(tomorrow_entity_id).get("forecast_data", False):
            self._add_prices_from_entity(prices, tomorrow_entity_id, current_hour, forecast=True)
        self._add_prices_from_entity(prices, forecast_entity_id, current_hour, forecast=True)
        return dict(sorted(prices.items()))

    def _add_prices_from_entity(self, prices: dict, entity_id, current_hour, forecast=False) -> None:
        if not entity_id:
            return
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        attr = self._get_entity_attr(entity_id)
        if "prices" not in attr:
            raise ValueError(f"prices not in {entity_id} attributes")
        for raw in attr["prices"]:
            timestamp = _naive(to_datetime(raw.get("start")))
            price = raw.get("price")
            if not isinstance(timestamp, datetime.datetime) or not isinstance(price, (int, float)):
                continue
            if timestamp in prices:
                continue
            if forecast:
                price += days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
            prices[timestamp] = price

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        if timestamp is None:
            timestamp = self.now.replace(hour=hour, minute=0, second=0, microsecond=0)
        timestamp = _naive(timestamp)
        values = (
            self._get_distribution_price(timestamp),
            self._get_entity_state(self.sell_systemtariff),
            self._get_entity_state(self.sell_electricity_tax),
            self._get_entity_state(self.sell_nettariff),
            self._get_entity_state(self.sell_surcharge),
        )
        return {
            "transmissions_nettarif": values[0],
            "systemtarif": values[1],
            "elafgift": values[2],
            "tariffs": values[3],
            "surcharge": values[4],
            "tariff_sum": sum(values),
        }

    def _get_distribution_price(self, timestamp) -> float:
        if not self._distribution_loaded:
            self._distribution_loaded = True
            self._distribution_prices = {}
            if self._entity_exists(self.sell_distribution):
                for raw in self._get_entity_attr(self.sell_distribution).get("prices", []):
                    price_timestamp = _naive(to_datetime(raw.get("start")))
                    price = raw.get("price")
                    if isinstance(price_timestamp, datetime.datetime) and isinstance(price, (int, float)):
                        self._distribution_prices[price_timestamp] = price
        result = self.get_price_at_timestamp(self._distribution_prices, timestamp)
        return result["price"] if result is not None else 0.0

    def _get_entity_state(self, entity_id) -> float:
        if not entity_id or not self._entity_exists(entity_id):
            return 0.0
        value = self._get_cached_entity_state(entity_id)
        return value if isinstance(value, (int, float)) else 0.0
