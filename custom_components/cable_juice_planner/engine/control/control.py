"""What to do right now, triggers, preheating and charging loss (engine part).

Port of the corresponding ev.py functions. The PyScript version sent commands to the
charger and the car (Easee services, switches, numbers, buttons). The integration
only publishes the decisions; the user's own automations act on them:

* ``sensor.<prefix>_charger_action``  charge / stop (attribute charger_enabled)
* ``sensor.<prefix>_charger_phase_1_amps`` ... and ``charging_watts`` (circuit amps
  as attributes of charger_action)
* ``sensor.<prefix>_charging_limit``  the charge limit for the car
* ``event.<prefix>_ev_request``       wake_up / preheat_start / preheat_stop
"""
from __future__ import annotations

from .actions import ControlActions
from .charging_loss import ControlChargingLoss
from .events import ControlEvents
from .preheat import ControlPreheat
from .schedule import ControlSchedule


class Control(ControlActions, ControlSchedule, ControlPreheat, ControlChargingLoss, ControlEvents):
    """charge_if_needed and the event handlers of ev.py."""
