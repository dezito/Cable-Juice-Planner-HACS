"""Price providers (Energi Data Service and Strømligning).

Port of the PyScript module ``price_provider``. The coordinator collects a snapshot of
the configured price entities on the event loop (at most once an hour, like the shared
price cache of the PyScript version)::

    {"attributes": {entity_id: {...}}, "values": {entity_id: float}}

and the providers turn it into ``{naive local datetime: price}`` dicts in the worker.
With several providers the first one with a price for a timestamp wins; real prices
always win over forecasts.
"""
from .base import PRICE_SNAPSHOT_SECONDS, BasePriceProvider, CombinedPriceProvider, TARIFF_STATE_KEYS
from .energidataservice import EnergiDataServicePriceProvider
from .factory import create_price_provider
from .stromligning import StromligningPriceProvider

__all__ = [
    "BasePriceProvider",
    "CombinedPriceProvider",
    "EnergiDataServicePriceProvider",
    "StromligningPriceProvider",
    "TARIFF_STATE_KEYS",
    "PRICE_SNAPSHOT_SECONDS",
    "create_price_provider",
]
