"""The current and public charging sessions."""
from __future__ import annotations

import datetime
import logging

from ...const import ENTITY_UNAVAILABLE_STATES, EV_PLUGGED_STATES, EV_UNPLUGGED_STATES
from ...utils.formatting import emoji_parse
from ...utils.helpers import to_datetime

_LOGGER = logging.getLogger(__name__)


class HistorySession:
    """The current and public charging sessions."""

    def reset_current_charging_session(self) -> None:
        self.current_charging_session = {"charging_type": None, "start": None, "start_charger_meter": 0.0, "emoji": "", "data": {}}

    def stop_current_charging_session(self) -> None:
        self.charging_history(None, "")

    def _is_not_home(self) -> bool:
        current_location = self.get_state(self.config["ev_car"]["entity_ids"]["location_entity_id"], error_state="away")
        return not (current_location == "home" or current_location in ENTITY_UNAVAILABLE_STATES)

    def _set_public_session_done(self, value: str) -> None:
        old = self.sensor_states.get("public_charging_session_done")
        self.set_state("public_charging_session_done", value)
        self.mark_dirty("runtime")
        if old != value:
            # state_trigger_public_charging_session_done
            self.public_charging_session_done_changed(value)

    def public_charging_session(self, entity_state: str):
        """Charge port/cable changed away from home (or the cost was entered: add_to_history)."""
        t = self.t
        sessions = self.public_charging_sessions
        if entity_state in EV_PLUGGED_STATES and self._is_not_home():
            if sessions and "end_battery_level" not in sessions[-1]:
                return None
            sessions.append({"start_battery_level": self.battery_level()})
            self.mark_dirty("runtime")
        elif sessions and entity_state in EV_UNPLUGGED_STATES and self._is_not_home():
            sessions[-1].update({
                "end_battery_level": self.battery_level(),
                "added_percentage": round(self.battery_level() - sessions[-1]["start_battery_level"], 1),
                "timestamp": self.now(),
            })
            self.mark_dirty("runtime")
            if sessions[-1]["added_percentage"] <= 0.0:
                _LOGGER.warning("Public charging session: End battery level %s%% is less than start battery level %s%%", sessions[-1]["end_battery_level"], sessions[-1]["start_battery_level"])
                sessions.pop(-1)
                if not sessions:
                    self._set_public_session_done("off")
                return None
            self._set_public_session_done("on")
            self.set_attr("public_charging_session_done", "public_charging_session", [dict(s) for s in sessions])
            self.set_setting("public_charging_session_cost", -2.0)
            data = {"actions": [{"action": "URI", "title": t("ui.public_charging_session.action"), "uri": f"entityId:{self.own_entity_id('number', 'public_charging_session_cost')}"}]}
            self.my_notify(
                t(
                    "ui.public_charging_session.message",
                    start_battery_level=sessions[-1]["start_battery_level"],
                    end_battery_level=sessions[-1]["end_battery_level"],
                    added_battery_level=sessions[-1]["added_percentage"],
                ),
                title=f"{self.title} {t('ui.public_charging_session.title')}",
                data=data,
                always=True,
            )
        elif self.get_public_charging_session_done() and sessions and entity_state == "add_to_history":
            cost = self.number_state("public_charging_session_cost")
            if not isinstance(cost, float):
                _LOGGER.warning("Public charging session: Invalid cost %s, not adding to history", cost)
                self.set_setting("public_charging_session_cost", -2.0)
                return None
            if cost == 0.0:
                return None
            if cost == -1.0:
                _LOGGER.info("Public charging session cost reset to -1.0, not adding to history")
                sessions.pop(0)
                self.mark_dirty("runtime")
                if not sessions:
                    self._set_public_session_done("off")
                else:
                    self.set_setting("public_charging_session_cost", -2.0)
                    self.set_attr("public_charging_session_done", "public_charging_session", [dict(s) for s in sessions])
                return None
            if cost < 0.0:
                self.set_setting("public_charging_session_cost", 0.0)
                return None
            current_session = sessions.pop(0)
            self.mark_dirty("runtime")
            added_kwh = self.percentage_to_kwh(current_session["added_percentage"], include_charging_loss=False)
            timestamp = self.now()
            charger_meter = float(self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True))
            self.charging_history_db[timestamp] = {
                "percentage": round(current_session["added_percentage"], 1),
                "kWh": round(added_kwh, 3),
                "kWh_from_public": round(added_kwh, 3),
                "kWh_from_local_energy": 0.0,
                "solar_kwh_of_local_energy": 0.0,
                "powerwall_kwh_of_local_energy": 0.0,
                "cost": round(cost, 3),
                "public_cost": round(cost, 3),
                "unit": round(cost / added_kwh if added_kwh > 0.0 else 0.0, 3),
                "emoji": emoji_parse({"public_charging": True}),
                "start_charger_meter": charger_meter,
                "end_charger_meter": charger_meter,
                "ended": f"{timestamp.strftime('%H:%M')}",
                "charging_type": "public_charging",
            }
            odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
            if odometer_entity:
                odometer = self.get_state(odometer_entity, float_type=True, error_state=None)
                if odometer:
                    self.charging_history_db[timestamp]["odometer"] = int(odometer)
            if not sessions:
                self._set_public_session_done("off")
            else:
                self.set_setting("public_charging_session_cost", -2.0)
                self.set_attr("public_charging_session_done", "public_charging_session", [dict(s) for s in sessions])
            _LOGGER.info("Public charging session added to history: %s%% %.2fkWh %.2fkr", current_session["added_percentage"], added_kwh, cost)
            try:
                self.charging_history_combine_and_set()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in public_charging_session: %s %s", err, type(err))
                self.persistent_notification(f"Error in public_charging_session: {err} {type(err)}", f"{self.title} error", f"{self.name}_public_charging_session_error")
        else:
            _LOGGER.warning(
                "Public charging session: Invalid entity_state transition from %s with %s, get_public_charging_session_done()=%s",
                entity_state, sessions, self.get_public_charging_session_done(),
            )
        return None

    def public_charging_session_done_changed(self, value) -> None:
        """state_trigger_public_charging_session_done"""
        if value in ENTITY_UNAVAILABLE_STATES:
            return
        if value == "off":
            self.public_charging_sessions = []
            self.mark_dirty("runtime")
            self.set_setting("public_charging_session_cost", 0.0)
            self.del_attr("public_charging_session_done", "public_charging_session")

    def manual_add_public_charging_session(self, charging_session_start, battery_level_begin=None, battery_level_end=None, battery_level_added=None,
                                           battery_kwh_added=None, total_charging_cost=None, include_charging_loss=False, add_to_history=False) -> dict:
        try:
            if charging_session_start is None:
                raise ValueError('"Charging Session Start" is required.')
            charging_session_start = to_datetime(charging_session_start)
            if isinstance(charging_session_start, datetime.datetime):
                charging_session_start = charging_session_start.replace(tzinfo=None)
            else:
                raise ValueError('"Charging Session Start" must be a datetime object.')
            if battery_level_begin is None and battery_level_end is None and battery_level_added is None and battery_kwh_added is None:
                raise ValueError('Use at least "Battery Level Begin" and "Battery Level End" or "Battery Level Added" or "Battery kWh Added" to calculate the added energy.')
            if battery_kwh_added is not None:
                if not isinstance(battery_kwh_added, (int, float)):
                    raise ValueError('"Battery kWh Added" must be a number.')
                if battery_kwh_added <= 0:
                    raise ValueError('"Battery kWh Added" must be greater than 0.')
                consumption_used = "Battery kWh Added"
                added_percentage = self.kwh_to_percentage(float(battery_kwh_added), include_charging_loss=include_charging_loss)
                added_kwh = float(battery_kwh_added)
            elif battery_level_added is not None:
                if not isinstance(battery_level_added, (int, float)):
                    raise ValueError('"Battery Level Added" must be a number.')
                if battery_level_added <= 0:
                    raise ValueError('"Battery Level Added" must be greater than 0.')
                consumption_used = "Battery Level Added"
                added_percentage = float(battery_level_added)
                added_kwh = self.percentage_to_kwh(added_percentage, include_charging_loss=include_charging_loss)
            else:
                if battery_level_begin is not None and battery_level_end is None:
                    raise ValueError('"Battery Level End" is required when "Battery Level Begin" is provided.')
                if battery_level_begin is None and battery_level_end is not None:
                    raise ValueError('"Battery Level Begin" is required when "Battery Level End" is provided.')
                if not isinstance(battery_level_begin, (int, float)) or not isinstance(battery_level_end, (int, float)):
                    raise ValueError('"Battery Level Begin" and "Battery Level End" must be numbers.')
                if battery_level_end <= battery_level_begin:
                    raise ValueError('"Battery Level End" must be greater than "Battery Level Begin".')
                consumption_used = "Battery Level Begin/End"
                added_percentage = battery_level_end - battery_level_begin
                added_kwh = self.percentage_to_kwh(added_percentage, include_charging_loss=include_charging_loss)
            if total_charging_cost is None:
                raise ValueError('"Total Charging Cost" is required.')
            if not isinstance(total_charging_cost, (int, float)):
                raise ValueError('"Total Charging Cost" must be a number.')
            if total_charging_cost <= 0:
                raise ValueError('"Total Charging Cost" must be greater than 0.')
            total_charging_cost = float(total_charging_cost)
            kwh_cost = total_charging_cost / added_kwh if added_kwh > 0 else 0.0
            data = {
                "Charging Session Start": charging_session_start.isoformat(),
                "Consumption Used": consumption_used,
                "Added Percentage": round(added_percentage, 1),
                "Added kWh": round(added_kwh, 3),
                "kWh Cost": round(kwh_cost, 3),
                "Total Charging Cost": round(total_charging_cost, 3),
            }
            if not add_to_history:
                return {"status": "success", "message": "Public charging session results. Nothing was added to history.", "data": data}
            charger_meter = float(self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True))
            self.charging_history_db[charging_session_start] = {
                "percentage": round(added_percentage, 1),
                "kWh": round(added_kwh, 3),
                "kWh_from_public": round(added_kwh, 3),
                "kWh_from_local_energy": 0.0,
                "solar_kwh_of_local_energy": 0.0,
                "powerwall_kwh_of_local_energy": 0.0,
                "cost": round(total_charging_cost, 3),
                "public_cost": round(total_charging_cost, 3),
                "unit": round(kwh_cost, 3),
                "emoji": emoji_parse({"public_charging": True}),
                "start_charger_meter": charger_meter,
                "end_charger_meter": charger_meter,
                "ended": f"{charging_session_start.strftime('%H:%M')}",
                "charging_type": "public_charging",
            }
            self.charging_history_combine_and_set()
            return {"status": "success", "message": "Public charging session added successfully to history.", "data": data}
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in manual_add_public_charging_session: %s", err)
            return {"status": "error", "message": f"{err}"}
