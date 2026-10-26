"""What to do right now, triggers, preheating and charging loss (engine mixin).

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

import datetime
import logging

from .const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    CHARGING_ALLOWED_AFTER_GOTO_TIME,
    CHARGING_LOSS_MAX,
    CHARGING_LOSS_MIN_BATTERY_LEVEL,
    ENTITY_UNAVAILABLE_STATES,
    EV_PLUGGED_STATES,
    POWERWALL_DISCHARGING_TRIGGER,
)
from .formatting import emoji_parse
from .helpers import (
    date_to_string,
    day_of_week_text,
    get_closest_key,
    minutes_between,
    reset_datetime,
    round_up,
    to_datetime,
)

_LOGGER = logging.getLogger(__name__)

BUTTON_DOMAINS = ("button", "input_button")
SWITCH_DOMAINS = ("switch", "input_boolean")


class ControlMixin:
    """charge_if_needed and the event handlers of ev.py."""

    # ------------------------------------------------------------------ #
    # Charging amps
    # ------------------------------------------------------------------ #

    def charging_power_dict(self) -> dict[float, dict]:
        """watt -> {amp, phase, watt}: 1 phase from the single phase min to max amps, then all
        phases from the three phase min amps where that is more than 1 phase can give."""
        solar = self.config["solar"]
        charger = self.config["charger"]
        power_dict = {}
        if float(solar["charging_single_phase_max_amp"]) > 0.0:
            for i in range(int(solar["charging_single_phase_min_amp"]), int(solar["charging_single_phase_max_amp"]) + 1):
                watt = float(i) * charger["power_voltage"]
                power_dict[watt] = {"amp": float(i), "phase": 1.0, "watt": watt}
        max_single_phase_watt = max(power_dict.keys()) if power_dict else 0.0
        for i in range(int(solar["charging_three_phase_min_amp"]), int(charger["charging_max_amp"]) + 1):
            watt = float(i) * (charger["power_voltage"] * charger["charging_phases"])
            if max_single_phase_watt < watt or max_single_phase_watt == 0.0:
                power_dict[watt] = {"amp": float(i), "phase": charger["charging_phases"], "watt": watt}
        return power_dict

    def calc_charging_amps(self, power=0.0, max_allowed=None, report=False):
        config = self.config
        solar = config["solar"]
        charger = config["charger"]
        power = float(power)
        power_dict = self.charging_power_dict()
        min_watt = min(power_dict.keys()) - 50.0
        power_dict[min_watt] = {"amp": 0.0, "phase": charger["charging_phases"], "watt": 0.0}
        if max_allowed is not None and min_watt > max_allowed:
            _LOGGER.warning("Max allowed watt %s is lower than minimum watt in powerDict %s, setting max allowed to %s", max_allowed, min_watt, min_watt)
            max_allowed = min_watt

        if report:
            lines = []
            if self.is_solar_configured():
                allow = solar["allow_grid_charging_above_solar_available"]
                lines.append("### Charging amps ###")
                lines.append(f"Voltage above(+)/under(-) overproduction available: {allow}W")
                lines.append("Solar overproduction examples:")
                for n, base in enumerate((500, 1500, 3000, 4000, 5000), start=1):
                    example = base + allow
                    lines.append(f"{n}. overproduction {base}W + {allow}W={example}W {get_closest_key(example, power_dict)}{chr(10) if n == 5 else ''}")
                lines.append("Solar overproduction charging:")
                first = "<"
                for key in sorted(power_dict):
                    lines.append(f"{first}{int(key)} W: {int(power_dict[key]['amp'])} A {int(power_dict[key]['phase'])} phase = {int(power_dict[key]['watt'])} W")
                    first = ""
            else:
                lines.append(f"Max charging: {charger['charging_max_amp']}A {charger['charging_phases']} phase = {self.max_watt_charging}W")
            return "\n".join(lines)

        output = get_closest_key(power, power_dict, max_allowed=max_allowed)
        phase_1 = phase_2 = phase_3 = 0
        if output["phase"] >= 1.0:
            phase_1 = output["amp"]
        if output["phase"] >= 2.0:
            phase_2 = output["amp"]
        if output["phase"] == 3.0:
            phase_3 = output["amp"]
        return [phase_1, phase_2, phase_3, output["watt"]]

    def _max_amps(self) -> list:
        charger = self.config["charger"]
        return [charger["charging_max_amp"]] * int(charger["charging_phases"])

    def set_sensor_charging_amps(self, phase_1=0, phase_2=0, phase_3=0, watt=0.0) -> None:
        self.set_state("charger_phase_1_amps", phase_1)
        self.set_state("charger_phase_2_amps", phase_2)
        self.set_state("charger_phase_3_amps", phase_3)
        self.set_state("charging_watts", watt)
        # also on the action sensor: an automation only needs that one entity
        for i, value in enumerate((phase_1, phase_2, phase_3), start=1):
            self.set_attr("charger_action", f"phase_{i}_amps", int(value))

    def set_charger_phase_limits(self, circuit_amps=None, charger_amps=None) -> None:
        circuit_amps = self._max_amps() if circuit_amps is None else circuit_amps
        charger_amps = self._max_amps() if charger_amps is None else charger_amps
        if self.site_active():
            circuit_amps, charger_amps = self._apply_site_limit(list(circuit_amps), list(charger_amps))
        for entity_id in self.config["charger"]["entity_ids"].values():
            if isinstance(entity_id, str) and self.is_entity_configured(entity_id):
                self.is_entity_available(entity_id)
        self.set_circuit_phase_limits(*circuit_amps)
        self.set_charger_charging_amps(*charger_amps)

    def _apply_site_limit(self, circuit_amps: list, charger_amps: list) -> tuple[list, list]:
        """Load balancing: never ask for more than the site has left for this car."""
        circuit_amps = (circuit_amps + [0, 0, 0])[:3]
        charger_amps = (charger_amps + [0, 0, 0])[:3]
        self.requested_circuit_amps = list(circuit_amps)
        self.requested_charger_amps = list(charger_amps)
        limited = self.site_clamp(charger_amps)
        text = self.site_text(charger_amps, limited)
        if limited != [int(a) for a in charger_amps]:
            _LOGGER.info("Site limit: %s A -> %s A per phase", [int(a) for a in charger_amps], limited)
            circuit_amps = [min(int(c), a) if a > 0 else 0 for c, a in zip(circuit_amps, limited)]
            if sum(charger_amps) > 0 and sum(limited) <= 0 and self.charger_action == "charge":
                self.stop_charging()
        self.set_attr("charger_action", "site_limited", bool(text))
        self.set_attr("charger_action", "site_available", [int(a) for a in self._site().get("live", [0, 0, 0])])
        if text != self.site_charging_text:
            self.site_charging_text = text
            if self.last_charging_rule_text:
                self.set_charging_rule(self.last_charging_rule_text, fresh=False)
        return circuit_amps, limited

    def set_circuit_phase_limits(self, phase_1=0, phase_2=0, phase_3=0) -> None:
        """The circuit limit for chargers with dynamic circuit limits (Easee master)."""
        phases = [int(phase_1), int(phase_2), int(phase_3)]
        if phases != self.current_circuit_amps:
            _LOGGER.debug("Circuit phase limits %s/%s/%s", *phases)
        self.current_circuit_amps = phases
        for i, value in enumerate(phases, start=1):
            self.set_attr("charger_action", f"circuit_phase_{i}_amps", value)

    def set_charger_charging_amps(self, phase_1=0, phase_2=0, phase_3=0, watt=0.0) -> None:
        phases = [int(phase_1), int(phase_2), int(phase_3)]
        watt = sum(phases) * self.config["charger"]["power_voltage"]
        self.set_sensor_charging_amps(*phases, watt)
        if phases != self.current_charging_amps:
            _LOGGER.info("Charger amps %s/%s/%s (%sW)", *phases, watt)
        self.current_charging_amps = phases

    # ------------------------------------------------------------------ #
    # Charger / car actions (replace the commands of the PyScript version)
    # ------------------------------------------------------------------ #

    def _publish_charger_action(self) -> None:
        self.set_state("charger_action", self.charger_action)
        self.set_attr("charger_action", "charger_enabled", self.charger_enabled)

    def start_charging(self, entities=None, force=False) -> None:
        """Charge (or with entities=['enabled']: only enable the charger)."""
        if entities == ["enabled"]:
            self.charger_enabled = True
        else:
            if self.charger_action != "charge":
                _LOGGER.info("Charger action: charge")
            self.charger_action = "charge"
            self.charger_enabled = True
        self._publish_charger_action()

    def stop_charging(self, entities=None, force=False) -> None:
        """Stop charging (with entities=['enabled']: disable the charger)."""
        if entities == ["enabled"]:
            self.charger_enabled = False
        else:
            if self.charger_action != "stop":
                _LOGGER.info("Charger action: stop")
            self.charger_action = "stop"
        self._publish_charger_action()

    def publish_charging_limit(self, value) -> None:
        """Publish the wanted charging limit of the car when it can be charged.

        Nothing is sent to the car integration: the user's automation (the charger
        blueprint) sets the limit from sensor.<prefix>_charging_limit.
        """
        if not self.is_ev_configured():
            return
        if not self.ready_to_charge():
            return
        if self.sensor_states.get("charging_limit") != value:
            _LOGGER.info("Car charging limit: %s", value)
        self.set_state("charging_limit", value)
        # also on the action sensor, so the charger automation only needs that entity
        self.set_attr("charger_action", "charging_limit", value)

    def wake_up_ev(self) -> None:
        if not self.is_ev_configured():
            return
        if minutes_between(self.last_wake_up_datetime, self.now()) <= 14:
            _LOGGER.debug("Wake up call already called")
            return
        last_update_entity = self.config["ev_car"]["entity_ids"]["last_update_entity_id"]
        if self.is_entity_available(last_update_entity):
            last_update = to_datetime(self.get_state(last_update_entity, error_state=reset_datetime()))
            if minutes_between(last_update, self.now()) <= 14:
                _LOGGER.debug("Ev already updated, skipping wake up call")
                return
        self.last_wake_up_datetime = self.now()
        self.fire_ev_request("wake_up")

    # ------------------------------------------------------------------ #
    # State of the car and charger
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Trips
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # charge_if_needed
    # ------------------------------------------------------------------ #

    def charge_if_needed(self, force_recalculate=False):
        """Plan and set the action entities, then publish the mode and the heartbeat.

        Attributes of sensor.<prefix>_charger_action for the user's automations:
        mode    normal / error (error: charge with max amps, like ev.py)
        updated time of the last calculation; an automation can fall back to charging
                when it gets old (the integration stopped or hangs)
        """
        self.charger_mode = "normal"
        try:
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

    # ------------------------------------------------------------------ #
    # Preheating
    # ------------------------------------------------------------------ #

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

        if 0 < minutes_between(now, next_drive, error_value=-1) <= preheat_min_before:
            if next_drive == trip_date_time:
                preheat = self.switch_state("trip_preheat") == "on"
            else:
                preheat = self.switch_state(f"preheat_{day_of_week_text(next_drive)}") == "on"

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

    # ------------------------------------------------------------------ #
    # Charging loss
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Notifications on plug in
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Jobs (called by the coordinator)
    # ------------------------------------------------------------------ #

    def startup(self, databases: dict, odometer_history: dict | None = None) -> dict:
        """Load the databases and the runtime state and calculate the first plan."""
        t = self.t
        runtime = databases.get("runtime") or {}
        for key, value in (runtime.get("sensors") or {}).items():
            self.sensor_states.setdefault(key, value)
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

    def preheat_cron(self) -> None:
        self.preheat_ev()

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
