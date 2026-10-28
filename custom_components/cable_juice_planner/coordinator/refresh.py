"""Charging loss, economy and analysis refresh."""
from __future__ import annotations

import datetime
import logging
from functools import partial
from typing import Any

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..const import ENTITY_UNAVAILABLE_STATES
from ..engine.loss import LOSS_GROUP_SIZE, as_series, resample_steps
from .constants import CHARGING_LOSS_STATISTICS_CHUNK_DAYS, CHARGING_LOSS_STATISTICS_DAYS, CONNECTED_STATUS

_LOGGER = logging.getLogger(__name__)


class CoordinatorRefresh:
    """Reads long-term statistics for the charging loss, economy and analysis."""

    @callback
    def _charging_loss_hourly(self, _now) -> None:
        self._background(self._async_charging_loss_refresh(), "charging_loss_history")

    async def _async_charging_loss_refresh(self, backfill: bool = False) -> None:
        """Find charging loss segments in the recorder: the last day every hour, at the first start
        the whole raw history and, before it, the hourly long-term statistics."""
        if not self.ev_configured or "recorder" not in self.hass.config.components:
            return
        async with self._charging_loss_lock:
            await self._async_charging_loss_read(backfill)

    async def _async_charging_loss_read(self, backfill: bool) -> None:
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        charger = self.config["charger"]["entity_ids"]
        soc_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
        meter_id = charger["kwh_meter_entity_id"]
        power_id = charger["power_consumtion_entity_id"]
        if not (soc_id and meter_id and power_id):
            return
        phase_ids = [f"sensor.{self.object_prefix}_charger_phase_{i}_amps" for i in (1, 2, 3)]
        end = dt_util.utcnow()
        raw_start = end - datetime.timedelta(days=getattr(instance, "keep_days", 10) if backfill else 1.1)
        ids = [soc_id, meter_id, *phase_ids]

        def raw_history(entity_ids):
            return instance.async_add_executor_job(
                partial(
                    history.get_significant_states, self.hass, raw_start, end, entity_ids, None,
                    include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                )
            )

        try:
            metadata = await instance.async_add_executor_job(partial(get_metadata, self.hass, statistic_ids={*ids, power_id}))
            states = await raw_history(ids)
            power_stats = await instance.async_add_executor_job(
                statistics_during_period, self.hass, raw_start, end, {power_id}, "5minute", None, {"mean", "min", "max"}
            )
            if not power_stats.get(power_id):
                # no statistics for the power sensor (no state_class): its raw readings, which can be many
                states.update(await raw_history([power_id]))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Charging loss: could not load the history: %s", err)
            return

        def unit(entity_id):
            if entity_id in metadata:
                return metadata[entity_id][1].get("unit_of_measurement")
            state = self.hass.states.get(entity_id)
            return state.attributes.get("unit_of_measurement") if state else None

        power_factor = 1000.0 if unit(power_id) == "kW" else 1.0
        meter_factor = {"Wh": 0.001, "MWh": 1000.0}.get(unit(meter_id), 1.0)

        def series(entity_id, factor=1.0):
            rows = [(state.last_changed.timestamp(), state.state) for state in states.get(entity_id, [])]
            return [(ts, value * factor) for ts, value in as_series(rows)]

        def stat_ts(row):
            start = row.get("start")
            return start.timestamp() if isinstance(start, datetime.datetime) else float(start)

        power_rows = [
            (stat_ts(row), row["mean"] * power_factor, row["min"] * power_factor, row["max"] * power_factor)
            for row in power_stats.get(power_id, []) if None not in (row.get("mean"), row.get("min"), row.get("max"))
        ]
        if not power_rows:
            # no statistics for the power sensor: the raw readings in 5 minute steps
            power_rows = resample_steps(series(power_id, power_factor), raw_start.timestamp(), end.timestamp())
        data: dict[str, Any] = {
            "soc": series(soc_id),
            "meter": series(meter_id, meter_factor),
            "power": power_rows,
            "phases": [series(entity_id) for entity_id in phase_ids],
        }

        _LOGGER.debug("Charging loss: history %s battery level, %s meter, %s power rows", len(data["soc"]), len(data["meter"]), len(data["power"]))
        groups = (await self.async_run_job("charging_loss_update", {**data, "backfill": False})).value or {}
        if not backfill:
            return

        # older charging from the hourly statistics, 30 days at a time, only as far back as a
        # group still misses measurements (a full group keeps its newest anyway)
        chunk_end = raw_start
        for _ in range(CHARGING_LOSS_STATISTICS_DAYS // CHARGING_LOSS_STATISTICS_CHUNK_DAYS):
            if groups and all(count >= LOSS_GROUP_SIZE for count in groups.values()):
                break
            chunk_start = chunk_end - datetime.timedelta(days=CHARGING_LOSS_STATISTICS_CHUNK_DAYS)
            try:
                hourly = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, chunk_start, chunk_end, {soc_id, meter_id, power_id}, "hour", None, {"mean", "min", "max", "change"}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Charging loss: could not load the statistics: %s", err)
                break
            if not hourly.get(power_id):
                break  # before the statistics of the power sensor began
            hours: dict[float, dict] = {}
            for row in hourly.get(power_id, []):
                if row.get("mean") is not None:
                    hours.setdefault(stat_ts(row), {})
                    hours[stat_ts(row)].update(power_mean=row["mean"] * power_factor, power_min=(row.get("min") or 0.0) * power_factor, power_max=(row.get("max") or 0.0) * power_factor)
            for row in hourly.get(meter_id, []):
                if row.get("change") is not None and stat_ts(row) in hours:
                    hours[stat_ts(row)]["kwh"] = row["change"] * meter_factor
            for row in hourly.get(soc_id, []):
                if row.get("mean") is not None and stat_ts(row) in hours:
                    hours[stat_ts(row)]["soc"] = row["mean"]
            _LOGGER.debug("Charging loss: statistics %s - %s, %s hours", chunk_start.date(), chunk_end.date(), len(hours))
            groups = (await self.async_run_job("charging_loss_update", {"hours": [{"start": ts, **values} for ts, values in hours.items()]})).value or groups
            chunk_end = chunk_start
        await self.async_run_job("charging_loss_update", {"backfill": True})
        _LOGGER.info("Charging loss: history read back to %s, measurements per group %s", chunk_end.date(), groups)

    @callback
    def _economy_daily(self, _now) -> None:
        self._background(self._async_economy_refresh(), "economy")

    async def _async_economy_refresh(self) -> None:
        """Hourly prices and the odometer per month from the long-term statistics; the first
        time also the connection windows of the last days from the charger's status."""
        if "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        prices_cfg = self.config["prices"]["entity_ids"]
        price_id = prices_cfg["energidataservice_buy_entity_id"] or prices_cfg["stromligning_buy_current_price"]
        odometer_id = self.config["ev_car"]["entity_ids"]["odometer_entity_id"] if self.ev_configured else ""
        status_id = self.config["charger"]["entity_ids"]["status_entity_id"]
        end = dt_util.utcnow()
        keep_days = getattr(instance, "keep_days", 10)
        backfill = not self.engine.economy_db.get("backfilled")
        data: dict[str, Any] = {}

        def stat_ts(row):
            start = row.get("start")
            return start.timestamp() if isinstance(start, datetime.datetime) else float(start)

        try:
            if price_id:
                stats = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, end - datetime.timedelta(days=keep_days + 5), end, {price_id}, "hour", None, {"mean"}
                )
                data["prices"] = {stat_ts(row): row["mean"] for row in stats.get(price_id, []) if row.get("mean") is not None}
                if not data["prices"]:
                    # no long-term statistics (no state_class): the raw history, the price changes every hour
                    start = end - datetime.timedelta(days=keep_days)
                    raw = await instance.async_add_executor_job(
                        partial(
                            history.get_significant_states, self.hass, start, end, [price_id], None,
                            include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                        )
                    )
                    series = as_series((state.last_changed.timestamp(), state.state) for state in raw.get(price_id, []))
                    data["prices"] = {row[0]: row[1] for row in resample_steps(series, start.timestamp(), end.timestamp(), 3600.0)}
            if odometer_id:
                months = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, end - datetime.timedelta(days=400), end, {odometer_id}, "month", None, {"change"}
                )
                data["odometer_months"] = {
                    dt_util.as_local(dt_util.utc_from_timestamp(stat_ts(row))).strftime("%Y-%m"): row["change"]
                    for row in months.get(odometer_id, []) if row.get("change") is not None
                }
            if backfill and status_id:
                states = await instance.async_add_executor_job(
                    partial(
                        history.get_significant_states, self.hass, end - datetime.timedelta(days=keep_days), end, [status_id], None,
                        include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                    )
                )
                windows, start = [], None
                for state in states.get(status_id, []):
                    connected = state.state in CONNECTED_STATUS
                    if connected and start is None:
                        start = state.last_changed.timestamp()
                    elif not connected and start is not None and state.state not in ENTITY_UNAVAILABLE_STATES:
                        windows.append((start, state.last_changed.timestamp()))
                        start = None
                data["windows"] = windows
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Economy: could not load the statistics: %s", err)
            return
        _LOGGER.debug("Economy: %s hourly prices, %s odometer months, %s windows", len(data.get("prices", {})), len(data.get("odometer_months", {})), len(data.get("windows", [])))
        await self.async_run_job("economy_update", data)
        if backfill:
            await self.async_run_job("economy_mark_backfilled")

    @callback
    def _analysis_daily(self, _now) -> None:
        self._background(self._async_analysis_refresh(), "analysis")

    async def _async_analysis_refresh(self) -> None:
        """Hourly outdoor temperatures for the drives; the first time also the drives of the
        recorder's raw history (unplugging to plugging in, odometer and battery level)."""
        if not self.ev_configured or "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        temp_id = self.config["forecast"]["entity_ids"]["outdoor_temp_entity_id"]
        ev = self.config["ev_car"]["entity_ids"]
        status_id = self.config["charger"]["entity_ids"]["status_entity_id"]
        end = dt_util.utcnow()
        keep_days = getattr(instance, "keep_days", 10)
        backfill = not self.engine.analysis_backfilled()
        data: dict[str, Any] = {"backfill": backfill}
        try:
            if temp_id:
                stats = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, end - datetime.timedelta(days=keep_days + 5), end, {temp_id}, "hour", None, {"mean"}
                )
                data["temps"] = {
                    (row["start"].timestamp() if isinstance(row["start"], datetime.datetime) else float(row["start"])): row["mean"]
                    for row in stats.get(temp_id, []) if row.get("mean") is not None
                }
            if backfill and status_id and ev["odometer_entity_id"] and ev["usable_battery_level_entity_id"]:
                ids = [status_id, ev["odometer_entity_id"], ev["usable_battery_level_entity_id"]]
                states = await instance.async_add_executor_job(
                    partial(
                        history.get_significant_states, self.hass, end - datetime.timedelta(days=keep_days), end, ids, None,
                        include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                    )
                )
                data["status"] = [
                    (state.last_changed.timestamp(), state.state in CONNECTED_STATUS)
                    for state in states.get(status_id, []) if state.state not in ENTITY_UNAVAILABLE_STATES
                ]
                data["odometer"] = as_series((state.last_changed.timestamp(), state.state) for state in states.get(ev["odometer_entity_id"], []))
                data["soc"] = as_series((state.last_changed.timestamp(), state.state) for state in states.get(ev["usable_battery_level_entity_id"], []))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Analysis: could not load the history: %s", err)
            return
        _LOGGER.debug("Analysis: %s hourly temperatures, %s status changes", len(data.get("temps", {})), len(data.get("status", [])))
        await self.async_run_job("analysis_update", data)

    async def async_recalculate_charging_loss(self) -> None:
        """button recalculate_charging_loss: forget the measurements and read the whole history again."""
        await self.async_run_job("charging_loss_reset")
        await self._async_charging_loss_refresh(backfill=True)
