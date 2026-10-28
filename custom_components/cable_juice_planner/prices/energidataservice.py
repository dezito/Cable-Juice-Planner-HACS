"""Energi Data Service price provider."""
from __future__ import annotations

import datetime
import logging

from ..const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ..utils.helpers import days_between, reset_time_to_hour, to_datetime
from .base import BasePriceProvider, _ZERO_TARIFFS, _naive

_LOGGER = logging.getLogger(__name__)


class EnergiDataServicePriceProvider(BasePriceProvider):
    """https://github.com/MTrab/energidataservice (raw_today / raw_tomorrow / forecast)."""

    source_name = "energidataservice"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.buy_entity_id = self.entity_ids.get("energidataservice_buy_entity_id", "")
        self.sell_entity_id = self.entity_ids.get("energidataservice_sell_entity_id", "") or self.buy_entity_id

    def get_buy_price_entity_id(self) -> str:
        return self.buy_entity_id

    def _load_buy_real_prices(self):
        return self._get_real_prices(self.buy_entity_id)

    def _load_buy_forecast_prices(self):
        return self._get_forecast_prices(self.buy_entity_id)

    def _load_sell_real_prices(self):
        prices = self._get_real_prices(self.sell_entity_id)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    def _load_sell_forecast_prices(self):
        prices = self._get_forecast_prices(self.sell_entity_id)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    @staticmethod
    def _rows(rows):
        for raw in rows or []:
            timestamp = _naive(to_datetime(raw["hour" if "hour" in raw else "time"]))
            price = raw["price"]
            if isinstance(timestamp, datetime.datetime) and isinstance(price, (int, float)):
                yield timestamp, price

    def _get_real_prices(self, entity_id) -> dict:
        current_hour = reset_time_to_hour(self.now)
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        attr = self._get_entity_attr(entity_id)
        prices = {ts: p for ts, p in self._rows(attr.get("raw_today", [])) if days_between(current_hour, ts) == 0}
        if attr.get("tomorrow_valid"):
            prices.update(self._rows(attr.get("raw_tomorrow", [])))
        return dict(sorted(prices.items()))

    def _get_forecast_prices(self, entity_id) -> dict:
        current_hour = reset_time_to_hour(self.now)
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        prices = {}
        for timestamp, price in self._rows(self._get_entity_attr(entity_id).get("forecast", [])):
            if days_between(current_hour, timestamp) > 0:
                prices[timestamp] = price + days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
        return dict(sorted(prices.items()))

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        try:
            if not self._entity_exists(self.sell_entity_id):
                raise ValueError(f"{self.sell_entity_id} not loaded")
            power_prices_attr = self._get_entity_attr(self.sell_entity_id)
            if "tariffs" not in power_prices_attr:
                return dict(_ZERO_TARIFFS)
            attr = power_prices_attr["tariffs"]
            additional = attr["additional_tariffs"]
            tariffs = attr["tariffs"][str(hour)]
            return {
                "transmissions_nettarif": additional["transmissions_nettarif"],
                "systemtarif": additional["systemtarif"],
                "elafgift": additional["elafgift"],
                "tariffs": tariffs,
                "surcharge": 0.0,
                "tariff_sum": sum([additional["transmissions_nettarif"], additional["systemtarif"], additional["elafgift"], tariffs]),
            }
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("get_tariffs(hour = %s, day_of_week = %s): %s", hour, day_of_week, err)
            return dict(_ZERO_TARIFFS)
