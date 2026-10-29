"""Event loop side of Cable Juice Planner.

The coordinator only does light work on the event loop: it collects a plain
:class:`Snapshot` of Home Assistant data, sends the calculation to the worker thread of
the config entry and publishes the result. All heavy work (planning, history,
predictions, markdown) runs in the worker.

The integration does not send commands to the charger or the car. The decisions are
published as action entities (see engine/control) for the user's own automations.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from logging.handlers import QueueHandler, QueueListener
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CAR_NAME,
    CONF_ENTITY_PREFIX,
    DOMAIN,
    HISTORY_BUFFER_HOURS,
    HISTORY_LONG_BUFFER_HOURS,
    HISTORY_SHORT_BUFFER_HOURS,
    SIGNAL_UPDATE,
    build_config,
    default_options,
    instance_title,
    is_ev_configured,
    is_solar_configured,
    normalize_prefix,
    object_prefix,
)
from ..management.debug_export import DebugRecorder
from ..engine import CJPEngine, JobResult
from ..utils.formatting import translate
from ..management.storage import CJPStorage
from .data import CoordinatorData
from .events import CoordinatorEvents
from .lifecycle import CoordinatorLifecycle
from .refresh import CoordinatorRefresh

_LOGGER = logging.getLogger(__name__)


class CJPCoordinator(CoordinatorLifecycle, CoordinatorEvents, CoordinatorData, CoordinatorRefresh):
    """Owns the engine, the worker thread and all scheduling for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.prefix: str = normalize_prefix(entry.data.get(CONF_ENTITY_PREFIX))
        self.object_prefix: str = object_prefix(self.prefix)
        self.car_name: str = (entry.data.get(CONF_CAR_NAME) or "").strip()
        self.config = build_config({**default_options(), **entry.options})
        self.testing = bool(self.config.get("testing_mode"))
        self.ev_configured = is_ev_configured(self.config)
        self.solar_configured = is_solar_configured(self.config)
        self.texts: dict[str, str] = {}
        self.engine: CJPEngine | None = None
        self.storage = CJPStorage(hass, entry.entry_id)

        self.data: dict[str, Any] = {}
        self.attributes: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {}
        # platform.key -> state from an imported backup, used by the entities instead of their last state
        self.restore_settings: dict[str, str] = {}
        self.setting_limits: dict[str, dict[str, float]] = {}
        self.started = False
        self.ignore_next_update = False

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{self.object_prefix}_worker")
        self._job_lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._started_unsub: Callable[[], None] | None = None
        self._settings_debounce: Callable[[], None] | None = None
        self._charging_loss_timeout: Callable[[], None] | None = None
        self._charging_loss_lock = asyncio.Lock()  # the history is read by one task at a time
        self._public_cost_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

        self._buffers: dict[str, list[tuple[float, str]]] = {}
        self._retention: dict[str, float] = {}
        self._statistics: dict[str, list[tuple[float, float | None]]] = {}
        self._statistics_refreshed = 0.0
        self._statistics_hour: datetime.datetime | None = None
        self._platforms: dict[str, str | None] = {}
        self._price_snapshot_time = 0.0
        self._log_listener: QueueListener | None = None
        self._log_handler: QueueHandler | None = None
        self.last_job: dict[str, Any] = {}
        # last jobs and snapshots for the debug export (management/debug_export.py)
        self.debug = DebugRecorder()

        charger = self.config["charger"]["entity_ids"]
        ev = self.config["ev_car"]["entity_ids"]
        home = self.config["home"]["entity_ids"]
        solar = self.config["solar"]["entity_ids"]
        self.price_entities = sorted({e for e in self.config["prices"]["entity_ids"].values() if e})
        self.power_entities = sorted({
            e for e in (
                home["power_consumption_entity_id"],
                home["powerwall_watt_flow_entity_id"],
                solar["production_entity_id"],
                charger["power_consumtion_entity_id"],
                *(home["ignore_consumption_from_entity_ids"] or []),
            ) if e
        })
        own_battery_level = f"number.{self.object_prefix}_battery_level"
        self.long_entities = sorted({e for e in (charger["status_entity_id"], ev["usable_battery_level_entity_id"] if self.ev_configured else own_battery_level) if e})
        entities: set[str] = set()
        for section in self.config.values():
            if isinstance(section, dict) and "entity_ids" in section:
                for value in section["entity_ids"].values():
                    entities.update(value if isinstance(value, list) else [value])
        self.configured_entities = sorted((entities - set(self.price_entities)) - {""})
        self.buffered_entities = sorted(set(self.configured_entities) | set(self.long_entities))
        for entity_id in self.buffered_entities:
            if entity_id in self.long_entities:
                self._retention[entity_id] = HISTORY_LONG_BUFFER_HOURS
            elif entity_id in self.power_entities:
                self._retention[entity_id] = HISTORY_BUFFER_HOURS
            else:
                self._retention[entity_id] = HISTORY_SHORT_BUFFER_HOURS

    # ------------------------------------------------------------------ #
    # Basics
    # ------------------------------------------------------------------ #

    @property
    def legacy_prefix(self) -> str | None:
        """Prefix of the PyScript installation chosen for import in the config flow."""
        return self.entry.data.get("legacy_prefix")

    @property
    def signal(self) -> str:
        return SIGNAL_UPDATE.format(entry_id=self.entry.entry_id)

    def t(self, key: str, default: str | None = None, **fmt) -> str:
        return translate(self.texts, key, default, **fmt)

    @property
    def distance_unit(self) -> str:
        """km or mi: the unit system of Home Assistant, the car's distance sensors are shown in it."""
        return str(self.hass.config.units.length_unit)

    @property
    def title(self) -> str:
        return instance_title(self.prefix, self.car_name)

    # ------------------------------------------------------------------ #
    # Site (load balancing with the other cars of the grid connection)
    # ------------------------------------------------------------------ #

    @property
    def site(self):
        return self.hass.data.get(DOMAIN, {}).get("site")

    @property
    def site_priority(self) -> int:
        return int(self.config["site"]["priority"])

    @property
    def site_phase_order(self) -> str:
        return str(self.config["site"]["phase_order"])

    @callback
    def async_site_recalculate(self) -> None:
        """The capacity changed (a car ahead changed its plan, the site was added/removed)."""
        if self.started:
            self._background(self.async_run_job("recalculate", include_forecast=True), "site_recalculate")

    @callback
    def async_site_limit(self) -> None:
        """A phase is above the fuse: apply the live limit now."""
        if self.started:
            self._background(self.async_run_job("site_limit"), "site_limit")

    def _local_now(self) -> datetime.datetime:
        return dt_util.now().replace(tzinfo=None, microsecond=0)

    def _background(self, coro, name: str) -> None:
        self.entry.async_create_background_task(self.hass, coro, name=f"cjp_{self.prefix}_{name}")

    # ------------------------------------------------------------------ #
    # Setup / unload
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Startup sequence
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Settings entities
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # State publishing
    # ------------------------------------------------------------------ #

    async def _async_publish(self, result: JobResult, job_name: str) -> None:
        keys = set(result.states) | set(result.attributes)
        for key in keys:
            if key in result.states:
                self.data[key] = result.states[key]
            if key in result.attributes:
                self.attributes[key] = result.attributes[key]
        if self.site is not None:
            if result.site is not None:
                self.site.update_from_result(self, result.site)
            # a shared circuit: its sum replaces the own circuit limit before the action is published
            if self.site.circuit_patch(self):
                keys.add("charger_action")
        self.last_job = {
            "job": job_name,
            "worker_thread": result.worker_thread,
            "duration_seconds": round(result.duration, 3),
            "finished": dt_util.now().isoformat(timespec="seconds"),
            "error": str(result.error) if result.error else None,
        }
        rule_attributes = self.attributes.setdefault("current_charging_rule", {})
        rule_attributes.update(last_job=job_name, worker_thread=result.worker_thread, calculation_seconds=round(result.duration, 3))
        keys.add("current_charging_rule")
        for key, limits in result.setting_limits.items():
            self.setting_limits.setdefault(key, {}).update(limits)
            keys.add(key)
        for key in keys:
            async_dispatcher_send(self.hass, self.signal, key)

        for key, value in result.settings.items():
            self.async_set_setting(key, value, source="engine")
        for event_type, data in result.events:
            async_dispatcher_send(self.hass, self.signal, ("event", event_type, data))

        for message, title, notification_id in result.notifications:
            persistent_notification.async_create(self.hass, message, title=title, notification_id=notification_id)
        for notification in result.mobile_notifications:
            await self._async_notify(notification)

        for name, encoded in result.db_saves.items():
            try:
                await self.storage.async_save(name, encoded)
                _LOGGER.debug("Saved database %s", name)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Could not save database %s: %s", name, err)

        if "charging_loss" in result.config_updates:
            self.ignore_next_update = True
            self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, "charging_loss": result.config_updates["charging_loss"]})
        if result.request_reload:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    async def _async_notify(self, notification: dict) -> None:
        """mynotify.my_notify: the configured notify services (default: notify.notify)."""
        services = self.config.get("notify_list") or ["notify"]
        for target in services:
            name = target.split(".", 1)[1] if "." in target else target
            try:
                if self.hass.services.has_service("notify", name):
                    await self.hass.services.async_call("notify", name, notification, blocking=False)
                elif self.hass.services.has_service("notify", "send_message") and self.hass.states.get(f"notify.{name}") is not None:
                    data = {"entity_id": f"notify.{name}", "message": notification["message"], "title": notification["title"]}
                    await self.hass.services.async_call("notify", "send_message", data, blocking=False)
                else:
                    _LOGGER.warning("Notify service notify.%s not found. Notification not sent", name)
            except HomeAssistantError as err:
                _LOGGER.error("Error in service call notify.%s: %s", name, err)

    # ------------------------------------------------------------------ #
    # Worker jobs
    # ------------------------------------------------------------------ #

    async def async_run_job(self, job_name: str, *args, include_prices: bool = False, include_forecast: bool = False) -> JobResult:
        """Collect a snapshot on the event loop and run the job in the worker thread."""
        async with self._job_lock:
            snapshot = await self._async_snapshot(include_prices=include_prices, include_forecast=include_forecast)
            func = getattr(self.engine, job_name)
            result: JobResult = await self.hass.loop.run_in_executor(self._executor, partial(self.engine.run_job, snapshot, func, *args))
            await self._async_publish(result, job_name)
            self.debug.record(job_name, args, snapshot, result, self.data.get("current_charging_rule"))
            _LOGGER.debug("Job %s ran in %s for %.3fs", job_name, result.worker_thread, result.duration)
            return result

    # ------------------------------------------------------------------ #
    # Recorder buffers
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Charging loss per power (engine/loss.py)
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Charging economy per month (engine/economy.py)
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Analysis: drives against the outdoor temperature (engine/analysis)
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Services
    # ------------------------------------------------------------------ #

    async def async_debug_info(self) -> str:
        extra = {
            "status": {
                "started": self.started,
                "buffered_rows": {e: len(rows) for e, rows in self._buffers.items()},
                "statistics_rows": {e: len(rows) for e, rows in self._statistics.items()},
            },
            "worker": self.last_job,
        }
        result = await self.async_run_job("debug_info", extra)
        return result.value or ""

    @callback
    def async_enforce_planning(self) -> None:
        self.async_press("enforce_planning")

    async def async_add_public_charging_session(self, **data) -> dict:
        result = await self._async_run_kwargs("manual_add_public_charging_session", data)
        return result.value or {}

    async def _async_run_kwargs(self, job_name: str, kwargs: dict) -> JobResult:
        async with self._job_lock:
            snapshot = await self._async_snapshot()
            func = partial(getattr(self.engine, job_name), **kwargs)
            result: JobResult = await self.hass.loop.run_in_executor(self._executor, partial(self.engine.run_job, snapshot, func))
            await self._async_publish(result, job_name)
            self.debug.record(job_name, (kwargs,), snapshot, result, self.data.get("current_charging_rule"))
            return result
