"""Triggers and reactions to state changes and settings."""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from functools import partial
from typing import Any, Callable

from homeassistant.core import Event, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)

from ..const import CHARGING_LOSS_METER_TIMEOUT, NUMBER_SETTINGS, SIGNAL_HELP_VALUES_CHANGED
from ..utils.formatting import format_charging_rule
from ..utils.helpers import float_or_none
from .constants import BATTERY_LEVEL_WAIT, CHARGE_TRIGGER_KEYS, PUBLIC_CHARGING_COST_DELAY, RULE_KEYS, SETTINGS_DEBOUNCE_SECONDS, TRIP_KEYS

_LOGGER = logging.getLogger(__name__)


class CoordinatorEvents:
    """Triggers, charger and meter changes, settings changes and button presses."""

    def _register_triggers(self) -> None:
        interval = min(int(self.config["cron_interval"]), 15)
        track = partial(async_track_time_change, self.hass)
        self._unsubs.append(track(self._handle_cron, minute=f"/{interval}", second=0))
        self._unsubs.append(track(self._job_handler("hour_end", include_forecast=True), minute=59, second=0))
        self._unsubs.append(track(self._job_handler("price_update", include_prices=True), minute=2, second=0))
        self._unsubs.append(track(self._job_handler("append_prices"), hour=5, minute=0, second=0))
        self._unsubs.append(track(self._economy_daily, hour=3, minute=41, second=0))
        self._unsubs.append(track(self._analysis_daily, hour=3, minute=47, second=0))
        if self.ev_configured:
            self._unsubs.append(track(self._job_handler("preheat_cron"), minute="/5", second=0))
            self._unsubs.append(track(self._charging_loss_hourly, minute=17, second=0))

        charger = self.config["charger"]["entity_ids"]
        ev = self.config["ev_car"]["entity_ids"]
        listeners: dict[str, Callable] = {}
        if charger["status_entity_id"]:
            listeners[charger["status_entity_id"]] = self._charger_status_changed
        if charger["kwh_meter_entity_id"]:
            listeners.setdefault(charger["kwh_meter_entity_id"], self._kwh_meter_changed)
        if charger["cable_connected_entity_id"] and not charger["status_entity_id"]:
            listeners.setdefault(charger["cable_connected_entity_id"], self._charger_cable_changed)
        if self.ev_configured:
            port = ev["charge_port_door_entity_id"] or ev["charge_cable_entity_id"]
            if port:
                listeners.setdefault(port, self._ev_port_changed)
        if listeners:
            self._unsubs.append(async_track_state_change_event(self.hass, list(listeners), partial(self._dispatch_state_change, listeners)))

    def _job_handler(self, job_name: str, **kwargs) -> Callable:
        @callback
        def _handler(_now: datetime.datetime) -> None:
            self._background(self.async_run_job(job_name, **kwargs), job_name)

        return _handler

    @callback
    def _handle_cron(self, _now: datetime.datetime) -> None:
        self._background(self.async_run_job("cron", include_forecast=True), "cron")

    @callback
    def _dispatch_state_change(self, listeners: dict[str, Callable], event: Event) -> None:
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        new = new_state.state if new_state else None
        old = old_state.state if old_state else None
        if new == old:
            return  # PyScript state triggers only fire when the state changes
        listeners[event.data["entity_id"]](new, old)

    @callback
    def _charger_status_changed(self, value, old_value) -> None:
        if self.settings.get("calculate_charging_loss"):
            self._background(self._async_charging_loss("status", value, old_value), "charging_loss")
        self._background(self._async_charger_status_flow(value, old_value), "charger_status")

    async def _async_charger_status_flow(self, value, old_value) -> None:
        result = await self.async_run_job("charger_status_changed", value, old_value)
        if result.value != "wait":
            return
        if not self.ev_configured:
            # wait_until_battery_level_input: the user enters the battery level of the car
            deadline = time.monotonic() + BATTERY_LEVEL_WAIT.total_seconds()
            while time.monotonic() < deadline and int(float_or_none(self.settings.get("battery_level")) or 0) <= 0:
                await asyncio.sleep(5)
            _LOGGER.info("Battery level input: %s", self.settings.get("battery_level"))
            if int(float_or_none(self.settings.get("battery_level")) or 0) <= 0:
                _LOGGER.info("Battery level was not entered within %s; skipping plug-in processing", BATTERY_LEVEL_WAIT)
                return
        await self.async_run_job("charger_connected", value, include_forecast=True)

    @callback
    def _kwh_meter_changed(self, value, old_value) -> None:
        if self.settings.get("calculate_charging_loss"):
            self._background(self._async_charging_loss("kwh_meter", value, old_value), "charging_loss")

    async def _async_charging_loss(self, source: str, value=None, old_value=None) -> None:
        """Charging loss measurement. After charging completed the engine waits for the
        kWh meter; a slow meter that never updates is read once CHARGING_LOSS_METER_TIMEOUT
        later."""
        result = await self.async_run_job("calculate_charging_loss", source, value, old_value)
        if result.value == "wait_for_meter":
            self._cancel_charging_loss_timeout()
            self._charging_loss_timeout = async_call_later(self.hass, CHARGING_LOSS_METER_TIMEOUT, self._charging_loss_timed_out)
        elif not self.settings.get("calculate_charging_loss"):
            self._cancel_charging_loss_timeout()

    @callback
    def _charging_loss_timed_out(self, _now) -> None:
        self._charging_loss_timeout = None
        if self.settings.get("calculate_charging_loss"):
            self._background(self._async_charging_loss("timeout"), "charging_loss")

    @callback
    def _cancel_charging_loss_timeout(self) -> None:
        if self._charging_loss_timeout:
            self._charging_loss_timeout()
            self._charging_loss_timeout = None

    @callback
    def _charger_cable_changed(self, value, old_value) -> None:
        self._background(self.async_run_job("charger_cable_changed", value, old_value), "charger_cable")

    @callback
    def _ev_port_changed(self, value, old_value) -> None:
        self._background(self.async_run_job("ev_port_changed", value, old_value), "ev_port")

    @callback
    def async_set_setting(self, key: str, value: Any, source: str = "user") -> None:
        """A setting entity changed. source: initial, user (entity) or engine (job result)."""
        old = self.settings.get(key)
        self.settings[key] = value
        if old != value and key in NUMBER_SETTINGS:
            self.hass.loop.call_soon(
                async_dispatcher_send, self.hass, self.signal, SIGNAL_HELP_VALUES_CHANGED
            )
        if source == "initial" or old == value or not self.started:
            return
        if source == "engine":
            async_dispatcher_send(self.hass, self.signal, key)
        _LOGGER.debug("Setting %s changed from %s to %s (%s)", key, old, value, source)
        triggered_charge = self._setting_trigger(key, value, old)
        if source == "user" and not triggered_charge:
            if self._settings_debounce:
                self._settings_debounce()
            self._settings_debounce = async_call_later(self.hass, SETTINGS_DEBOUNCE_SECONDS, self._settings_changed)

    def _setting_trigger(self, key: str, value: Any, old: Any) -> bool:
        """Run the PyScript state trigger of a setting. True if it already planned."""
        if key in RULE_KEYS:
            self._background(self.async_run_job("charging_rule_changed", key, value, include_forecast=True), "rule_changed")
            return False
        if key in CHARGE_TRIGGER_KEYS:
            if key == "calculate_charging_loss":
                self._background(self._async_charging_loss("switch", value), "charging_loss")
            self._background(self.async_run_job("charge_trigger", key, include_forecast=True), "charge_trigger")
            return True
        if key in TRIP_KEYS:
            self._background(self.async_run_job("trips_changed", key, value), "trips")
            return False
        if key == "public_charging_session_cost":
            self._background(self._async_public_cost_flow(value), "public_cost")
            return True
        return False

    @callback
    def _settings_changed(self, _now) -> None:
        self._settings_debounce = None
        self._background(self.async_run_job("recalculate", include_forecast=True), "recalculate")

    @callback
    def async_press(self, key: str) -> None:
        if not self.started:
            return
        if key == "enforce_planning":
            self._background(self.async_run_job("charge_trigger", key, include_forecast=True), "enforce_planning")
        elif key == "trip_reset":
            self._background(self.async_run_job("trips_changed", key, None), "trip_reset")
        elif key == "recalculate_charging_loss":
            self._background(self.async_recalculate_charging_loss(), "charging_loss_recalculate")

    async def _async_public_cost_flow(self, value) -> None:
        result = await self.async_run_job("public_charging_cost_changed", float_or_none(value))
        if result.value != "add":
            return
        if self._public_cost_task and not self._public_cost_task.done() and self._public_cost_task is not asyncio.current_task():
            self._public_cost_task.cancel()
        self._public_cost_task = asyncio.current_task()
        await asyncio.sleep(PUBLIC_CHARGING_COST_DELAY)
        await self.async_run_job("add_public_charging_session")

    @callback
    def set_charging_rule(self, text: str) -> None:
        """Progress text during startup (the engine sets the rule afterwards)."""
        self.data["current_charging_rule"] = format_charging_rule(text, self.testing)
        async_dispatcher_send(self.hass, self.signal, "current_charging_rule")
