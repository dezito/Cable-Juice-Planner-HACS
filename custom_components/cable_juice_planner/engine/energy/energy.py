"""Power values, local energy, solar prediction, prices and the hourly databases (engine part).

Port of the corresponding ev.py functions. Window values come from :mod:`history`
(buffers), the weather forecast and the Solcast attributes from the job snapshot.
"""
from __future__ import annotations

from .forecast import EnergyForecast
from .power import EnergyPower
from .price import EnergyPrice
from .solar import EnergySolar


class Energy(EnergyPower, EnergyForecast, EnergySolar, EnergyPrice):
    """Power measurements, solar prediction, prices and the databases filled every hour."""
