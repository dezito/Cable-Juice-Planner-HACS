"""Deciding whether to charge right now."""
from __future__ import annotations

import datetime
import logging

from ...const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    CHARGING_ALLOWED_AFTER_GOTO_TIME,
    ENTITY_UNAVAILABLE_STATES,
    EV_PLUGGED_STATES,
    POWERWALL_DISCHARGING_TRIGGER,
)
from ...utils.formatting import emoji_parse
from ...utils.helpers import date_to_string, minutes_between, reset_datetime, round_up

_LOGGER = logging.getLogger(__name__)


class ControlSchedule:
    """Deciding whether to charge right now."""

    def ev_power_connected(self) -> bool:
        if not self.is_ev_configured():
            return True
        ids = self.config["ev_car"]["entity_ids"]
        if not self.is_entity_configured(ids["charge_cable_entity_id"]) and not self.is_entity_configured(ids["charge_port_door_entity_id"]):
            return True
        return self.get_state(ids["charge_cable_entity_id"]) in EV_PLUGGED_STATES or self.get_state(ids["charge_port_door_entity_id"]) in EV_PLUGGED_STATES

    def ready_to_charge(self):  # noqa: C901
        charger = self.config["charger"]["entity_ids"]

        def entity_unavailable(entity_id):
            return True if self.is_entity_configured(entity_id) and not self.is_entity_available(entity_id) else None

        if entity_unavailable(charger["status_entity_id"]) or entity_unavailable(charger["enabled_entity_id"]):
            return None

        charger_status = self.get_state(charger["status_entity_id"], error_state="connected")
        charger_enabled = self.get_state(charger["enabled_entity_id"], error_state="off") if self.is_entity_configured(charger["enabled_entity_id"]) else "on"
        if charger_enabled != "on":
            _LOGGER.warning("Charger was off, turning it on")
            self.start_charging(["enabled"])

        if charger_status in CHARGER_NOT_READY_STATUS:
            _LOGGER.debug("Charger cable disconnected")
            self.set_charging_rule(self.t("ui.ready_to_charge.cable_disconnected"))
            if not self.is_ev_configured() and float(self.number_state("battery_level", error_state=0.0)) != 0.0:
                _LOGGER.info("Reseting battery level to 0.0")
                self.set_setting("battery_level", 0.0)
            return None
        if self.manual_charging_enabled() or self.manual_charging_solar_enabled():
            return True
        if self.is_ev_configured():
            ids = self.config["ev_car"]["entity_ids"]
            ev_charger_connector = self.get_state(ids["charge_cable_entity_id"], error_state="on") if self.is_entity_configured(ids["charge_cable_entity_id"]) else "not_configured"
            if (ev_charger_connector in EV_PLUGGED_STATES or charger_status in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS) and not self.is_ev_home():
                _LOGGER.debug("To long away from home")
                self.set_charging_rule(f"⛔{self.t('ui.ready_to_charge.cable_connected_ev_not_home')}")
                return None
            if ev_charger_connector not in EV_PLUGGED_STATES:
                _LOGGER.debug("Charger cable is Disconnected")
                self.set_charging_rule(f"⚠️{self.t('ui.ready_to_charge.cable_connected_not_to_ev')}")
                self.wake_up_ev()
                return True
            if not self.ev_power_connected():
                _LOGGER.debug("Chargeport not open")
                self.set_charging_rule(f"⚠️{self.t('ui.ready_to_charge.charge_port_closed')}")
                self.wake_up_ev()
                return True
            if entity_unavailable(ids["charge_port_door_entity_id"]) or entity_unavailable(ids["charge_cable_entity_id"]):
                _LOGGER.debug("Chargeport or charge cable entity not available")
                self.set_charging_rule(f"⛔{self.t('ui.ready_to_charge.ev_unavailable')}")
                self.wake_up_ev()
                return None
        return True

    def is_charging(self):  # noqa: C901
        """Check that the charger charges when it should, else stop/start it (restart sequence)."""
        t = self.t
        charger = self.config["charger"]["entity_ids"]

        def reset():
            if self.restarting_charger_count > 0 or charger_enabled != "on":
                self.start_charging(["enabled"])
            self.charging_is_beginning = False
            self.restarting_charger = False
            self.restarting_charger_count = 0

        if self.testing:
            _LOGGER.debug("Not checking if is charging TESTMODE")
            return None

        charger_enabled = self.get_state(charger["enabled_entity_id"], error_state="off")
        charger_status = self.get_state(charger["status_entity_id"], error_state="unavailable")
        data = {"ttl": 0, "priority": "high", "channel": "alarm_stream"} if self.restarting_charger_count == 3 else {}
        emoji_charging_problem = f"{emoji_parse({'charging_problem': True})}"
        emoji_charging_error = f"{emoji_parse({'charging_error': True})}"
        db = self.charging_history_db

        if charger_status in CHARGER_COMPLETED_STATUS or charger_status in CHARGER_CHARGING_STATUS:
            if self.restarting_charger_count > 0:
                _LOGGER.info("Ev is charging as it should again")
                if self.current_charging_session["start"]:
                    when = self.current_charging_session["start"]
                    if 1 < self.restarting_charger_count < 3 and emoji_charging_error not in db[when]["emoji"]:
                        db[when]["emoji"] = db[when]["emoji"].replace(emoji_charging_error, emoji_charging_problem)
                        self.charging_history_combine_and_set()
                self.my_notify(t("ui.is_charging.charging_again"), title=f"{self.title} {t('ui.is_charging.charging_title')}", data=data, always=True, persistent_notification=True)
            reset()
            return True if charger_status in CHARGER_CHARGING_STATUS else None

        if not self.ready_to_charge() or not self.current_charging_session["start"]:
            reset()
            return None

        when = self.current_charging_session["start"]
        if self.restarting_charger_count == 0 and minutes_between(self.now(), when, error_value=self.config["cron_interval"] + 5) <= self.config["cron_interval"] * 2:
            return None

        current_charging_amps = sum(self.current_charging_amps)
        dynamic_charger_limit = sum(self.current_charging_amps)
        try:
            if self.restarting_charger:
                message = f"{t('ui.is_charging.charging_failed')}\n{t('ui.is_charging.charger_restart_count', count=self.restarting_charger_count)}"
                self.set_charging_rule(f"⛔{message}")
                _LOGGER.warning("Starting charging (attempts %s): Starting charging again", self.restarting_charger_count)
                self.my_notify(message, title=f"{self.title} {t('ui.is_charging.fail_title')}", data=data, always=True, persistent_notification=True)
                self.start_charging(force=True)
                self.restarting_charger = False
            elif charger_status in ENTITY_UNAVAILABLE_STATES:
                self.set_charging_rule(f"⛔{t('ui.is_charging.charging_failed')}\n{t('ui.is_charging.charger_unavailable')}")
                raise ValueError(f"Charger is unavailable, entity id: {charger['status_entity_id']}")
            elif charger_status == "error":
                self.set_charging_rule(f"⛔{t('ui.is_charging.charging_failed')}\n{t('ui.is_charging.charger_error')}")
                raise ValueError("Charger critical error, check mobile app or Home Assistant")
            elif charger_status in CHARGER_READY_STATUS:
                if current_charging_amps == 0:
                    if self.charging_is_beginning or self.restarting_charger or self.restarting_charger_count or charger_enabled != "on":
                        reset()
                elif self.restarting_charger_count == 3:
                    self.set_charging_rule(f"⛔{t('ui.is_charging.charging_failed')}")
                    raise ValueError("Charging has not started even after restarting multiple times")
                elif self.restarting_charger_count < 3:
                    if current_charging_amps != dynamic_charger_limit:
                        self.set_charging_rule(f"⛔{t('ui.is_charging.charging_failed')}\n{t('ui.is_charging.charger_dynamic_failed')}")
                        raise ValueError("Chargers dynamic circuit limit not set")
                    if self.charging_is_beginning:
                        self.set_charging_rule(f"⛔{t('ui.is_charging.charging_failed')}\n{t('ui.is_charging.charger_restart')}")
                        raise ValueError("Ev is not charging, restarting charger")
                    self.charging_is_beginning = True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Charging has not started as expected: %s", err)
            if 1 < self.restarting_charger_count < 3:
                if emoji_charging_problem not in db[when]["emoji"]:
                    db[when]["emoji"] = db[when]["emoji"] + f" {emoji_charging_problem}"
                    self.charging_history_combine_and_set()
            elif self.restarting_charger_count == 3:
                if emoji_charging_problem in db[when]["emoji"]:
                    db[when]["emoji"] = db[when]["emoji"].replace(emoji_charging_problem, emoji_charging_error)
                    self.charging_history_combine_and_set()
            restarting = ""
            if not self.restarting_charger:
                self.restarting_charger = True
                self.restarting_charger_count += 1
                if self.is_entity_configured(charger["enabled_entity_id"]):
                    _LOGGER.warning("Restarting charger (attempts %s): Stopping charger for now", self.restarting_charger_count)
                    restarting = f"\n{t('ui.is_charging.charger_restart_count', count=self.restarting_charger_count)}"
                    self.stop_charging(["enabled"], force=True)
                else:
                    _LOGGER.warning("Restarting charging (attempts %s): Stopping charging for now", self.restarting_charger_count)
                    restarting = f"\n{t('ui.is_charging.ev_restart_count', count=self.restarting_charger_count)}"
                    self.stop_charging(force=True)
            self.my_notify(f"{t('ui.is_charging.ev_not_charging')}:\n{err}{restarting}", title=f"{self.title} {t('ui.is_charging.fail_title')}", data=data, always=True, persistent_notification=True)
        return None

    def charging_without_rule(self) -> bool | None:
        minutes = max(5, int(round_up(self.config["cron_interval"] / 2)))
        if self.now().minute < minutes or self.testing:
            return False
        now = self.now()
        past = now - datetime.timedelta(minutes=minutes)
        entity_id = self.config["charger"]["entity_ids"]["power_consumtion_entity_id"]
        power = abs(float(self.get_state(entity_id, float_type=True, error_state=0.0)))
        power_avg = round(abs(float(self.history.get_average_value(entity_id, past, now, convert_to="W", error_state=0.0))), 3)
        trigger_count = 3
        voltage = self.config["charger"]["power_voltage"]
        if power > voltage and power_avg > voltage:
            if self.charging_no_rule_count > 0:
                self.stop_charging(force=self.charging_no_rule_count >= 2)
            if self.charging_no_rule_count > trigger_count:
                if not self.current_charging_session["start"]:
                    self.charging_history({"Price": self.get_current_hour_price(), "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "no_rule": True}, "no_rule")
                self.set_charging_rule(f"{emoji_parse({'no_rule': True})}{self.t('ui.charging_without_rule.charging_no_rule', watt=int(power_avg))}")
                _LOGGER.warning("Charging without rule for %s power: %sW, power_avg: %sW %s times", entity_id, power, power_avg, self.charging_no_rule_count)
                if self.charging_no_rule_count == trigger_count + 1:
                    unavailable_entities = self.get_all_unavailable_entities()
                    if unavailable_entities:
                        lines = [f"\n\n{self.t('ui.charging_without_rule.entities_not_available')}:", *(f"\n- {entity}" for entity in unavailable_entities)]
                        self.my_notify(
                            f"{self.t('ui.charging_without_rule.check_options')}{''.join(lines)}",
                            title=f"{self.title} {self.t('ui.charging_without_rule.ev_charging_no_rule')}",
                            always=True,
                            persistent_notification=True,
                            persistent_notification_id=f"{self.name}_charging_without_rule",
                        )
                integration = self.get_integration(entity_id)
                if integration:
                    self.persistent_notification(
                        f"⛔{self.t('ui.charging_without_rule.restart_integration', entity_id=entity_id, integration=integration.capitalize())}",
                        title=f"{self.title} {self.t('ui.charging_without_rule.ev_charging_no_rule')}",
                        notification_id=f"{self.name}_charging_without_rule_{entity_id}",
                    )
                return True
            self.charging_no_rule_count += 1
        else:
            self.charging_no_rule_count = 0
        return False

    def other_ev_connected(self) -> bool:
        charger_status = self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], error_state="disconnected")
        if charger_status not in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS:
            return False
        for entity_id in self.config["charger"]["entity_ids"]["other_ev_using_this_charger_entity_ids"]:
            if self.get_state(entity_id, try_history=False, error_state="off") in EV_PLUGGED_STATES:
                return True
        return False

    def current_hour_in_charge_hours(self):
        current_hour = self.current_hour()
        for timestamp in self.charge_hours:
            if isinstance(timestamp, datetime.datetime) and timestamp.replace(minute=0, second=0, microsecond=0) == current_hour:
                return timestamp
        return False

    def charge_if_needed(self, force_recalculate=False):
        """Plan and set the action entities, then publish the mode and the heartbeat.

        Attributes of sensor.<prefix>_charger_action for the user's automations:
        mode    normal / error (error: charge with max amps, like ev.py)
        updated time of the last calculation; an automation can fall back to charging
                when it gets old (the integration stopped or hangs)
        """
        self.charger_mode = "normal"
        try:
            self.is_battery_fully_charged()
            return self._charge_if_needed(force_recalculate)
        finally:
            self.publish_site()
            self.set_attr("charger_action", "mode", self.charger_mode)
            self.set_attr("charger_action", "updated", self.now().replace(tzinfo=self._snap.tz).isoformat() if self._snap else None)

    def _charge_if_needed(self, force_recalculate=False):  # noqa: C901
        t = self.t
        config = self.config
        max_amps = self._max_amps
        self.rule_set_this_run = False
        try:
            charging_rule = None
            trip_date_time = self.get_trip_date_time() if self.get_trip_date_time() != reset_datetime() else reset_datetime()
            if self.is_trip_planned():
                if trip_date_time != reset_datetime() and minutes_between(self.now(), trip_date_time, error_value=0) < CHARGING_ALLOWED_AFTER_GOTO_TIME:
                    _LOGGER.info("Trip date %s exceeded by an hour. Reseting trip settings", trip_date_time)
                    self.trip_reset()

            # ev.py ran these two as parallel tasks; the short one usually finished first
            inverter_watt, inverter_watt_solar_only = self.max_local_energy_available_remaining_period()
            self.cheap_grid_charge_hours(force_recalculate)

            if self.other_ev_connected():
                self.set_charging_rule(f"⛔{t('ui.charge_if_needed.other_ev_connected')}")
                return None

            inverter_amps = self.calc_charging_amps(inverter_watt, max_allowed=config["solar"]["inverter_discharging_power_limit"])[:-1]

            powerwall_discharge_watt = 0.0
            if self.is_powerwall_configured():
                discharge_above_needed = self.get_powerwall_discharge_above_needed()
                powerwall_battery_level = self.get_powerwall_battery_level()
                powerwall_reserved_battery_level = self.get_ev_charge_after_powerwall_battery_level()
                powerwall_discharging_consumption = self.power_values(period=max(config["cron_interval"] * 2, 20))["powerwall_discharging_consumption"]
                powerwall_discharge_watt = powerwall_discharging_consumption
                if discharge_above_needed and powerwall_battery_level > powerwall_reserved_battery_level + 1.0 and powerwall_discharging_consumption < POWERWALL_DISCHARGING_TRIGGER:
                    powerwall_discharge_watt = config["solar"]["powerwall_discharging_power"]

            current_hour = self.current_hour()
            current_price = self.get_current_hour_price()
            charging_limit = min(self.range_to_battery_level(), self.get_max_recommended_charge_limit_battery_level())
            circuit_amps = inverter_amps if sum(inverter_amps) > 0.0 else max_amps()
            charger_amps = [0.0, 0.0, 0.0]
            charge_hour = self.current_hour_in_charge_hours()
            solar_using_grid_price = False
            also_cheap_power = ""

            if sum(inverter_amps) != 0.0:
                charging_limit = self.get_max_recommended_charge_limit_battery_level()
                if self.is_solar_configured():
                    solar_using_grid_price = float(config["solar"]["production_price"]) == -1.0
                if charge_hour:
                    hour_data = self.charge_hours[charge_hour]
                    circuit_amps = max_amps()
                    charger_amps = max_amps()
                    if "half_min_avg_price" in hour_data:
                        ultra = self.get_ultra_cheap_grid_charging_max_battery_level()
                        charging_limit = charging_limit if charging_limit > ultra else ultra
                        also_cheap_power = " + Ultra cheap power"
                    elif "under_min_avg_price" in hour_data:
                        very = self.get_very_cheap_grid_charging_max_battery_level()
                        charging_limit = charging_limit if charging_limit > very else very
                        also_cheap_power = " + Cheap power"
                    else:
                        charging_limit = round_up(self.battery_level() + hour_data["battery_level"])
                        also_cheap_power = " + Grid Charging not enough solar production"
                    charging_limit = min(charging_limit, self.get_max_recommended_charge_limit_battery_level())

            solar = self.is_solar_production_available(inverter_watt_solar_only)
            powerwall = self.is_powerwall_discharging(powerwall_discharge_watt)

            if self.no_charging_modes_active():
                _LOGGER.debug("No charging modes active, setting amps to max")
                charging_limit = self.get_max_recommended_charge_limit_battery_level()
                circuit_amps = max_amps()
                charger_amps = max_amps()
                charging_rule = f"⛔{t('ui.charge_if_needed.no_charging_modes_active', watt=int(sum(charger_amps) * config['charger']['power_voltage']))}"
                self.charging_history({"Price": self.get_solar_sell_price() if sum(inverter_amps) != 0.0 else current_price, "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "manual": True, "solar": solar}, "deactivate")
            elif self.is_calculating_charging_loss():
                limit_entity = config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
                if self.is_ev_configured() and self.is_entity_configured(limit_entity):
                    completed_battery_level = float(self.get_state(limit_entity, float_type=True, error_state=100.0))
                else:
                    completed_battery_level = self.get_completed_battery_level()
                _LOGGER.info("Calculating charging loss %s%%", completed_battery_level)
                if completed_battery_level < self.get_max_recommended_charge_limit_battery_level():
                    completed_battery_level = self.get_max_recommended_charge_limit_battery_level()
                # the car must charge to the limit the measurement expects
                charging_limit = completed_battery_level
                charging_rule = f"{emoji_parse({'charging_loss': True})}{t('ui.charge_if_needed.charging_loss', percentage=completed_battery_level)}"
                circuit_amps = max_amps()
                charger_amps = max_amps()
                battery = round(completed_battery_level - self.battery_level(), 1)
                kwh = round(self.percentage_to_kwh(battery, include_charging_loss=True))
                cost = round(current_price * kwh, 2)
                self.charging_history({"Price": current_price, "Cost": cost, "kWh": kwh, "battery_level": battery, "charging_loss": True, "solar": solar, "powerwall": powerwall}, "charging_loss")
            elif self.manual_charging_enabled() or self.manual_charging_solar_enabled():
                charging_limit = self.get_max_recommended_charge_limit_battery_level()
                voltage = config["charger"]["power_voltage"]
                if not self.manual_charging_solar_enabled():
                    _LOGGER.debug("Manual charging")
                    circuit_amps = max_amps()
                    charger_amps = max_amps()
                    charging_rule = f"{emoji_parse({'manual': True})}{t('ui.charge_if_needed.manual_charging', watt=int(sum(charger_amps) * voltage))}"
                else:
                    _LOGGER.debug("Manual charging solar only")
                    charger_amps = self.calc_charging_amps(inverter_watt_solar_only, max_allowed=config["solar"]["inverter_discharging_power_limit"])[:-1]
                    circuit_amps = charger_amps
                    charging_rule = f"{emoji_parse({'manual': True, 'solar': True})}{t('ui.charge_if_needed.manual_charging_solar', watt=int(sum(charger_amps) * voltage))}"
                if sum(charger_amps) > 0.0:
                    self.charging_history({"Price": self.get_solar_sell_price() if sum(inverter_amps) != 0.0 else current_price, "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "manual": True, "solar": solar}, "manual")
                else:
                    self.stop_current_charging_session()
            elif self.ready_to_charge():
                if charge_hour:
                    hour_data = self.charge_hours[charge_hour]
                    hour_data["solar"] = self.is_solar_production_available(inverter_watt_solar_only)
                    hour_data["powerwall"] = self.is_powerwall_discharging(powerwall_discharge_watt)
                    self.charging_history(hour_data, "planned")
                    battery_level_plus_charge = self.battery_level() + hour_data["battery_level"]
                    charging_limit = min(round_up(max(battery_level_plus_charge, self.charge_hours["max_charging_level_today"], self.range_to_battery_level())), 100.0)
                    circuit_amps = max_amps()
                    charger_amps = max_amps()
                    emoji = emoji_parse(hour_data)
                    charging_rule = t("ui.charge_if_needed.planned_charging", emoji=emoji)
                    _LOGGER.info(
                        "Charging because of %s %s%s. %sx%samps (%skWh)",
                        emoji, hour_data["Price"], t("ui.common.valuta"), int(config["charger"]["charging_phases"]), hour_data["ChargingAmps"], self.max_kwh_charging,
                    )
                elif self.switch_state("forced_charging_daily_battery_level", error_state="on") == "on" and self.battery_level() < self.get_min_daily_battery_level():
                    if current_hour in self.charge_hours["expensive_hours"]:
                        charging_rule = f"{emoji_parse({'low_battery': True})}{t('ui.charge_if_needed.low_battery_but_expensive')}"
                        _LOGGER.info("Battery under <%s%%, but power is expensive: %s", self.get_min_daily_battery_level(), date_to_string(self.charge_hours["expensive_hours"], format="%H:%M"))
                    else:
                        battery = round(self.get_min_daily_battery_level() - self.battery_level(), 1)
                        kwh = round(self.percentage_to_kwh(battery, include_charging_loss=True))
                        cost = round(current_price * kwh, 2)
                        self.charging_history({"Price": current_price, "Cost": cost, "kWh": kwh, "battery_level": battery, "low_battery": True, "solar": solar, "powerwall": powerwall}, "low_battery")
                        charging_limit = self.get_min_daily_battery_level()
                        circuit_amps = max_amps()
                        charger_amps = max_amps()
                        charging_rule = f"{emoji_parse({'low_battery': True})}{t('ui.charge_if_needed.low_battery', percentage=self.get_min_daily_battery_level())}"
                        _LOGGER.info("Charging because of under <%s%%", self.get_min_daily_battery_level())
                elif self.solar_charging_enabled() and sum(inverter_amps) != 0.0:
                    if self.battery_level() < self.get_max_recommended_charge_limit_battery_level():
                        if config["solar"]["enable_selling_during_expensive_hours"] and solar_using_grid_price and current_hour in self.charge_hours["expensive_hours"] and solar:
                            charging_rule = t("ui.charge_if_needed.solar_production_but_expensive", price=round(self.get_solar_sell_price(), 2))
                            _LOGGER.info("Ignoring solar overproduction, because of expensive hour")
                            inverter_amps = [0.0, 0.0, 0.0]
                            charger_amps = inverter_amps
                        else:
                            if charge_hour:
                                hour_data = self.charge_hours[charge_hour]
                                hour_data["solar"] = solar
                                hour_data["powerwall"] = powerwall
                                self.charging_history(hour_data, "planned")
                            else:
                                self.charging_history({"Price": self.get_solar_sell_price(), "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "solar": solar, "powerwall": powerwall}, "local_energy")
                            solar_string = f"{emoji_parse({'solar': True})}{t('ui.charge_if_needed.solar')}" if solar else ""
                            powerwall_string = f"{emoji_parse({'powerwall': True})}{t('ui.charge_if_needed.powerwall')}" if powerwall else ""
                            local_energy_string = " &".join([solar_string, powerwall_string]) if solar and powerwall else solar_string or powerwall_string
                            charger_amps = inverter_amps
                            charging_rule = t("ui.charge_if_needed.local_energy_charging", local_energy=local_energy_string, watt=int(sum(charger_amps) * config["charger"]["power_voltage"]))
                            _LOGGER.debug("EV solar/powerwall charging at max %s%s", charger_amps, also_cheap_power)
                    else:
                        charging_rule = f"{emoji_parse({'solar': True})}{t('ui.charge_if_needed.local_energy_charging_not_needed', percentage=self.get_max_recommended_charge_limit_battery_level())}"
                        _LOGGER.debug("EV solar charging not needed, battery over %s%%", self.get_max_recommended_charge_limit_battery_level())
                else:
                    if not self.charging_without_rule():
                        self.stop_current_charging_session()
                        _LOGGER.debug("No rules for charging")
                        charging_rule = t("ui.charge_if_needed.not_charging")
            else:
                if not self.charging_without_rule():
                    self.stop_current_charging_session()
                    _LOGGER.debug("EV not ready to charge")

            if self.fill_up_charging_enabled():
                charging_limit = max(charging_limit, self.get_max_recommended_charge_limit_battery_level())

            if sum(charger_amps) > 0.0:
                self.publish_charging_limit(self.verify_charge_limit(charging_limit))
                self.start_charging()
            else:
                self.stop_charging()
            if charging_rule:
                self.set_charging_rule(charging_rule)
            elif not self.rule_set_this_run:
                # no branch set a rule this run, so never keep the previous one (stale emoji)
                self.set_charging_rule(t("ui.charge_if_needed.not_charging"))
            self.set_charger_phase_limits(circuit_amps, charger_amps)
        except Exception as err:  # noqa: BLE001
            self.charger_mode = "error"
            message = f"Error running charge_if_needed(), setting charger and car to max: {err} {type(err)}"
            _LOGGER.exception(message)
            self.persistent_notification(
                f"Error running charge_if_needed(), setting charger and car to max\nTrying to restart script to fix error in {self.error_count}/3: {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_charge_if_needed_restart_count_error",
            )
            if self.error_count == 3:
                _LOGGER.error("Reloading the integration to maybe fix error!!!")
                self.persistent_notification("Reloading the integration to maybe fix error!!!", title=f"{self.title} error", notification_id=f"{self.name}_charge_if_needed_restart_script")
                if self._out is not None:
                    self._out.request_reload = True
            self.error_count += 1
            self.publish_charging_limit(self.verify_charge_limit(self.get_max_recommended_charge_limit_battery_level()))
            self.start_charging()
            self.charging_history({"Price": 0.0, "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "error": True}, "error")
            self.set_charging_rule(f"{emoji_parse({'error': True})}{t('ui.charge_if_needed.error_charging_max')}")
            self.set_charger_phase_limits(max_amps(), max_amps())
        return self.charger_action
