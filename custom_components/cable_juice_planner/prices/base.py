"""Shared price helpers, the base provider and the combined provider."""
from __future__ import annotations

import datetime
import logging


_LOGGER = logging.getLogger(__name__)


TARIFF_STATE_KEYS = (
    "stromligning_sell_electricity_tax",
    "stromligning_sell_nettariff",
    "stromligning_sell_systemtariff",
    "stromligning_sell_surcharge",
)

PRICE_SNAPSHOT_SECONDS = 60 * 60 - 5

_ZERO_TARIFFS = {
    "transmissions_nettarif": 0.0,
    "systemtarif": 0.0,
    "elafgift": 0.0,
    "tariffs": 0.0,
    "surcharge": 0.0,
    "tariff_sum": 0.0,
}


def _naive(timestamp):
    return timestamp.replace(tzinfo=None) if isinstance(timestamp, datetime.datetime) else timestamp


class BasePriceProvider:
    source_name = "base"

    def __init__(self, config: dict) -> None:
        prices_config = config.get("prices", {})
        self.entity_ids = dict(prices_config.get("entity_ids", {}))
        self.refund = abs(prices_config.get("refund", 0.0))
        self.sell_fee = abs(prices_config.get("sell_fee", 0.0))
        self.buy_real_prices: dict = {}
        self.buy_forecast_prices: dict = {}
        self.sell_real_prices: dict = {}
        self.sell_forecast_prices: dict = {}
        self.sell_price_details: dict = {}
        self.shared_data: dict = {"attributes": {}, "values": {}, "version": 0}
        self.now = datetime.datetime.now().replace(microsecond=0)

    @staticmethod
    def get_price_at_timestamp(prices: dict, timestamp):
        """Latest price at or before timestamp on the same day."""
        if not prices:
            return None
        timestamp = _naive(timestamp)
        best = None
        for price_timestamp in prices:
            price_timestamp = _naive(price_timestamp)
            if price_timestamp.date() == timestamp.date() and price_timestamp <= timestamp:
                if best is None or price_timestamp > best:
                    best = price_timestamp
        if best is None:
            return None
        return {"price_timestamp": best, "price": prices[best]}

    def update_prices(self) -> None:
        old_details = self.sell_price_details
        self.sell_price_details = {}
        try:
            buy_real = self._load_buy_real_prices()
            buy_forecast = self._load_buy_forecast_prices()
            sell_real = self._load_sell_real_prices()
            sell_forecast = self._load_sell_forecast_prices()
        except Exception:
            self.sell_price_details = old_details
            raise
        self.buy_real_prices = buy_real
        self.buy_forecast_prices = buy_forecast
        self.sell_real_prices = sell_real
        self.sell_forecast_prices = sell_forecast

    def _entity_exists(self, entity_id) -> bool:
        return bool(entity_id) and entity_id in self.shared_data["attributes"]

    def _get_entity_attr(self, entity_id) -> dict:
        return self.shared_data["attributes"].get(entity_id, {})

    def _get_cached_entity_state(self, entity_id):
        return self.shared_data["values"].get(entity_id, 0.0)

    def get_buy_real_prices(self):
        return self.buy_real_prices

    def get_buy_forecast_prices(self):
        return self.buy_forecast_prices

    def get_sell_real_prices(self):
        return self.sell_real_prices

    def get_sell_forecast_prices(self):
        return self.sell_forecast_prices

    def get_buy_price_entity_id(self) -> str:
        return ""

    def _load_buy_real_prices(self):
        raise NotImplementedError

    def _load_buy_forecast_prices(self):
        return {}

    def _load_sell_real_prices(self):
        raise NotImplementedError

    def _load_sell_forecast_prices(self):
        return {}

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        raise NotImplementedError

    def calculate_sell_price(self, timestamp, price) -> float:
        timestamp = _naive(timestamp)
        tariff = self.get_tariffs(timestamp.hour, timestamp.weekday(), timestamp=timestamp)
        raw_price = price - tariff["tariff_sum"]
        sell_tariffs = sum((self.sell_fee, tariff["transmissions_nettarif"], tariff["systemtarif"]))
        sell_price = round(raw_price + sell_tariffs, 2)
        self.sell_price_details[timestamp] = {
            "price": price,
            "price_timestamp": timestamp,
            "transmissions_nettarif": tariff["transmissions_nettarif"],
            "systemtarif": tariff["systemtarif"],
            "elafgift": tariff["elafgift"],
            "tariffs": tariff["tariffs"],
            "surcharge": tariff["surcharge"],
            "tariff_sum": tariff["tariff_sum"],
            "raw_price": raw_price,
            "sell_fee": self.sell_fee,
            "sell_tariffs": sell_tariffs,
            "solar_sell_price": sell_price,
        }
        return sell_price

    @staticmethod
    def _periods_in_hour(prices: dict, timestamp) -> int:
        hour_start = _naive(timestamp).replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + datetime.timedelta(hours=1)
        return sum(1 for price_timestamp in prices if hour_start <= _naive(price_timestamp) < hour_end)


class CombinedPriceProvider(BasePriceProvider):
    """Merges the configured providers."""

    source_name = "combined"

    def __init__(self, config: dict, providers: list[BasePriceProvider]) -> None:
        super().__init__(config)
        self.providers = providers
        self.processed_version = -1
        self.buy_prices: dict = {}
        self.sell_prices: dict = {}
        self.buy_price_sources: dict = {}
        self.sell_price_sources: dict = {}
        self.last_update = None
        for provider in providers:
            provider.shared_data = self.shared_data

    def set_now(self, now: datetime.datetime) -> None:
        self.now = now
        for provider in self.providers:
            provider.now = now

    def set_snapshot(self, price_data: dict) -> None:
        """New HA snapshot (replaces _update_shared_price_data)."""
        self.shared_data["attributes"] = dict(price_data.get("attributes", {}))
        self.shared_data["values"] = dict(price_data.get("values", {}))
        self.shared_data["version"] += 1

    def update_prices(self, force=False) -> None:
        self._sync_prices()

    def _sync_prices(self) -> None:
        if self.processed_version == self.shared_data["version"] or self.shared_data["version"] == 0:
            return
        for provider in self.providers:
            try:
                provider.update_prices()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't update prices from %s: %s %s", provider.source_name, err, type(err))
        self.buy_prices, self.buy_real_prices, self.buy_price_sources, _ = self._combine("buy")
        self.sell_prices, self.sell_real_prices, self.sell_price_sources, self.sell_price_details = self._combine("sell")
        self.last_update = self.now
        self.processed_version = self.shared_data["version"]

    def _combine(self, kind: str) -> tuple[dict, dict, dict, dict]:
        prices: dict = {}
        real_prices: dict = {}
        sources: dict = {}
        details: dict = {}
        sell = kind == "sell"
        for provider in self.providers:
            try:
                for timestamp, price in getattr(provider, f"get_{kind}_real_prices")().items():
                    timestamp = _naive(timestamp)
                    if timestamp not in prices:
                        price = round(price, 2)
                        prices[timestamp] = price
                        real_prices[timestamp] = price
                        sources[timestamp] = f"{provider.source_name}:real"
                        if sell and timestamp in provider.sell_price_details:
                            details[timestamp] = provider.sell_price_details[timestamp].copy()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't get real prices from %s: %s %s", provider.source_name, err, type(err))
        for provider in self.providers:
            try:
                for timestamp, price in getattr(provider, f"get_{kind}_forecast_prices")().items():
                    timestamp = _naive(timestamp)
                    if timestamp not in prices:
                        prices[timestamp] = round(price, 2)
                        sources[timestamp] = f"{provider.source_name}:forecast"
                        if sell and timestamp in provider.sell_price_details:
                            details[timestamp] = provider.sell_price_details[timestamp].copy()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't get forecast prices from %s: %s %s", provider.source_name, err, type(err))
        if not prices:
            raise ValueError("No prices available from configured price providers")
        return (
            dict(sorted(prices.items())),
            dict(sorted(real_prices.items())),
            dict(sorted(sources.items())),
            dict(sorted(details.items())),
        )

    def get_buy_prices(self) -> dict:
        self._sync_prices()
        return self.buy_prices

    def get_sell_prices(self) -> dict:
        self._sync_prices()
        return self.sell_prices

    def get_buy_real_prices(self) -> dict:
        self._sync_prices()
        return self.buy_real_prices

    def get_sell_real_prices(self) -> dict:
        self._sync_prices()
        return self.sell_real_prices

    def get_buy_price(self, timestamp=None) -> float:
        self._sync_prices()
        timestamp = _naive(timestamp if timestamp is not None else self.now)
        result = self.get_price_at_timestamp(self.buy_real_prices, timestamp)
        if result is None:
            raise ValueError(f"No real buy price available for {timestamp}")
        return result["price"]

    def get_sell_price(self, timestamp=None) -> dict:
        self._sync_prices()
        timestamp = _naive(timestamp if timestamp is not None else self.now)
        result = self.get_price_at_timestamp(self.sell_prices, timestamp)
        if result is None:
            raise ValueError(f"No sell price available for {timestamp}")
        price_timestamp = result["price_timestamp"]
        details = self.sell_price_details.get(price_timestamp)
        if details is None:
            return {"price": result["price"], "details": {"price_timestamp": price_timestamp, "solar_sell_price": result["price"]}}
        return {"price": result["price"], "details": details.copy()}

    def buy_prices_available(self) -> bool:
        self._sync_prices()
        return bool(self.buy_prices)

    def get_buy_price_entity_id(self) -> str:
        return ", ".join(provider.get_buy_price_entity_id() for provider in self.providers if provider.get_buy_price_entity_id())

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        self._sync_prices()
        if timestamp is None:
            timestamp = self.now.replace(hour=hour, minute=0, second=0, microsecond=0)
        result = self.get_price_at_timestamp(self.sell_price_details, _naive(timestamp))
        if result is not None:
            details = result["price"]
            return {key: details.get(key, 0.0) for key in _ZERO_TARIFFS}
        return dict(_ZERO_TARIFFS)

    def get_sell_periods_in_hour(self, timestamp) -> int:
        self._sync_prices()
        return self._periods_in_hour(self.sell_real_prices, timestamp) or 1
