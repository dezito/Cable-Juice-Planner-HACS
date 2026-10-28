"""Setup, startup and unload of the coordinator."""
from __future__ import annotations

import asyncio
import datetime
import logging
from functools import partial
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from queue import SimpleQueue
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..management.backup import async_load_backup, async_restore_databases, pending_restores
from ..const import ENTITY_UNAVAILABLE_STATES, MIGRATED_LAST_FULL_CHARGE, TITLE, normalize_language
from ..engine import CJPEngine
from ..utils.formatting import load_texts
from ..utils.helpers import float_or_none
from ..management.storage import load_legacy_databases

_LOGGER = logging.getLogger(__name__)


class CoordinatorLifecycle:
    """Setup, startup, restore of a backup, file log and unload."""

    async def async_setup(self) -> None:
        language = normalize_language(self.config.get("language"), normalize_language(self.hass.config.language))
        self.texts = await self.hass.async_add_executor_job(load_texts, language, self.hass.config.currency, self.distance_unit)
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

        await self._async_apply_restore()
        self.set_charging_rule(f"📟{self.t('ui.init.script_starting')}")

    async def _async_apply_restore(self) -> None:
        """Databases and settings of a backup imported or chosen in the config flow (management/backup.py)."""
        name = pending_restores(self.hass).pop(self.entry.entry_id, None)
        if name is None and (name := self.entry.data.get("restore_backup")):
            # only once, the restored databases are saved from now on
            data = {key: value for key, value in self.entry.data.items() if key != "restore_backup"}
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        if not name:
            return
        try:
            backup = await async_load_backup(self.hass, name)
        except HomeAssistantError as err:
            _LOGGER.error("Could not restore the Cable Juice Planner backup: %s", err)
            return
        restored = await async_restore_databases(self.storage, backup)
        self.restore_settings = dict(backup.get("settings") or {})
        _LOGGER.info("Restored backup %s: databases %s, %s settings", name, ", ".join(restored) or "none", len(self.restore_settings))
        persistent_notification.async_create(
            self.hass,
            self.t("ui.backup.imported", file=name, databases=", ".join(restored) or "-"),
            title=f"📟{self.title}",
            notification_id=f"{self.object_prefix}_backup",
        )

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
        if self._charging_loss_timeout:
            self._charging_loss_timeout()
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
        package_logger = logging.getLogger(__package__.rpartition(".")[0])
        package_logger.addHandler(self._log_handler)
        if package_logger.getEffectiveLevel() > logging.DEBUG:
            package_logger.setLevel(logging.DEBUG)

    async def _async_disable_file_log(self) -> None:
        if self._log_handler:
            logging.getLogger(__package__.rpartition(".")[0]).removeHandler(self._log_handler)
            self._log_handler = None
        if self._log_listener:
            listener = self._log_listener
            self._log_listener = None
            await self.hass.async_add_executor_job(listener.stop)

    async def _async_startup(self) -> None:
        # All config entries receive homeassistant_started together. Yield once,
        # then wait for the shared site entry before the first car calculation.
        await asyncio.sleep(0)
        if (site := self.site) is not None:
            await site.async_wait_started()
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
            if (migrated := self.entry.data.get(MIGRATED_LAST_FULL_CHARGE)) and (moment := dt_util.parse_datetime(migrated)):
                # the value of the former datetime entity (version 2.3)
                runtime = databases.get("runtime") or {}
                runtime.setdefault("sensors", {}).setdefault("last_full_charge", dt_util.as_local(moment).replace(tzinfo=None))
                databases["runtime"] = runtime

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
            # the first time the whole history, then every hour the last day
            self._background(self._async_charging_loss_refresh(backfill=not self.engine.charging_loss_backfilled()), "charging_loss_history")
            self._background(self._async_economy_refresh(), "economy")
            self._background(self._async_analysis_refresh(), "analysis")
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
        legacy_full_charge = self.hass.states.get(f"input_datetime.{legacy}_last_full_charge")
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
        if legacy_full_charge is not None and (moment := dt_util.parse_datetime(legacy_full_charge.state)):
            runtime["sensors"]["last_full_charge"] = moment.replace(tzinfo=None)
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
