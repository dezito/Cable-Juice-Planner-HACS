"""Startup, triggers and state changes."""
from __future__ import annotations

import datetime
import logging

from ...const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    ENTITY_UNAVAILABLE_STATES,
)
from ...utils.helpers import reset_datetime

_LOGGER = logging.getLogger(__name__)


class ControlEvents:
    """Startup, triggers and state changes."""

    def trip_reset(self) -> None:
        self.set_setting("trip_charge_procent", 0.0)
        self.set_setting("trip_range_needed", 0.0)
        self.set_setting("trip_date_time", None)
        self.set_setting("trip_homecoming_date_time", None)
        if self.is_ev_configured():
            self.set_setting("trip_preheat", False)

    def trips_changed(self, key: str, value) -> None:
        """state_trigger_ev_trips: trip percentage and range exclude each other."""
        if key == "trip_charge_procent":
            if value is None:
                return
            if self.get_trip_target_level() != 0.0:
                self.set_setting("trip_range_needed", 0.0)
        elif key == "trip_range_needed":
            if value is None:
                return
            if self.get_trip_range() != 0.0:
                self.set_setting("trip_charge_procent", 0.0)
        elif key == "trip_date_time":
            value = value if value is not None else reset_datetime()
            homecoming = self.get_trip_homecoming_date_time()
            if value != reset_datetime() and (homecoming == reset_datetime() or homecoming < value):
                self.set_setting("trip_homecoming_date_time", self.end_of_day(value))
        elif key == "trip_homecoming_date_time":
            value = value if value is not None else reset_datetime()
            trip = self.get_trip_date_time()
            if value != reset_datetime() and (trip == reset_datetime() or trip > value):
                self.set_setting("trip_date_time", value - datetime.timedelta(hours=1))
        elif key == "trip_reset":
            self.trip_reset()
        self.last_trip_change_datetime = self.now()

    def startup(self, databases: dict, odometer_history: dict | None = None) -> dict:
        """Load the databases and the runtime state and calculate the first plan."""
        t = self.t
        runtime = databases.get("runtime") or {}
        for key, value in (runtime.get("sensors") or {}).items():
            self.sensor_states.setdefault(key, value)
        full_charge_99_since = runtime.get("full_charge_99_since")
        self.full_charge_99_since = full_charge_99_since if isinstance(full_charge_99_since, datetime.datetime) else None
        if runtime.get("last_battery_level_attributes"):
            self.sensor_attrs.setdefault("drive_efficiency_last_battery_level", {}).update(runtime["last_battery_level_attributes"])
        if isinstance(runtime.get("charging_loss"), (int, float)):
            self.config["charger"]["charging_loss"] = runtime["charging_loss"]
        self.daily_odometer = {str(date): float(value) for date, value in (runtime.get("daily_odometer") or {}).items()}
        if (measurement := runtime.get("charging_loss_measurement")) and self.is_calculating_charging_loss():
            # a measurement in progress survives a restart or reload
            self.charging_loss_car_begin_battery_level = measurement.get("car_begin_battery_level", 0.0)
            self.charging_loss_car_begin_kwh = measurement.get("car_begin_kwh", 0.0)
            self.charging_loss_charger_begin_kwh = measurement.get("charger_begin_kwh", 0.0)
            self.charging_loss_charging_completed = bool(measurement.get("charging_completed"))
        for key in ("co2_emitted", "kwh_charged_by_solar"):
            if self.sensor_states.get(key) is None:
                self.sensor_states[key] = 0.0
            self._changed_sensors.add(key)
        self._changed_sensors.add("last_full_charge")
        if self.is_ev_configured():
            # startup_public_charging_session_done
            self.sensor_states["public_charging_session_done"] = runtime.get("public_charging_session_done") or "off"
            self.public_charging_sessions = list(runtime.get("public_charging_session") or [])
            if self.sensor_states["public_charging_session_done"] == "on" and not self.public_charging_sessions:
                self.sensor_states["public_charging_session_done"] = "off"
            if self.public_charging_sessions:
                self.sensor_attrs.setdefault("public_charging_session_done", {})["public_charging_session"] = [dict(s) for s in self.public_charging_sessions]
            self._changed_sensors.add("public_charging_session_done")

        self.set_charging_rule(f"📟{t('ui.startup.setting_entities')}")
        self.set_entity_friendlynames()
        self.emoji_description()
        self.set_default_entity_states()
        self._publish_charger_action()

        self.set_charging_rule(f"📟{t('ui.startup.loading_db')}")
        self.load_power_values_db(databases.get("power_values"))
        self.load_solar_available_db(databases.get("solar_available"))
        # ev.py loaded the price history before the price provider existed
        provider, self.price_provider = self.price_provider, None
        try:
            self.load_kwh_prices(databases.get("kwh_avg_prices"))
        finally:
            self.price_provider = provider
        self.load_drive_efficiency(databases.get("drive_efficiency"))
        self.load_km_kwh_efficiency(databases.get("km_kwh_efficiency"))

        self.update_grid_prices()
        self.load_charging_history(databases.get("charging_history"), odometer_history)
        self.update_learned_distances()
        self.load_charging_loss_db(databases.get("charging_loss"))
        self.load_economy_db(databases.get("economy"))
        self.publish_economy()
        self.load_analysis_db(databases.get("analysis"))
        if self.is_ev_configured():
            self.publish_analysis()
        self.publish_charging_loss_model()

        self.solar_charged_percentage()
        self.is_battery_fully_charged()
        self.set_estimated_range()
        self.calc_kwh_price(self.now().minute, update_entities=True)

        distance_per_percent = self.distance_per_percentage()
        info = {
            "charger_configured": self.is_charger_configured(),
            "ev_configured": self.is_ev_configured(),
            "solar_configured": self.is_solar_configured(),
            "powerwall_configured": self.is_powerwall_configured(),
            "range": self.battery_range(),
            "battery_level": self.battery_level(),
            "distance_per_percentage": distance_per_percent,
            "estimated_range": self.battery_level() * distance_per_percent,
            "amps_report": self.calc_charging_amps(0.0, report=True),
        }
        _LOGGER.info(
            "Loaded databases: charging history %s entries, price history %s days, drive efficiency %s drives",
            len(self.charging_history_db), len(self.kwh_avg_prices_db.get("mean", [])), len(self.drive_efficiency_db),
        )

        self.set_charging_rule(f"📟{t('ui.startup.calc_charging_plan')}")
        self.charging_rule_changed("startup")
        # same order as the tasks startup() created
        self.charge_if_needed()
        self.preheat_ev()
        try:
            self.drive_efficiency_save_car_stats(bootup=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("drive_efficiency_save_car_stats(bootup=True) failed: %s %s", err, type(err))
        self.append_overview_output(f"📟{self.title} started")
        # save everything once, so databases imported from the PyScript YAML files are kept
        self._dirty.update(self._databases())
        return info

    def cron(self) -> None:
        """triggers_charge_if_needed (time trigger every cron_interval minutes)."""
        self.calc_kwh_price(self.now().minute, update_entities=True)
        self.charge_trigger()

    def charge_trigger(self, var_name: str | None = None) -> None:
        """triggers_charge_if_needed for the cron and the enforce/manual/charging loss entities."""
        if self.is_charging() and not self.other_ev_connected():
            self.wake_up_ev()
            if not self.is_ev_configured():
                self.charging_power_to_emulated_battery_level()
        force = var_name == "enforce_planning"
        self.charge_if_needed(force_recalculate=force)
        self.is_battery_fully_charged()
        if self.is_ev_configured():
            self.analysis_check_departure()
        self.set_estimated_range()
        if var_name == "enforce_planning" and self.testing:
            self.debug_info()

    def recalculate(self) -> None:
        """A setting changed: recalculate the plan (forced, the plan hash ignores most settings)."""
        self.charge_if_needed(force_recalculate=True)
        self.set_estimated_range()
        # use_measured_charging_loss may have changed what the plan uses
        self.publish_charging_loss_model()

    def hour_end(self) -> None:
        """cron_hour_end (minute 59)."""
        local_energy = self.local_energy_available(period=60, without_all_exclusion=True, solar_only=True)
        power_values = self.power_values(period=60)
        self.solar_available_append_to_db(local_energy)
        self.power_values_to_db(power_values)
        self.stop_current_charging_session()
        self.kwh_charged_by_solar()
        self.solar_charged_percentage()
        self.record_daily_odometer()
        self.analysis_record_planned()
        self.analysis_record_prices()
        self.economy_store_prices()

    def price_update(self) -> None:
        self.update_grid_prices()

    def append_prices(self) -> None:
        self.append_kwh_prices()

    def charging_rule_changed(self, key: str, value=None) -> None:
        """fill_up and workplan_charging exclude each other."""
        t = self.t
        conflict_title = f"📟{self.title} {t('ui.charging_rule_changed.charging_rule_conflict')}"
        if key == "startup":
            if self.fill_up_charging_enabled() and self.workplan_charging_enabled():
                self.set_setting("fill_up", False)
                self.persistent_notification(
                    f"❗{t('ui.charging_rule_changed.deactivate_optimal_rule')}\n\n📎{t('ui.charging_rule_changed.reason')}",
                    title=conflict_title,
                    notification_id=f"{self.name}_charging_rule_changed_conflict",
                )
            return
        if value:
            if key == "fill_up" and self.workplan_charging_enabled():
                self.set_setting("workplan_charging", False)
                self.persistent_notification(
                    f"❗{t('ui.charging_rule_changed.deactivate_workplan_rule')}\n\n📎{t('ui.charging_rule_changed.reason')}",
                    title=conflict_title,
                    notification_id=f"{self.name}_charging_rule_changed_conflict",
                )
            elif key == "workplan_charging" and self.fill_up_charging_enabled():
                self.set_setting("fill_up", False)
                self.persistent_notification(
                    f"❗{t('ui.charging_rule_changed.deactivate_optimal_rule')}\n\n📎{t('ui.charging_rule_changed.reason')}",
                    title=conflict_title,
                    notification_id=f"{self.name}_charging_rule_changed_conflict",
                )

    def charger_status_changed(self, value, old_value) -> str | None:
        """state_trigger_charger_port, first part. Returns 'wait' when the coordinator
        should wait for the car (odometer/battery level) and then call charger_connected."""
        if old_value in ENTITY_UNAVAILABLE_STATES or value in ENTITY_UNAVAILABLE_STATES:
            return None
        if self.other_ev_connected():
            return None
        _LOGGER.info("Charger port status changed from %s to %s", old_value, value)
        if value in CHARGER_READY_STATUS and old_value in CHARGER_NOT_READY_STATUS:
            self.economy_plugged_in()
            self.set_setting("allow_manual_charging_now", False)
            self.set_setting("allow_manual_charging_solar", False)
            self.set_setting("forced_charging_daily_battery_level", False)
            self.wake_up_ev()
            self.notify_set_battery_level()
            return "wait"
        if value in CHARGER_NOT_READY_STATUS:
            # the charger is at home: the connection window ends wherever the car says it is
            self.economy_unplugged()
        if not self.is_ev_home():
            return None
        if value in CHARGER_NOT_READY_STATUS:
            self.stop_current_charging_session()
            self.wake_up_ev()
            self.power_connected_trigger(value)
        elif old_value in CHARGER_CHARGING_STATUS and value in CHARGER_COMPLETED_STATUS:
            if not self.is_ev_configured() and self.current_charging_session["start"]:
                self.stop_current_charging_session()
                self.set_setting("battery_level", self.get_completed_battery_level())
        return None

    def charger_connected(self, value) -> None:
        """state_trigger_charger_port, after waiting for the car."""
        if not self.is_ev_home():
            return
        self.power_connected_trigger(value)
        self.charge_if_needed()

    def power_connected_trigger(self, value) -> None:
        self.drive_efficiency(str(value))
        self.notify_battery_under_daily_battery_level()

    def charger_cable_changed(self, value, old_value) -> None:
        """state_trigger_charger_cable_connected (charger without a status entity)."""
        if not self.is_ev_configured() or old_value in ENTITY_UNAVAILABLE_STATES or value in ENTITY_UNAVAILABLE_STATES:
            return
        if self.other_ev_connected():
            return
        _LOGGER.info("Charger cable connected changed from %s to %s", old_value, value)
        self.wake_up_ev()
        ids = self.config["ev_car"]["entity_ids"]
        if not self.is_entity_configured(ids["charge_port_door_entity_id"]) and not self.is_entity_configured(ids["charge_cable_entity_id"]):
            self.power_connected_trigger(value)

    def ev_port_changed(self, value, old_value) -> None:
        """state_trigger_ev_charger_port / _cable."""
        if old_value in ENTITY_UNAVAILABLE_STATES or value in ENTITY_UNAVAILABLE_STATES:
            return
        _LOGGER.info("Ev charge port/cable changed from %s to %s", old_value, value)
        self.power_connected_trigger(value)
        self.public_charging_session(value)

    def emulated_battery_level_changed(self, value, old_value) -> None:
        try:
            old_value = float(old_value)
            value = float(value)
        except (TypeError, ValueError):
            return
        if old_value == 0.0 and value > 0.0:
            self.power_connected_trigger("on")

    def public_charging_cost_changed(self, value) -> str | None:
        """state_trigger_public_charging_session_cost. 'add' = wait 60 s and add to history."""
        if value is None or value == 0.0:
            return None
        if not self.get_public_charging_session_done() or not self.public_charging_sessions or value == -2.0:
            self.set_setting("public_charging_session_cost", 0.0)
            return None
        return "add"

    def add_public_charging_session(self) -> None:
        self.public_charging_session("add_to_history")
