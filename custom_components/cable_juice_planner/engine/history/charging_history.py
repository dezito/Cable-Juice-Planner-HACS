"""Charging history, battery expenses, drive efficiency and public charging (engine part).

Port of the corresponding ev.py functions. The PyScript version handled the charging
history in a queue worker task; here the calls run inline in the worker thread, which
serialises them the same way.
"""
from __future__ import annotations

from .combine import HistoryCombine
from .cost import HistoryCost
from .efficiency import HistoryEfficiency
from .load import HistoryLoad
from .recalculate import HistoryRecalculate
from .session import HistorySession


class ChargingHistory(HistoryLoad, HistoryRecalculate, HistoryCombine, HistoryCost, HistoryEfficiency, HistorySession):
    """Charging sessions, the charging history markdown and drive efficiency."""
