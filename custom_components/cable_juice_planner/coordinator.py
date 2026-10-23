"""Event loop side of Cable Juice Planner.

The coordinator only does light work on the event loop: it collects a plain
:class:`Snapshot` of Home Assistant data, sends the calculation to the worker thread of
the config entry and publishes the result. All heavy work (planning, history,
predictions, markdown) runs in the worker.

The integration does not send commands to the charger or the car. The decisions are
published as action entities (see engine_control) for the user's own automations.
"""
from __future__ import annotations

import asyncio
import bisect
import datetime
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from queue import SimpleQueue
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import sun as sun_helper
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_change
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTITY_PREFIX,
    DOMAIN,
    ENTITY_UNAVAILABLE_STATES,
    HISTORY_BUFFER_HOURS,
    HISTORY_LONG_BUFFER_HOURS,
    HISTORY_SHORT_BUFFER_HOURS,
    SIGNAL_UPDATE,
    TITLE,
    build_config,
    default_options,
    is_ev_configured,
    CONF_CAR_NAME,
    instance_title,
    is_solar_configured,
    normalize_language,
    normalize_prefix,
    object_prefix,
)
from .engine import CJPEngine, JobResult, Snapshot
from .engine_energy import SOLCAST_ENDINGS, strip_forecast_suffix
from .formatting import format_charging_rule, translate
from .helpers import float_or_none
from .history import merge_rows
from .prices import PRICE_SNAPSHOT_SECONDS, TARIFF_STATE_KEYS
from .storage import CJPStorage, load_legacy_databases

_LOGGER = logging.getLogger(__name__)

STATISTICS_REFRESH_SECONDS = 300
SETTINGS_DEBOUNCE_SECONDS = 5
FORECAST_TIMEOUT = 30
PUBLIC_CHARGING_COST_DELAY = 60
BATTERY_LEVEL_WAIT = datetime.timedelta(minutes=30)

# Settings with their own trigger (as the PyScript state triggers)
RULE_KEYS = ("fill_up", "workplan_charging", "deactivate_script")
CHARGE_TRIGGER_KEYS = ("allow_manual_charging_now", "allow_manual_charging_solar", "calculate_charging_loss")
TRIP_KEYS = ("trip_charge_procent", "trip_range_needed", "trip_date_time", "trip_homecoming_date_time")


class CJPCoordinator:
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
        self.setting_limits: dict[str, dict[str, float]] = {}
        self.started = False
        self.ignore_next_update = False

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{self.object_prefix}_worker")
        self._job_lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._started_unsub: Callable[[], None] | None = None
        self._settings_debounce: Callable[[], None] | None = None
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

    async def async_setup(self) -> None:
        language = normalize_language(self.config.get("language"), normalize_language(self.hass.config.language))
        prefix = f"component.{DOMAIN}.runtime."
        translations = await async_get_translations(self.hass, language, "runtime", [DOMAIN])
        self.texts = {key.removeprefix(prefix): value for key, value in translations.items() if key.startswith(prefix)}
        self.engine = CJPEngine(self.config, self.object_prefix, self.texts, title=self.title)
        if self.site is not None:
            self.site.register(self)

        if self.config.get("enable_diagnostic_file_log"):
            await self._async_enable_file_log()

        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        for entity_id in {*self.configured_entities, *self.price_entities}:
            entry = ent_reg.async_get(entity_id)
            platform = entry.platform if entry else None
            if entry and entry.device_id and (device := dev_reg.async_get(entry.device_id)) and device.identifiers:
                # ev.py used the domain of the first device identifier
                platform = next(iter(device.identifiers))[0]
            self._platforms[entity_id] = platform

        self.set_charging_rule(f"📟{self.t('ui.init.script_starting')}")

    @callback
    def async_start(self) -> None:
        """Start once the platforms (and the restored settings) are set up."""
        if self.hass.state is CoreState.running:
            self._start_background()
        else:
            self._started_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._start_background)

    @callback
    def _start_background(self, _event: Event | None = None) -> None:
        self._started_unsub = None
        self._background(self._async_startup(), "startup")

    async def async_unload(self) -> None:
        if self.site is not None:
            self.site.unregister(self.entry.entry_id)
        if self._started_unsub:
            self._started_unsub()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._settings_debounce:
            self._settings_debounce()
        for task in (self._public_cost_task, *self._tasks):
            if task and not task.done():
                task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        await self._async_disable_file_log()

    async def _async_enable_file_log(self) -> None:
        log_path = self.hass.config.path(f"cable_juice_planner_{self.prefix}.log")

        def _create_handler() -> RotatingFileHandler:
            handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s (%(threadName)s) [%(name)s] %(message)s"))
            return handler

        file_handler = await self.hass.async_add_executor_job(_create_handler)
        queue: SimpleQueue = SimpleQueue()
        self._log_handler = QueueHandler(queue)
        self._log_handler.setLevel(logging.DEBUG)
        self._log_listener = QueueListener(queue, file_handler)
        self._log_listener.start()
        package_logger = logging.getLogger(__package__)
        package_logger.addHandler(self._log_handler)
        if package_logger.getEffectiveLevel() > logging.DEBUG:
            package_logger.setLevel(logging.DEBUG)

    async def _async_disable_file_log(self) -> None:
        if self._log_handler:
            logging.getLogger(__package__).removeHandler(self._log_handler)
            self._log_handler = None
        if self._log_listener:
            listener = self._log_listener
            self._log_listener = None
            await self.hass.async_add_executor_job(listener.stop)

    # ------------------------------------------------------------------ #
    # Startup sequence
    # ------------------------------------------------------------------ #

    async def _async_startup(self) -> None:
        t = self.t
        log_lines: list[str] = []
        _LOGGER.info("Cable Juice Planner %s starting, calculations run in thread cjp_%s_worker_0", self.prefix, self.prefix)
        try:
            await self._async_validate_entities()

            self.set_charging_rule(f"📟{t('ui.startup.loading_db')}")
            databases = await self.storage.async_load()
            missing = [name for name, value in databases.items() if value is None]
            if missing and self.legacy_prefix:
                legacy = await self.hass.async_add_executor_job(load_legacy_databases, self.hass.config.config_dir, self.legacy_prefix)
                imported = [name for name in missing if name in legacy]
                for name in imported:
                    databases[name] = legacy[name]
                if imported:
                    log_lines.append(f"📟{t('ui.startup.imported_databases', databases=', '.join(sorted(imported)))}")
            if databases.get("runtime") is None and self.legacy_prefix:
                databases["runtime"] = await self._async_legacy_runtime_state()

            self.set_charging_rule(f"📟{t('ui.startup.loading_history')}")
            await self._async_setup_buffers()
            _LOGGER.info("History buffers: %s", ", ".join(f"{e} {len(rows)}" for e, rows in self._buffers.items()) or "none")
            odometer_history = await self._async_odometer_history(databases.get("charging_history"))

            result = await self.async_run_job("startup", databases, odometer_history, include_prices=True, include_forecast=True)
            info = result.value or {}
            configured = t("ui.startup.configuration.configured")
            not_configured = t("ui.startup.configuration.not_configured")
            distance = t("ui.common.distance_type")
            log_lines[:0] = [
                "---",
                f"### 🚗{TITLE}🔋🌞📅 ({self.prefix})",
                "",
                f"**{t('ui.startup.configuration.header')}:**",
                f"📟{t('ui.startup.configuration.charger')}: {configured if info.get('charger_configured') else not_configured}",
                f"📟{t('ui.startup.configuration.ev')}: {configured if info.get('ev_configured') else not_configured}",
                f"📟{t('ui.startup.configuration.solar')}: {configured if info.get('solar_configured') else not_configured}",
                f"📟{t('ui.startup.configuration.powerwall')}: {configured if info.get('powerwall_configured') else not_configured}",
                "",
            ]
            if info:
                log_lines += [
                    "",
                    f"🚗{t('ui.startup.ev.range')}: {info['range']:.2f} {distance}",
                    f"🚗{t('ui.startup.ev.battery_level')}: {info['battery_level']:.0f}%",
                    f"🚗{t('ui.common.distance_percentage')}: {info['distance_per_percentage']:.2f} {distance}",
                    f"🚗{t('ui.startup.ev.estimated_range')}: {info['estimated_range']:.2f} {distance}",
                    "",
                    *info.get("amps_report", "").splitlines(),
                    "",
                    f"📟{t('ui.startup.calc_charging_plan')}",
                ]
            self._register_triggers()
            self.started = True
            if self.last_job.get("worker_thread"):
                log_lines.append(f"📟Worker: {self.last_job['worker_thread']}")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error during Cable Juice Planner startup")
            log_lines.append(f"⛔Error during startup: {err}")
            self.set_charging_rule(f"⛔{t('ui.init.script_stopped')}.\n{t('ui.init.check_log')}:\n{err}")
        finally:
            for line in log_lines:
                if line.strip("-# "):
                    _LOGGER.info(line.lstrip("#* ").replace("**", ""))
            persistent_notification.async_create(
                self.hass, "\n".join(log_lines), title=f"📟{self.title} started", notification_id=f"{self.prefix}_startup_notification"
            )

    async def _async_legacy_runtime_state(self) -> dict:
        """State the PyScript version kept in helpers and template sensors.

        Right after a restart the PyScript template sensors are 'unavailable' until
        PyScript has set them, so like get_state(try_history=True) the last value of the
        last 24 hours in the recorder is used then.
        """
        legacy = self.legacy_prefix
        last_battery_level = f"sensor.{legacy}_drive_efficiency_last_battery_level"
        public_session = f"binary_sensor.{legacy}_public_charging_session_done"
        numeric = {
            "co2_emitted": f"input_number.{legacy}_co2_emitted",
            "kwh_charged_by_solar": f"input_number.{legacy}_kwh_charged_by_solar",
            "drive_efficiency_last_battery_level": last_battery_level,
            "drive_efficiency_last_odometer": f"sensor.{legacy}_drive_efficiency_last_odometer",
        }
        states: dict[str, State | None] = {entity_id: self.hass.states.get(entity_id) for entity_id in (*numeric.values(), public_session)}
        stale = [
            entity_id for entity_id, state in states.items()
            if state is None or (entity_id == public_session and state.state not in ("on", "off")) or (entity_id != public_session and float_or_none(state.state) is None)
        ]
        if stale and "recorder" in self.hass.config.components:
            from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415

            end = dt_util.utcnow()
            try:
                recorded = await get_instance(self.hass).async_add_executor_job(
                    partial(history.get_significant_states, self.hass, end - datetime.timedelta(days=1), end, stale, None,
                            include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=False)
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not load the history of the PyScript entities: %s", err)
                recorded = {}
            for entity_id in stale:
                for state in reversed(recorded.get(entity_id, [])):
                    if not isinstance(state, State):
                        continue
                    if (state.state in ("on", "off")) if entity_id == public_session else (float_or_none(state.state) is not None):
                        states[entity_id] = state
                        break

        runtime: dict[str, Any] = {"sensors": {}}
        for key, entity_id in numeric.items():
            state = states[entity_id]
            if state is not None and (value := float_or_none(state.state)) is not None:
                runtime["sensors"][key] = value
        if (state := states[last_battery_level]) is not None:
            runtime["last_battery_level_attributes"] = {
                key: value for key, value in state.attributes.items() if key.startswith("battery_level_expenses") or key == "last_drive_efficiency_data"
            }
        if (state := states[public_session]) is not None:
            runtime["public_charging_session_done"] = state.state if state.state in ("on", "off") else "off"
            runtime["public_charging_session"] = list(state.attributes.get("public_charging_session") or [])
        return runtime

    async def _async_validate_entities(self) -> None:
        t = self.t
        self.set_charging_rule(f"📟{t('ui.validation_procedure.entity_validation')}")
        entities = [*self.configured_entities, *self.price_entities]
        missing = [e for e in entities if self.hass.states.get(e) is None]
        if missing:
            _LOGGER.warning("Entities not found, retrying in 2 minutes: %s", ", ".join(missing))
            self.set_charging_rule(f"📟{t('ui.validation_procedure.entity_missing_try_again')}")
            await asyncio.sleep(120)
            missing = [e for e in entities if self.hass.states.get(e) is None]
            if missing:
                persistent_notification.async_create(
                    self.hass,
                    "- " + "\n- ".join(missing) + f"\n\n{t('ui.validation_procedure.check_entity_ids')}",
                    title=f"{self.title} {t('ui.validation_procedure.missing_entity_ids_title')}",
                    notification_id=f"{self.prefix}_validation_procedure_missing_entity_ids",
                )

        def _unavailable() -> list[str]:
            return [e for e in entities if (state := self.hass.states.get(e)) is not None and state.state in ENTITY_UNAVAILABLE_STATES]

        unavailable = _unavailable()
        if not missing and not unavailable:
            _LOGGER.info("All %s configured entities are present and available", len(entities))
        if unavailable:
            _LOGGER.warning("Entities unavailable, waiting up to 2 minutes: %s", ", ".join(unavailable))
            self.set_charging_rule(f"📟{t('ui.validation_procedure.entity_unavailable_try_again')}")
            for _ in range(24):
                await asyncio.sleep(5)
                unavailable = _unavailable()
                if not unavailable:
                    break
            if unavailable:
                persistent_notification.async_create(
                    self.hass,
                    "- " + "\n- ".join(unavailable) + f"\n\n{t('ui.validation_procedure.check_entity_ids')}",
                    title=f"{self.title} {t('ui.validation_procedure.unavailable_entity_ids_title')}",
                    notification_id=f"{self.prefix}_validation_procedure_unavailable_entity_ids",
                )
        self.set_charging_rule(f"📟{t('ui.init.script_starting')}")

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #

    def _register_triggers(self) -> None:
        interval = int(self.config["cron_interval"])
        track = partial(async_track_time_change, self.hass)
        self._unsubs.append(track(self._handle_cron, minute=f"/{interval}", second=0))
        self._unsubs.append(track(self._job_handler("hour_end", include_forecast=True), minute=59, second=0))
        self._unsubs.append(track(self._job_handler("price_update", include_prices=True), minute=2, second=0))
        self._unsubs.append(track(self._job_handler("append_prices"), hour=5, minute=0, second=0))
        if self.ev_configured:
            self._unsubs.append(track(self._job_handler("preheat_cron"), minute="/5", second=0))

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
            self._background(self.async_run_job("calculate_charging_loss", "status", value, old_value), "charging_loss")
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
        await self.async_run_job("charger_connected", value, include_forecast=True)

    @callback
    def _kwh_meter_changed(self, value, old_value) -> None:
        if self.settings.get("calculate_charging_loss"):
            self._background(self.async_run_job("calculate_charging_loss", "kwh_meter", value, old_value), "charging_loss")

    @callback
    def _charger_cable_changed(self, value, old_value) -> None:
        self._background(self.async_run_job("charger_cable_changed", value, old_value), "charger_cable")

    @callback
    def _ev_port_changed(self, value, old_value) -> None:
        self._background(self.async_run_job("ev_port_changed", value, old_value), "ev_port")

    # ------------------------------------------------------------------ #
    # Settings entities
    # ------------------------------------------------------------------ #

    @callback
    def async_set_setting(self, key: str, value: Any, source: str = "user") -> None:
        """A setting entity changed. source: initial, user (entity) or engine (job result)."""
        old = self.settings.get(key)
        self.settings[key] = value
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
            return key == "deactivate_script"
        if key in CHARGE_TRIGGER_KEYS:
            if key == "calculate_charging_loss":
                self._background(self.async_run_job("calculate_charging_loss", "switch", value), "charging_loss")
            self._background(self.async_run_job("charge_trigger", key, include_forecast=True), "charge_trigger")
            return True
        if key in TRIP_KEYS:
            self._background(self.async_run_job("trips_changed", key, value), "trips")
            return False
        if key == "public_charging_session_cost":
            self._background(self._async_public_cost_flow(value), "public_cost")
            return True
        if key == "battery_level" and not self.ev_configured:
            self._background(self.async_run_job("emulated_battery_level_changed", value, old), "battery_level")
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
        elif key == "restart_script":
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    async def _async_public_cost_flow(self, value) -> None:
        result = await self.async_run_job("public_charging_cost_changed", float_or_none(value))
        if result.value != "add":
            return
        if self._public_cost_task and not self._public_cost_task.done() and self._public_cost_task is not asyncio.current_task():
            self._public_cost_task.cancel()
        self._public_cost_task = asyncio.current_task()
        await asyncio.sleep(PUBLIC_CHARGING_COST_DELAY)
        await self.async_run_job("add_public_charging_session")

    # ------------------------------------------------------------------ #
    # State publishing
    # ------------------------------------------------------------------ #

    @callback
    def set_charging_rule(self, text: str) -> None:
        """Progress text during startup (the engine sets the rule afterwards)."""
        self.data["current_charging_rule"] = format_charging_rule(text, self.testing)
        async_dispatcher_send(self.hass, self.signal, "current_charging_rule")

    async def _async_publish(self, result: JobResult, job_name: str) -> None:
        keys = set(result.states) | set(result.attributes)
        for key in keys:
            if key in result.states:
                self.data[key] = result.states[key]
            if key in result.attributes:
                self.attributes[key] = result.attributes[key]
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
        if result.site is not None and self.site is not None:
            self.site.update_from_result(self, result.site)
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
                    _LOGGER.warning("Notify service notify.%s not found. Message not send: %s", name, notification["message"])
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
            _LOGGER.debug("Job %s ran in %s for %.3fs", job_name, result.worker_thread, result.duration)
            return result

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
        location, elevation = sun_helper.get_astral_location(self.hass)
        observer = sun_helper.get_astral_observer(self.hass) if hasattr(sun_helper, "get_astral_observer") else None
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
            astral=(location, elevation, observer),
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

    # ------------------------------------------------------------------ #
    # Recorder buffers
    # ------------------------------------------------------------------ #

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
            return result
