"""Preheating, charging loss calculation and battery level notifications."""
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
from ...utils.helpers import day_of_week_text, minutes_between, reset_datetime

_LOGGER = logging.getLogger(__name__)

BUTTON_DOMAINS = ("button", "input_button")
SWITCH_DOMAINS = ("switch", "input_boolean")


def preheat_lead_time(minutes: float, thawing: bool) -> float:
    """Double the configured preheat lead time when frost requires defrosting."""
    return minutes * 2 if thawing else minutes


class ControlPreheat:
    """Preheating, charging loss calculation and battery level notifications."""

    def preheat_ev(self) -> None:  # noqa: C901
        t = self.t

        def stop_preheat_no_driving(next_drive, now, preheat_min_before):
            limit = preheat_min_before * 3
            return minutes_between(next_drive, now, error_value=limit + 1) > limit and minutes_between(next_drive, now, error_value=limit + 11) <= limit + 10

        if self.testing:
            _LOGGER.debug("TESTING not preheating car")
            return
        if not self.is_ev_configured() or not self.workplan_charging_enabled():
            return
        climate_entity = self.config["ev_car"]["entity_ids"]["climate_entity_id"]
        if not self.is_entity_available(climate_entity):
            return
        if not self.ready_to_charge():
            return
        preheat_min_before = self.number_state("preheat_minutes_before")
        if preheat_min_before is None or preheat_min_before <= 0.0:
            return

        preheat = False
        now = self.now()
        trip_date_time = self.get_trip_date_time()
        next_work_time = reset_datetime()
        try:
            day_name = day_of_week_text(now)
            work_time = self.setting(f"workday_departure_{day_name}")
            if self.switch_state(f"workday_{day_name}") == "on" and work_time is not None:
                next_work_time = now.replace(hour=work_time.hour, minute=work_time.minute, second=0, microsecond=0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(err)
        next_work_time_diff = abs(now - next_work_time)
        trip_date_time_diff = abs(now - trip_date_time)
        next_drive = next_work_time if min(next_work_time_diff, trip_date_time_diff) == next_work_time_diff else trip_date_time

        outdoor_temp = 0.0
        forecast_temp = 0.0
        heating_type = t("ui.preheat_ev.preheating")
        forecast_ids = self.config["forecast"]["entity_ids"]
        try:
            if forecast_ids["outdoor_temp_entity_id"]:
                value = self.get_state(forecast_ids["outdoor_temp_entity_id"], float_type=True, error_state=None)
                if value is None:
                    # unavailable and no value the last day: the forecast temperature decides
                    _LOGGER.debug("Outdoor temperature %s not available, using the forecast", forecast_ids["outdoor_temp_entity_id"])
                else:
                    outdoor_temp = float(value)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant get outdoor temp from entity %s: %s", forecast_ids["outdoor_temp_entity_id"], err)
        try:
            try:
                forecast_temp = float(self.get_attr(forecast_ids["hourly_service_entity_id"], "temperature"))
            except Exception:  # noqa: BLE001
                forecast_temp = float(self.get_attr(forecast_ids["daily_service_entity_id"], "temperature"))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant get forecast temp: %s", err)
        thawing = outdoor_temp <= -1.0 or forecast_temp <= -1.0

        preheat_lead = preheat_lead_time(preheat_min_before, thawing)
        if 0 < minutes_between(now, next_drive, error_value=-1) <= preheat_lead:
            if next_drive == trip_date_time:
                preheat = self.switch_state("trip_preheat") == "on"
            else:
                preheat = self.switch_state(f"preheat_{day_of_week_text(next_drive)}") == "on"

        climate_state = self.get_state(climate_entity, error_state="unknown")
        integration = self.get_integration(climate_entity)
        domain = climate_entity.split(".")[0]
        notify_type = None

        if integration in ("tessie", "cupra_we_connect"):
            # the climate state tells if the car is heating (like the services ev.py called)
            heating_state = "heating" if integration == "cupra_we_connect" else "heat_cool"
            if preheat and climate_state == "off":
                notify_type = "preheat_start"
            elif climate_state == heating_state and stop_preheat_no_driving(next_drive, now, preheat_min_before):
                notify_type = "preheat_cancel"
        elif domain in BUTTON_DOMAINS + SWITCH_DOMAINS or domain == "climate":
            # other cars: the PREHEATING flag decides (ev.py pressed a button or toggled a switch)
            if self.preheating:
                if stop_preheat_no_driving(next_drive, now, preheat_min_before):
                    notify_type = "preheat_cancel"
            elif preheat:
                notify_type = "preheat_start"
        else:
            _LOGGER.warning("Integration %s not supported", integration)
            return

        if notify_type == "preheat_start":
            if thawing:
                heating_type = t("ui.preheat_ev.thawing")
            _LOGGER.info("Preheating ev car (%s)", heating_type)
            self.fire_ev_request("preheat_start", departure=next_drive.strftime("%H:%M"), defrost=thawing)
            self.drive_efficiency("preheat_start")
        elif notify_type == "preheat_cancel":
            _LOGGER.info("Car not moved stopping preheating ev car")
            self.fire_ev_request("preheat_stop", departure=next_drive.strftime("%H:%M"))
            self.drive_efficiency("preheat_cancel")

        if self.config["notification"]["preheating"]:
            if notify_type == "preheat_start":
                self.my_notify(t("ui.preheat_ev.preheat_start", heating_type=heating_type, time=next_drive.strftime("%H:%M")), title=self.title)
            elif notify_type == "preheat_cancel":
                self.my_notify(t("ui.preheat_ev.preheat_cancel", time=next_drive.strftime("%H:%M")), title=self.title)

    def preheat_cron(self) -> None:
        self.preheat_ev()

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

    def notify_set_battery_level(self) -> None:
        if self.is_ev_configured() or self.battery_level() != 0.0:
            return
        data = {"actions": [{"action": "URI", "title": self.t("ui.notify_set_battery_level.action"), "uri": f"entityId:{self.own_entity_id('number', 'battery_level')}"}]}
        self.my_notify(self.t("ui.notify_set_battery_level.message"), title=f"{self.title} {self.t('ui.notify_set_battery_level.title')}", data=data, always=True)

    def notify_battery_under_daily_battery_level(self) -> None:
        if not self.ready_to_charge():
            return
        if self.battery_level() < self.get_min_daily_battery_level() and self.battery_level() != 0.0:
            data = {"actions": [{"action": "URI", "title": self.t("ui.notify_battery_under_daily_battery_level.action"), "uri": f"entityId:{self.own_entity_id('switch', 'forced_charging_daily_battery_level')}"}]}
            self.my_notify(
                f"{self.t('ui.notify_battery_under_daily_battery_level.message')}\n{self.t('ui.notify_battery_under_daily_battery_level.no_charge_expensive_hours')}",
                title=f"{self.title} {self.t('ui.notify_battery_under_daily_battery_level.title')}",
                data=data,
                always=True,
            )
