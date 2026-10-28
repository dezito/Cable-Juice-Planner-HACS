"""Charging amps and the commands to the charger."""
from __future__ import annotations

import logging

from ...utils.helpers import get_closest_key, minutes_between, reset_datetime, to_datetime

_LOGGER = logging.getLogger(__name__)


class ControlActions:
    """Charging amps and the commands to the charger."""

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
