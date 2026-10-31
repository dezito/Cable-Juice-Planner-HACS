"""Charging loss measured over one charging session (the calculate_charging_loss switch).

The continuous charging loss model per charging power is in engine/loss.py.
"""
from __future__ import annotations

import logging

from ...const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    CHARGING_LOSS_MAX,
    CHARGING_LOSS_MIN_BATTERY_LEVEL,
)

_LOGGER = logging.getLogger(__name__)


class ControlChargingLoss:
    """Charging loss measured over one charging session."""

    def charging_loss_completed_battery_level(self) -> float:
        """Battery level at the end of the measurement: the car's own level when it has
        one, else the charging limit (car) or the completed level (no car integration)."""
        if self.is_ev_configured():
            battery_level = self.battery_level()
            if battery_level > 0.0:
                return battery_level
            limit_entity = self.config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
            if self.is_entity_configured(limit_entity):
                return float(self.get_state(limit_entity, float_type=True, error_state=100.0))
        return self.get_completed_battery_level()

    def calculate_charging_loss(self, source: str, value=None, old_value=None) -> str | None:
        """source: 'switch' (calculate_charging_loss changed), 'status', 'kwh_meter' or 'timeout'.

        Returns 'wait_for_meter' when charging has completed: some chargers are slow to
        update the kWh meter, so the loss is calculated at the next meter update, or by the
        coordinator's 'timeout' call (CHARGING_LOSS_METER_TIMEOUT) with the value then.
        """
        charger = self.config["charger"]["entity_ids"]
        t = self.t
        if not self.is_entity_available(charger["kwh_meter_entity_id"]) or not self.is_entity_available(charger["status_entity_id"]):
            if self.is_calculating_charging_loss():
                _LOGGER.error("Entities not available, stopping charging loss calculation")
                self.my_notify(
                    "Entities for calculating charging loss is not available, stopping calculation",
                    title=f"{self.title} Error",
                    always=True,
                    persistent_notification=True,
                    persistent_notification_id=f"{self.name}_calculate_charging_loss_not_available",
                )
                self.set_setting("calculate_charging_loss", False)
            return None

        def reset():
            self.charging_loss_car_begin_battery_level = 0.0
            self.charging_loss_car_begin_kwh = 0.0
            self.charging_loss_charger_begin_kwh = 0.0
            self.charging_loss_charging_completed = False
            self.mark_dirty("runtime")

        try:
            battery_size = self.config["ev_car"]["battery_size"]
            charger_current_kwh = float(self.get_state(charger["kwh_meter_entity_id"], float_type=True, error_state=None))

            if source == "switch":
                if not value:
                    reset()
                    return None
                if not self.is_ev_configured() and self.battery_level() == 0.0:
                    raise ValueError("Battery level is 0.0, make sure battery level is set or car is connected")
                self.charging_loss_car_begin_battery_level = self.battery_level()
                self.charging_loss_car_begin_kwh = battery_size * (self.charging_loss_car_begin_battery_level / 100.0)
                self.charging_loss_charger_begin_kwh = charger_current_kwh
                self.charging_loss_charging_completed = False
                self.mark_dirty("runtime")
                _LOGGER.debug("Charging loss: begin %s%% (%skWh), charger begin %skWh", self.charging_loss_car_begin_battery_level, self.charging_loss_car_begin_kwh, charger_current_kwh)
                return None

            if not self.is_calculating_charging_loss():
                return None
            if source == "status":
                if value in CHARGER_READY_STATUS + CHARGER_COMPLETED_STATUS and old_value in CHARGER_CHARGING_STATUS:
                    self.charging_loss_charging_completed = True
                    self.mark_dirty("runtime")
                    return "wait_for_meter"
                if value in CHARGER_CHARGING_STATUS and self.charging_loss_charging_completed:
                    # the car started again (e.g. topping up a cold battery), keep measuring
                    self.charging_loss_charging_completed = False
                    self.mark_dirty("runtime")
                elif value in CHARGER_NOT_READY_STATUS and not self.charging_loss_charging_completed:
                    raise ValueError("The charger was disconnected before charging completed")
                return None
            if source not in ("kwh_meter", "timeout"):
                raise ValueError(f"Unknown error: source={source}, value={value}, old_value={old_value}")
            if not self.charging_loss_charging_completed:
                return None

            completed_battery_level = self.charging_loss_completed_battery_level()
            added_battery_level = completed_battery_level - self.charging_loss_car_begin_battery_level
            _LOGGER.debug(
                "Charging loss (%s): completed %s%%, begin %s%% (%skWh), charger begin %skWh, now %skWh",
                source, completed_battery_level, self.charging_loss_car_begin_battery_level, self.charging_loss_car_begin_kwh, self.charging_loss_charger_begin_kwh, charger_current_kwh,
            )
            if added_battery_level < CHARGING_LOSS_MIN_BATTERY_LEVEL:
                raise ValueError(f"Only {round(added_battery_level, 1)}% was charged, at least {CHARGING_LOSS_MIN_BATTERY_LEVEL}% is needed for a precise result")
            added_kwh = charger_current_kwh - self.charging_loss_charger_begin_kwh
            car_kwh = battery_size * (completed_battery_level / 100.0) - self.charging_loss_car_begin_kwh
            if added_kwh <= 0.0:
                raise ValueError(f"The kWh meter did not count any energy ({round(added_kwh, 2)}kWh)")
            # stored negative and used as kWh from the charger = kWh in the car * (1 + |loss|)
            charging_loss = round(1 - (added_kwh / car_kwh), 4)
            if not CHARGING_LOSS_MAX <= charging_loss < 0.0:
                raise ValueError(
                    f"Charging loss {round(charging_loss * 100.0, 1)}% is not between {CHARGING_LOSS_MAX * 100.0:.0f}% and 0% "
                    f"({round(added_kwh, 2)}kWh from the charger, {round(car_kwh, 2)}kWh in the car), set charging limit in car and let charging complete"
                )
            self.config["charger"]["charging_loss"] = charging_loss
            if self._out is not None:
                self._out.config_updates["charging_loss"] = charging_loss
            reset()
            self.set_setting("calculate_charging_loss", False)
            message = t("ui.calculate_charging_loss.calculated", percentage=round(charging_loss * 100))
            _LOGGER.info("Charging loss calculated: %s", charging_loss)
            self.my_notify(message, title=f"{self.title} {t('ui.calculate_charging_loss.title')}", always=True)
            self.persistent_notification(message, title=f"{self.title} {t('ui.calculate_charging_loss.title')}", notification_id=f"{self.name}_calculate_charging_loss_calculated")
            return None
        except Exception as err:  # noqa: BLE001
            reset()
            self.set_setting("calculate_charging_loss", False)
            _LOGGER.error("Failed to calculate charging loss: %s %s", err, type(err))
            message = f"{t('ui.calculate_charging_loss.failed')}:\n{err} {type(err)}"
            self.my_notify(message, title=f"{self.title} {t('ui.calculate_charging_loss.failed_title')}", always=True, persistent_notification=True)
            self.persistent_notification(message, title=f"{self.title} {t('ui.calculate_charging_loss.failed_title')}", notification_id=f"{self.name}_calculate_charging_loss_error")
