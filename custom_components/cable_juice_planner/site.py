"""Site: the grid connection shared by several Cable Juice Planner instances.

The site config entry holds the main fuse per phase, an optional current (or power)
sensor per phase and how soon a trip makes a car urgent. Every car instance registers
here and asks for its capacity before each job (event loop, no I/O):

* order: urgent cars first (battery under the daily minimum or a trip soon), then the
  fixed priority of the instance, then the prefix
* planned capacity per hour = fuse - margin - forecast house current (hour of day mean
  of the last week, without the cars) - reservations of the cars ahead
* live capacity = fuse - margin - house current now - what the cars ahead draw now

Phases are grid phases here; every car maps its charger L1/L2/L3 onto them with its
phase order. When a car ahead changes its plan, the cars behind it are recalculated.
With phase sensors an overloaded phase immediately limits the last charging car.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_change
from homeassistant.util import dt as dt_util

from .const import PHASE_ORDERS, SIGNAL_SITE_UPDATE, SITE_DEFAULTS, SITE_HOURS_AHEAD
from .helpers import float_or_none

if TYPE_CHECKING:
    from .coordinator import CJPCoordinator

_LOGGER = logging.getLogger(__name__)

RECALCULATE_DELAY = 15  # s, the cars behind a changed plan are recalculated after this
OVERLOAD_THROTTLE = 20  # s between two overload reactions for the same car
FORECAST_DAYS = 7
POWER_UNITS = {"W": 1.0, "kW": 1000.0}


class CJPSite:
    """Shared capacity of one grid connection."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.config: dict[str, Any] = {**SITE_DEFAULTS, **entry.options}
        self.members: dict[str, CJPCoordinator] = {}
        self.reservations: dict[str, dict[datetime.datetime, list[float]]] = {}  # grid phases
        self.requests: dict[str, list[float]] = {}  # grid phases
        self.urgent: dict[str, bool] = {}
        self.house_forecast: dict[int, list[float]] = {}  # hour of day -> grid phases
        self.house_now: list[float] = [0.0, 0.0, 0.0]
        self.available_now: dict[str, list[float]] = {}
        self._unsubs: list = []
        self._pending: dict[str, Any] = {}
        self._last_overload: dict[str, float] = {}
        self._startup_ready = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    @property
    def phase_entities(self) -> list[str]:
        return [self.config.get(f"phase_l{i}_entity_id") or "" for i in (1, 2, 3)]

    @property
    def has_phase_sensors(self) -> bool:
        return all(self.phase_entities)

    def fuse(self) -> list[float]:
        margin = float(self.config.get("margin") or 0.0)
        return [max(float(self.config.get(f"fuse_l{i}") or 0.0) - margin, 0.0) for i in (1, 2, 3)]

    async def async_start(self) -> None:
        try:
            if self.has_phase_sensors:
                self._unsubs.append(async_track_state_change_event(self.hass, self.phase_entities, self._phase_changed))
                self._unsubs.append(async_track_time_change(self.hass, self._refresh_forecast, minute=7, second=0))
                await self._async_refresh_forecast()
            self.recalculate_all()
        finally:
            # Cars starting on the same homeassistant_started event must not
            # calculate against a half-initialized site forecast.
            self._startup_ready.set()

    async def async_wait_started(self) -> None:
        """Wait until the site's initial load-balancing data is ready."""
        await self._startup_ready.wait()

    async def async_stop(self) -> None:
        for unsub in (*self._unsubs, *self._pending.values()):
            unsub()
        self._unsubs.clear()
        self._pending.clear()
        members = list(self.members.values())
        self.members.clear()
        for coordinator in members:
            coordinator.async_site_recalculate()

    def register(self, coordinator: CJPCoordinator) -> None:
        self.members[coordinator.entry.entry_id] = coordinator
        _LOGGER.info("Site: %s joined (priority %s, phase order %s)", coordinator.prefix, coordinator.site_priority, coordinator.site_phase_order)

    def unregister(self, entry_id: str) -> None:
        self.members.pop(entry_id, None)
        self.reservations.pop(entry_id, None)
        self.requests.pop(entry_id, None)
        self.urgent.pop(entry_id, None)
        self.recalculate_all()

    # ------------------------------------------------------------------ #
    # Order and phase mapping
    # ------------------------------------------------------------------ #

    def order(self) -> list[str]:
        return sorted(
            self.members,
            key=lambda entry_id: (not self.urgent.get(entry_id, False), self.members[entry_id].site_priority, self.members[entry_id].prefix),
        )

    def ahead(self, entry_id: str) -> list[str]:
        order = self.order()
        return order[: order.index(entry_id)] if entry_id in order else order

    def behind(self, entry_id: str) -> list[str]:
        order = self.order()
        return order[order.index(entry_id) + 1:] if entry_id in order else []

    @staticmethod
    def _order(coordinator: CJPCoordinator) -> tuple[int, int, int]:
        return PHASE_ORDERS.get(coordinator.site_phase_order, PHASE_ORDERS["L1-L2-L3"])

    def to_grid(self, coordinator: CJPCoordinator, charger: list) -> list[float]:
        grid = [0.0, 0.0, 0.0]
        for i, grid_phase in enumerate(self._order(coordinator)):
            grid[grid_phase] += float(charger[i]) if i < len(charger) else 0.0
        return grid

    def to_charger(self, coordinator: CJPCoordinator, grid: list) -> list[float]:
        return [round(float(grid[grid_phase]), 1) for grid_phase in self._order(coordinator)]

    # ------------------------------------------------------------------ #
    # Measurements
    # ------------------------------------------------------------------ #

    def _amps(self, entity_id: str, voltage: float) -> float | None:
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or (value := float_or_none(state.state)) is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit in POWER_UNITS:
            return abs(value) * POWER_UNITS[unit] / voltage
        if unit == "mA":
            return abs(value) / 1000
        return abs(value)

    def _measured(self) -> list[float] | None:
        voltage = float(self.config.get("voltage") or 230.0)
        values = [self._amps(entity_id, voltage) for entity_id in self.phase_entities]
        return None if any(value is None for value in values) else values

    def _car_draw(self, coordinator: CJPCoordinator) -> list[float]:
        """What the car draws now per grid phase (from its charger power sensor)."""
        charger = coordinator.config["charger"]
        voltage = float(charger["power_voltage"]) or 230.0
        watt = self._amps(charger["entity_ids"]["power_consumtion_entity_id"], 1.0) or 0.0
        request = self.requests.get(coordinator.entry.entry_id) or [0.0, 0.0, 0.0]
        phases = sum(1 for value in request if value > 0) or int(charger["charging_phases"])
        per_phase = watt / voltage / phases
        grid = [0.0, 0.0, 0.0]
        for i, grid_phase in enumerate(self._order(coordinator)):
            if i < phases:
                grid[grid_phase] += per_phase
        return grid

    def _house(self) -> list[float]:
        """House current per grid phase now, without the cars."""
        measured = self._measured()
        if measured is None:
            return [0.0, 0.0, 0.0]
        cars = [0.0, 0.0, 0.0]
        for coordinator in self.members.values():
            cars = [a + b for a, b in zip(cars, self._car_draw(coordinator))]
        return [max(m - c, 0.0) for m, c in zip(measured, cars)]

    # ------------------------------------------------------------------ #
    # Capacity for one car (called by the coordinator before each job)
    # ------------------------------------------------------------------ #

    def snapshot_for(self, coordinator: CJPCoordinator, now: datetime.datetime) -> dict:
        entry_id = coordinator.entry.entry_id
        fuse = self.fuse()
        ahead = [self.members[e] for e in self.ahead(entry_id) if e in self.members]
        self.house_now = house = self._house()
        live = list(fuse)
        for other in ahead:
            # with phase sensors the real draw counts, else what the car was told
            draw = self._car_draw(other) if self.has_phase_sensors else (self.requests.get(other.entry.entry_id) or [0.0, 0.0, 0.0])
            live = [a - b for a, b in zip(live, draw)]
        live = [max(a - h, 0.0) for a, h in zip(live, house)]
        self.available_now[entry_id] = live
        hour_capacity = {}
        start = now.replace(minute=0, second=0, microsecond=0)
        for offset in range(SITE_HOURS_AHEAD):
            hour = start + datetime.timedelta(hours=offset)
            capacity = [a - h for a, h in zip(fuse, self.house_forecast.get(hour.hour, [0.0, 0.0, 0.0]))]
            for other in ahead:
                reserved = self.reservations.get(other.entry.entry_id, {}).get(hour)
                if reserved:
                    capacity = [a - b for a, b in zip(capacity, reserved)]
            hour_capacity[hour] = self.to_charger(coordinator, [max(a, 0.0) for a in capacity])
        async_dispatcher_send(self.hass, SIGNAL_SITE_UPDATE)
        return {
            "live": self.to_charger(coordinator, live),
            "hour_capacity": hour_capacity,
            "urgent_hours": float(self.config.get("urgent_trip_hours") or 12),
        }

    @callback
    def update_from_result(self, coordinator: CJPCoordinator, site_result: dict) -> None:
        """A car published its plan/request; recalculate the cars behind if it changed."""
        entry_id = coordinator.entry.entry_id
        reservation = {hour: self.to_grid(coordinator, amps) for hour, amps in (site_result.get("reservation") or {}).items()}
        request = self.to_grid(coordinator, site_result.get("request") or [0, 0, 0])
        urgent = bool(site_result.get("urgent"))
        changed_order = self.urgent.get(entry_id) != urgent
        # the live request counts too: a car ahead that draws more (or less) changes what is left
        changed_plan = self.reservations.get(entry_id) != reservation or self.requests.get(entry_id) != request
        self.reservations[entry_id] = reservation
        self.requests[entry_id] = request
        self.urgent[entry_id] = urgent
        if changed_order:
            _LOGGER.info("Site: %s is %s", coordinator.prefix, "urgent, charging first" if urgent else "no longer urgent")
            # every car incl. this one: its result was calculated with its old place in the order
            self.recalculate_all()
        elif changed_plan:
            for other in self.behind(entry_id):
                self._schedule_recalculate(other)
        async_dispatcher_send(self.hass, SIGNAL_SITE_UPDATE)

    def recalculate_all(self) -> None:
        for entry_id in self.members:
            self._schedule_recalculate(entry_id)

    def _schedule_recalculate(self, entry_id: str) -> None:
        if entry_id in self._pending:
            return

        @callback
        def _run(_now) -> None:
            self._pending.pop(entry_id, None)
            if (coordinator := self.members.get(entry_id)) is not None:
                coordinator.async_site_recalculate()

        self._pending[entry_id] = async_call_later(self.hass, RECALCULATE_DELAY, _run)

    # ------------------------------------------------------------------ #
    # Overload protection
    # ------------------------------------------------------------------ #

    @callback
    def _phase_changed(self, _event: Event) -> None:
        measured = self._measured()
        if measured is None:
            return
        fuses = [float(self.config.get(f"fuse_l{i}") or 0.0) for i in (1, 2, 3)]
        if not any(m > f for m, f in zip(measured, fuses)):
            return
        for entry_id in reversed(self.order()):
            if sum(self.requests.get(entry_id) or [0]) <= 0:
                continue
            if time.monotonic() - self._last_overload.get(entry_id, 0.0) < OVERLOAD_THROTTLE:
                return
            self._last_overload[entry_id] = time.monotonic()
            coordinator = self.members[entry_id]
            _LOGGER.warning("Site: phase current %s A above the fuse %s A, limiting %s", [round(m, 1) for m in measured], fuses, coordinator.prefix)
            coordinator.async_site_limit()
            return

    # ------------------------------------------------------------------ #
    # House forecast (hour of day mean of the last week, without the cars)
    # ------------------------------------------------------------------ #

    @callback
    def _refresh_forecast(self, _now) -> None:
        self.entry.async_create_background_task(self.hass, self._async_refresh_forecast(), name="cjp_site_forecast")

    async def _async_refresh_forecast(self) -> None:
        if "recorder" not in self.hass.config.components or not self.has_phase_sensors:
            return
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period  # noqa: PLC0415

        end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - datetime.timedelta(days=FORECAST_DAYS)
        chargers = {e: c.config["charger"]["entity_ids"]["power_consumtion_entity_id"] for e, c in self.members.items()}
        ids = {*self.phase_entities, *(c for c in chargers.values() if c)}
        try:
            stats = await get_instance(self.hass).async_add_executor_job(statistics_during_period, self.hass, start, end, ids, "hour", None, {"mean"})
            metadata = await get_instance(self.hass).async_add_executor_job(partial(get_metadata, self.hass, statistic_ids=ids))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Site: could not load statistics: %s", err)
            return
        voltage = float(self.config.get("voltage") or 230.0)
        units = {statistic_id: meta.get("unit_of_measurement") for statistic_id, (_, meta) in metadata.items()}

        def series(entity_id, to_amps):
            result = {}
            for row in stats.get(entity_id, []):
                ts = row.get("start")
                ts = ts.timestamp() if isinstance(ts, datetime.datetime) else ts
                if ts is not None and row.get("mean") is not None:
                    result[ts] = to_amps(float(row["mean"]))
            return result

        def unit_factor(entity_id):
            # the unit the statistics were recorded in; the state is not there yet right after a restart
            if unit := units.get(entity_id):
                return unit
            state = self.hass.states.get(entity_id)
            return state.attributes.get("unit_of_measurement") if state else None

        phases = []
        for entity_id in self.phase_entities:
            unit = unit_factor(entity_id)
            if unit is None and stats.get(entity_id):
                # W read as A would leave no current for the cars: keep the previous forecast
                _LOGGER.debug("Site: unit of %s unknown, house forecast not updated", entity_id)
                return
            factor = (POWER_UNITS[unit] / voltage) if unit in POWER_UNITS else (0.001 if unit == "mA" else 1.0)
            phases.append(series(entity_id, lambda v, f=factor: abs(v) * f))
        cars = []
        for entry_id, entity_id in chargers.items():
            coordinator = self.members[entry_id]
            unit = unit_factor(entity_id) if entity_id else None
            watt = POWER_UNITS.get(unit, 1.0)
            car_voltage = float(coordinator.config["charger"]["power_voltage"]) or 230.0
            car_phases = int(coordinator.config["charger"]["charging_phases"])
            draw = series(entity_id, lambda v, w=watt, cv=car_voltage, cp=car_phases: abs(v) * w / cv / cp) if entity_id else {}
            cars.append((self._order(coordinator)[:car_phases], draw))
        sums: dict[int, list[float]] = {}
        counts: dict[int, int] = {}
        for ts in set(phases[0]) & set(phases[1]) & set(phases[2]):
            values = [phases[i][ts] for i in range(3)]
            for grid_phases, draw in cars:
                for grid_phase in grid_phases:
                    values[grid_phase] -= draw.get(ts, 0.0)
            hour = dt_util.as_local(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)).hour
            sums[hour] = [a + max(b, 0.0) for a, b in zip(sums.get(hour, [0.0, 0.0, 0.0]), values)]
            counts[hour] = counts.get(hour, 0) + 1
        self.house_forecast = {hour: [round(v / counts[hour], 2) for v in values] for hour, values in sums.items()}
        _LOGGER.debug("Site: house forecast per hour of day %s", self.house_forecast)
