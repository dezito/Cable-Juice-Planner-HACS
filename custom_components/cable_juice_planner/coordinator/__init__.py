"""Event loop side of Cable Juice Planner.

The coordinator only does light work on the event loop: it collects a plain
:class:`Snapshot` of Home Assistant data, sends the calculation to the worker thread of
the config entry and publishes the result. All heavy work (planning, history,
predictions, markdown) runs in the worker.

The integration does not send commands to the charger or the car. The decisions are
published as action entities (see engine/control) for the user's own automations.
"""
from .coordinator import CJPCoordinator

__all__ = ["CJPCoordinator"]
