"""The snapshot of Home Assistant data, buffers and statistics."""
from __future__ import annotations

import asyncio
import bisect
import datetime
import logging
import time
from functools import partial
from typing import Any

import astral
from homeassistant.core import Event, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..const import HISTORY_BUFFER_HOURS, HISTORY_SHORT_BUFFER_HOURS
from ..engine import Snapshot
from ..engine.energy.forecast import SOLCAST_ENDINGS, strip_forecast_suffix
from ..utils.helpers import float_or_none
from ..engine.history.history import merge_rows
from ..prices import PRICE_SNAPSHOT_SECONDS, TARIFF_STATE_KEYS
from .constants import FORECAST_TIMEOUT, STATISTICS_REFRESH_SECONDS

_LOGGER = logging.getLogger(__name__)


class CoordinatorData:
    """Collects the snapshot for the worker and keeps the history buffers."""

    async def _async_snapshot(self, include_prices: bool = False, include_forecast: bool = False) -> Snapshot:
        await self._async_refresh_statistics()
        now = self._local_now()
        states: dict[str, str] = {}
        attributes: dict[str, dict] = {}
        units: dict[str, str | None] = {}
        for entity_id in self.configured_entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            states[entity_id] = state.state
            attributes[entity_id] = dict(state.attributes)
            units[entity_id] = state.attributes.get("unit_of_measurement")
        for entity_id in self.price_entities:
            if (state := self.hass.states.get(entity_id)) is not None:
                states[entity_id] = state.state
        self._prune_buffers()
        # like sun.get_astral_observer (HA 2026.10+), built here so older HA versions work too
        observer = astral.Observer(self.hass.config.latitude, self.hass.config.longitude, self.hass.config.elevation)
        snapshot = Snapshot(
            now=now,
            tz=dt_util.get_default_time_zone(),
            settings=dict(self.settings),
            states=states,
            attributes=attributes,
            platforms=dict(self._platforms),
            units=units,
            buffers={entity_id: list(rows) for entity_id, rows in self._buffers.items()},
            statistics={entity_id: list(rows) for entity_id, rows in self._statistics.items()},
            astral=observer,
            site=self.site.snapshot_for(self, now) if self.site is not None and self.entry.entry_id in self.site.members else None,
        )
        # like the shared price cache of the PyScript version: refreshed at most once an hour
        if include_prices and (not self._price_snapshot_time or time.monotonic() - self._price_snapshot_time > PRICE_SNAPSHOT_SECONDS):
            snapshot.price_data = self._price_snapshot()
            self._price_snapshot_time = time.monotonic()
        if include_forecast and self.solar_configured:
            snapshot.forecast_dict = await self._async_weather_forecasts()
            snapshot.solcast = self._solcast_snapshot()
        return snapshot

    def _price_snapshot(self) -> dict:
        entity_ids = self.config["prices"]["entity_ids"]
        attributes: dict[str, dict] = {}
        values: dict[str, float] = {}
        tariff_entities = {entity_ids.get(key) for key in TARIFF_STATE_KEYS} - {None, ""}
        for entity_id in self.price_entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            attributes[entity_id] = dict(state.attributes)
            if entity_id in tariff_entities:
                value = float_or_none(state.state)
                values[entity_id] = value if value is not None else 0.0
        return {"attributes": attributes, "values": values}

    def _solcast_snapshot(self) -> dict[str, list]:
        forecast_entity = self.config["solar"]["entity_ids"]["forecast_entity_id"]
        if not forecast_entity or self._platforms.get(forecast_entity) != "solcast_solar":
            return {}
        base = strip_forecast_suffix(forecast_entity, SOLCAST_ENDINGS)
        result = {}
        for ending in SOLCAST_ENDINGS:
            if (state := self.hass.states.get(f"{base}_{ending}")) is not None:
                result[state.entity_id] = list(state.attributes.get("detailedHourly") or [])
        return result

    async def _async_weather_forecasts(self) -> dict:
        result: dict[str, Any] = {"hourly": {}, "daily": {}}
        if not self.hass.services.has_service("weather", "get_forecasts"):
            _LOGGER.error("Forecast service not available: weather.get_forecasts")
            return result
        for forecast_type, key in (("hourly", "hourly_service_entity_id"), ("daily", "daily_service_entity_id")):
            entity_id = self.config["forecast"]["entity_ids"].get(key)
            if not entity_id:
                continue
            try:
                async with asyncio.timeout(FORECAST_TIMEOUT):
                    response = await self.hass.services.async_call(
                        "weather", "get_forecasts", {"entity_id": entity_id, "type": forecast_type}, blocking=True, return_response=True
                    )
                if entity_id not in (response or {}):
                    raise HomeAssistantError(f"{entity_id} not in return dict")
                result[forecast_type] = list(response[entity_id].get("forecast", []))
            except (HomeAssistantError, TimeoutError) as err:
                _LOGGER.error("Cant get %s forecast dict: %s", forecast_type, err)
        return result

    async def _async_setup_buffers(self) -> None:
        entities = self.buffered_entities
        if not entities:
            return
        for entity_id in entities:
            self._buffers[entity_id] = []

        @callback
        def _state_listener(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            rows = self._buffers.setdefault(event.data["entity_id"], [])
            row = (new_state.last_updated.timestamp(), new_state.state)
            if rows and row[0] <= rows[-1][0]:
                if row[0] == rows[-1][0]:
                    rows[-1] = row
                return
            rows.append(row)

        self._unsubs.append(async_track_state_change_event(self.hass, entities, _state_listener))

        if "recorder" not in self.hass.config.components:
            _LOGGER.warning("Recorder is not loaded, Cable Juice Planner starts without history")
            return

        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415

        end = dt_util.utcnow()
        groups: dict[float, list[str]] = {}
        for entity_id in entities:
            groups.setdefault(self._retention[entity_id], []).append(entity_id)
        for hours, group in groups.items():
            try:
                states = await get_instance(self.hass).async_add_executor_job(
                    partial(
                        history.get_significant_states,
                        self.hass,
                        end - datetime.timedelta(hours=hours),
                        end,
                        group,
                        None,
                        include_start_time_state=True,
                        significant_changes_only=False,
                        minimal_response=False,
                        no_attributes=True,
                    )
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Could not load history: %s", err)
                continue
            for entity_id in group:
                loaded = []
                for state in states.get(entity_id, []):
                    try:
                        loaded.append((state.last_updated.timestamp(), state.state))
                    except AttributeError:
                        continue
                self._buffers[entity_id] = merge_rows(loaded, self._buffers.get(entity_id, []))
        await self._async_refresh_statistics(force=True)

    async def _async_odometer_history(self, charging_history) -> dict | None:
        """Odometer around history entries without one (load_charging_history added them)."""
        odometer = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
        if not odometer or not isinstance(charging_history, dict) or "recorder" not in self.hass.config.components:
            return None
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415

        instance = get_instance(self.hass)
        oldest = self._local_now() - datetime.timedelta(days=getattr(instance, "keep_days", 10))
        keys = [key for key, value in charging_history.items() if isinstance(key, datetime.datetime) and isinstance(value, dict) and "odometer" not in value and key >= oldest]
        result: dict = {}
        tz = dt_util.get_default_time_zone()
        for key in keys[:200]:
            start = key.replace(tzinfo=tz)
            try:
                states = await instance.async_add_executor_job(
                    partial(
                        history.get_significant_states, self.hass, start, start + datetime.timedelta(minutes=1), [odometer], None,
                        include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                    )
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not load odometer history for %s: %s", key, err)
                continue
            rows = []
            for state in states.get(odometer, []):
                ts = state.last_updated.timestamp()
                rows.append((max(ts, start.timestamp()), state.state))
            if rows:
                result[key] = rows
        return result

    async def _async_refresh_statistics(self, force: bool = False) -> None:
        hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        if not force and time.monotonic() - self._statistics_refreshed < STATISTICS_REFRESH_SECONDS and hour == self._statistics_hour:
            return
        if "recorder" not in self.hass.config.components or not self.power_entities:
            return
        self._statistics_refreshed = time.monotonic()
        self._statistics_hour = hour

        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        end = dt_util.utcnow()
        hours = HISTORY_BUFFER_HOURS if force or not self._statistics else 4
        start = (end - datetime.timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, set(self.power_entities), "hour", None, {"mean"}
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not load statistics: %s", err)
            return
        for entity_id in self.power_entities:
            rows = []
            for row in stats.get(entity_id, []):
                start_value = row.get("start")
                if isinstance(start_value, datetime.datetime):
                    start_value = start_value.timestamp()
                if start_value is None:
                    continue
                rows.append((float(start_value), row.get("mean")))
            self._statistics[entity_id] = merge_rows(self._statistics.get(entity_id, []), rows)

    def _prune_buffers(self) -> None:
        now = time.time()
        for entity_id, rows in self._buffers.items():
            cutoff = now - self._retention.get(entity_id, HISTORY_SHORT_BUFFER_HOURS) * 3600
            index = bisect.bisect_left(rows, (cutoff,))
            if index > 1:
                del rows[: index - 1]
        cutoff = now - HISTORY_BUFFER_HOURS * 3600 - 3600
        for rows in self._statistics.values():
            del rows[: bisect.bisect_left(rows, (cutoff,))]
