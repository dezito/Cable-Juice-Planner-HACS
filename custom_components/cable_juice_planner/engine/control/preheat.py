"""Preheating the car before the next departure."""
from __future__ import annotations

import logging

from ...utils.helpers import day_of_week_text, minutes_between, reset_datetime

_LOGGER = logging.getLogger(__name__)

BUTTON_DOMAINS = ("button", "input_button")
SWITCH_DOMAINS = ("switch", "input_boolean")


def preheat_lead_time(minutes: float, thawing: bool) -> float:
    """Double the configured preheat lead time when frost requires defrosting."""
    return minutes * 2 if thawing else minutes


class ControlPreheat:
    """Preheating the car before the next departure."""

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
