"""Cable Juice Planner calculation engine.

The engine owns all runtime state (databases, charging plan, charging session, price
cache, ...) and runs in the worker thread of the config entry. Each call goes through
:meth:`CJPEngine.run_job` with a :class:`Snapshot` of Home Assistant data collected on
the event loop. Nothing in the engine touches Home Assistant: outputs (sensor states
and attributes, setting changes, events, notifications, database saves) are returned
in a :class:`JobResult` and published by the coordinator.

The integration does not control the charger or the car. Where the PyScript version
sent commands, the engine sets the action entities instead (``charger_action``, the
phase amps, ``charging_limit`` and the ``ev_request`` event), which the user's own
automations act on.

The algorithm is a port of ``ev.py`` split in mixins:

* :mod:`engine_planner` - the charging plan and the overview markdown
* :mod:`engine_control`  - what to do right now, triggers, preheating, charging loss
* :mod:`engine_history`  - charging history, battery expenses, drive efficiency
* :mod:`engine_energy`   - power values, solar prediction, prices and the hourly databases
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pprint import pformat
from typing import Any, Callable

import astral.sun

from .codec import encode
from .const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    CHARGING_TYPES,
    EMOJI_DESCRIPTION_GROUPS,
    ENTITY_UNAVAILABLE_STATES,
    EV_PLUGGED_STATES,
    EV_UNPLUGGED_STATES,
    NUMBER_SETTINGS,
    POWERWALL_CHARGING_TRIGGER,
    POWERWALL_DISCHARGING_TRIGGER,
    PRICE_ADDER_DAY_BETWEEN_DIVIDER,
    SOLAR_PRODUCTION_TRIGGER,
    WEEKDAYS,
    is_charger_configured,
    is_ev_configured,
    is_powerwall_configured,
    is_solar_configured,
)
from .engine_analysis import AnalysisMixin
from .engine_control import ControlMixin
from .engine_distance import DistanceMixin
from .engine_economy import EconomyMixin
from .engine_energy import EnergyMixin
from .engine_history import HistoryMixin
from .engine_loss import LossMixin
from .engine_planner import PlannerMixin
from .engine_site import SiteMixin
from .formatting import format_charging_rule, get_list_values, translate
from .helpers import (
    average,
    calculate_ema,
    calculate_trend,
    day_of_week_text,
    days_between,
    end_of_day,
    float_or_none,
    limit_dict_size,
    minutes_between,
    parse_datetime,
    reset_datetime,
    reset_time_to_hour,
    reverse_list,
    round_up,
    start_of_day,
)
from .history import History
from .prices import create_price_provider

_LOGGER = logging.getLogger(__name__)

# How long the same notification is suppressed (my_notify always=False)
NOTIFY_SUPPRESS = datetime.timedelta(minutes=60)


@dataclass
class Snapshot:
    """Plain data collected on the event loop for one worker job."""

    now: datetime.datetime
    tz: Any
    settings: dict[str, Any] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, dict] = field(default_factory=dict)
    platforms: dict[str, str | None] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    buffers: dict[str, list] = field(default_factory=dict)
    statistics: dict[str, list] = field(default_factory=dict)
    price_data: dict | None = None
    forecast_dict: dict | None = None
    solcast: dict[str, list] = field(default_factory=dict)
    astral: Any = None  # astral.Observer
    site: dict | None = None  # load balancing, see engine_site


@dataclass
class JobResult:
    """Everything a worker job wants the event loop to publish."""

    states: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    setting_limits: dict[str, dict[str, float]] = field(default_factory=dict)
    events: list[tuple[str, dict]] = field(default_factory=list)
    notifications: list[tuple[str, str, str]] = field(default_factory=list)
    mobile_notifications: list[dict] = field(default_factory=list)
    db_saves: dict[str, Any] = field(default_factory=dict)
    config_updates: dict[str, Any] = field(default_factory=dict)
    value: Any = None
    error: BaseException | None = None
    request_reload: bool = False
    site: dict | None = None  # reservation / request / urgent for the site
    worker_thread: str = ""
    duration: float = 0.0


class CJPEngine(PlannerMixin, ControlMixin, HistoryMixin, DistanceMixin, LossMixin, EconomyMixin, AnalysisMixin, EnergyMixin, SiteMixin):
    """Holds the Cable Juice Planner state and implements the algorithm."""

    def __init__(self, config: dict, prefix: str, texts: dict[str, str], title: str | None = None) -> None:
        self.config = config
        self.name = prefix  # start of the entity ids (cjp or cjp_<prefix>)
        self.texts = texts  # runtime texts, see formatting.translate
        self.testing = bool(config.get("testing_mode"))
        self.title = title or f"Cable Juice Planner ({prefix})"

        self.charger_configured = is_charger_configured(config)
        self.solar_configured = is_solar_configured(config)
        self.powerwall_configured = is_powerwall_configured(config)
        self.ev_configured = is_ev_configured(config)

        charger = config["charger"]
        solar = config["solar"]
        # init() limited the solar amps to the charger maximum
        solar["charging_single_phase_max_amp"] = min(solar["charging_single_phase_max_amp"], charger["charging_max_amp"])
        solar["charging_single_phase_min_amp"] = min(solar["charging_single_phase_min_amp"], charger["charging_max_amp"])
        solar["charging_three_phase_min_amp"] = min(solar["charging_three_phase_min_amp"], charger["charging_max_amp"])
        solar_min_amp = solar["charging_single_phase_min_amp"] if float(charger["charging_phases"]) > 1.0 else solar["charging_three_phase_min_amp"] * 3.0
        self.solar_charging_trigger_on = abs(charger["power_voltage"] * solar_min_amp)
        self.max_watt_charging = (charger["power_voltage"] * charger["charging_phases"]) * charger["charging_max_amp"]
        self.max_kwh_charging = self.max_watt_charging / 1000

        try:
            self.price_provider = create_price_provider(config)
        except ValueError as err:
            _LOGGER.error("No price provider configured: %s", err)
            self.price_provider = None

        # databases (persisted)
        self.charging_history_db: dict = {}
        self.power_values_db: dict = {}
        self.solar_available_db: dict = {}
        self.kwh_avg_prices_db: dict = {}
        self.drive_efficiency_db: list = []
        self.km_kwh_efficiency_db: list = []

        # own sensors (state and full attribute dict)
        self.sensor_states: dict[str, Any] = {}
        self.sensor_attrs: dict[str, dict[str, Any]] = {}

        # runtime state (the globals of ev.py)
        self.preheating = False
        self.last_hash_results: dict = {}
        self.grid_price_cache: dict = {"using_offline_prices": False}
        self.sell_price_cache: dict = {"using_offline_prices": False}
        self.charging_is_beginning = False
        self.restarting_charger = False
        self.restarting_charger_count = 0
        self.current_charging_amps = [0, 0, 0]
        self.current_circuit_amps = [0, 0, 0]
        # amps asked for before the site limit (site_limit re-applies them)
        self.requested_charger_amps = [0, 0, 0]
        self.requested_circuit_amps = [0, 0, 0]
        self.site_charging_text = ""
        self.last_charging_rule_text = ""
        self.rule_set_this_run = False
        self.charging_loss_car_begin_kwh = 0.0
        self.charging_loss_car_begin_battery_level = 0.0
        self.charging_loss_charger_begin_kwh = 0.0
        self.charging_loss_charging_completed = False
        self.daily_odometer: dict[str, float] = {}  # ISO date -> odometer at the end of the day
        self.charging_loss_db: dict = {"segments": [], "backfilled": False}
        self.economy_db: dict = {"windows": []}
        self.analysis_db: dict = {"drives": [], "departures": [], "planned_days": {}, "price_hours": {}, "price_days": {}}
        self.charging_no_rule_count = 0
        self.error_count = 0
        self.forecast_type = "ema"
        self.public_charging_sessions: list = []
        self.powerwall_charging_text = ""
        self.last_wake_up_datetime = reset_datetime()
        self.last_trip_change_datetime = datetime.datetime.now().replace(microsecond=0)
        self.charging_history_ending_byte_size: int | None = None
        self.overview_history: dict = {}
        self.battery_level_expenses: dict = {}
        self.charging_plan: dict = {}
        self.charge_hours: dict = {}
        self.last_drive_efficiency_data: dict = {}
        self.local_energy_prices: dict = {"solar_kwh_price": {}, "powerwall_kwh_price": {}}
        self.local_energy_prediction_db: dict = {"solar_prediction": {}, "solar_prediction_timestamps": {}}
        self.max_local_energy_available_history: dict = {}
        self.charging_history_last_result: dict = {}
        self.integration_offline_since: dict = {}
        self.notify_history: dict = {}
        self.charger_action = "stop"
        self.charger_enabled = True
        self.charger_mode = "normal"
        self.reset_current_charging_session()

        # per job
        self._snap: Snapshot | None = None
        self._out: JobResult | None = None
        self._history: History | None = None
        self._dirty: set[str] = set()
        self._job_cache: dict[str, Any] = {}
        self._changed_sensors: set[str] = set()

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #

    def run_job(self, snapshot: Snapshot, func: Callable, *args, **kwargs) -> JobResult:
        """Run func with snapshot as input. Called in the (single) worker thread."""
        started = time.perf_counter()
        self._snap = snapshot
        self._out = result = JobResult(worker_thread=threading.current_thread().name)
        self._history = History(snapshot.buffers, snapshot.statistics, snapshot.units, snapshot.tz, snapshot.now)
        self._dirty = set()
        self._job_cache = {}
        self._changed_sensors = set()
        if self.price_provider is not None:
            self.price_provider.set_now(snapshot.now)
            if snapshot.price_data is not None:
                self.price_provider.set_snapshot(snapshot.price_data)
        try:
            result.value = func(*args, **kwargs)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Cable Juice Planner job %s failed", getattr(func, "__name__", func))
            result.error = err
        finally:
            for key in self._changed_sensors:
                result.states[key] = self.sensor_states.get(key)
                result.attributes[key] = dict(self.sensor_attrs.get(key, {}))
            databases = self._databases()
            for name in self._dirty:
                result.db_saves[name] = encode(databases[name])
            self._snap = self._out = self._history = None
        result.duration = time.perf_counter() - started
        return result

    def _databases(self) -> dict[str, Any]:
        return {
            "charging_history": self.charging_history_db,
            "power_values": {**self.power_values_db, "version": 1.0} if self.power_values_db else {},
            "solar_available": {**self.solar_available_db, "version": 2.0} if self.solar_available_db else {},
            "kwh_avg_prices": {**self.kwh_avg_prices_db, "version": 2.0} if self.kwh_avg_prices_db else {},
            "drive_efficiency": self.drive_efficiency_db,
            "km_kwh_efficiency": self.km_kwh_efficiency_db,
            "runtime": self._runtime_state(),
            "charging_loss": self.charging_loss_db,
            "economy": self.economy_db,
            "analysis": self.analysis_db,
        }

    def mark_dirty(self, name: str) -> None:
        self._dirty.add(name)

    def _runtime_state(self) -> dict:
        """State the PyScript version kept in helpers and sensor attributes."""
        keep = ("drive_efficiency_last_battery_level", "drive_efficiency_last_odometer", "co2_emitted", "kwh_charged_by_solar", "last_full_charge")
        return {
            "sensors": {key: self.sensor_states.get(key) for key in keep if self.sensor_states.get(key) is not None},
            "last_battery_level_attributes": dict(self.sensor_attrs.get("drive_efficiency_last_battery_level", {})),
            "public_charging_session": self.public_charging_sessions,
            "public_charging_session_done": self.sensor_states.get("public_charging_session_done"),
            "charging_loss": self.config["charger"]["charging_loss"],
            "daily_odometer": dict(self.daily_odometer),
            "charging_loss_measurement": {
                "car_begin_battery_level": self.charging_loss_car_begin_battery_level,
                "car_begin_kwh": self.charging_loss_car_begin_kwh,
                "charger_begin_kwh": self.charging_loss_charger_begin_kwh,
                "charging_completed": self.charging_loss_charging_completed,
            },
        }

    # ------------------------------------------------------------------ #
    # Snapshot access
    # ------------------------------------------------------------------ #

    def t(self, key: str, default: str | None = None, **fmt) -> str:
        return translate(self.texts, key, default, **fmt)

    def now(self) -> datetime.datetime:
        return self._snap.now if self._snap else datetime.datetime.now().replace(microsecond=0)

    def current_hour(self) -> datetime.datetime:
        return reset_time_to_hour(self.now())

    @property
    def history(self) -> History:
        return self._history

    def entity_exists(self, entity_id) -> bool:
        return bool(entity_id) and self._snap is not None and entity_id in self._snap.states

    def get_state(self, entity_id=None, try_history=True, float_type=False, error_state="unknown"):
        """hass_manager.get_state for a configured Home Assistant entity."""
        if entity_id == "":
            return error_state
        output = None
        try:
            if entity_id is None or "." not in entity_id:
                raise ValueError(f"Invalid entity_id: {entity_id}")
            if not self.entity_exists(entity_id):
                raise KeyError(f"Entity not found in Home Assistant: {entity_id}")
            output = self._snap.states[entity_id]
            if try_history and output in ENTITY_UNAVAILABLE_STATES:
                output = self.history.get_last_value(entity_id, float_type=float_type, error_state=error_state)
            if float_type:
                output = float(output) if output not in ENTITY_UNAVAILABLE_STATES else error_state
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Can't get state for %s, output is %s: %s", entity_id, output, err)
            return error_state
        return output

    def get_attr(self, entity_id=None, attr=None, error_state="unknown"):
        try:
            if not self.entity_exists(entity_id):
                raise KeyError(f"Entity not found: {entity_id}")
            attributes = self._snap.attributes.get(entity_id) or {}
            return dict(attributes) if attr is None else attributes[attr]
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Can't get attribute for %s['%s']: %s", entity_id, attr, err)
            return error_state

    def own_entity_id(self, platform: str, key: str) -> str:
        return f"{platform}.{self.name}_{key}"

    def get_integration(self, entity_id):
        return self._snap.platforms.get(entity_id) if self._snap and entity_id else None

    # settings (the former input_* helpers) ----------------------------- #

    def has_setting(self, key: str) -> bool:
        return self._snap is not None and key in self._snap.settings

    def setting(self, key: str, error_state=None):
        """Raw setting value: bool, float, str, datetime.time or datetime (None = not set)."""
        if not self.has_setting(key):
            return error_state
        return self._snap.settings[key]

    def switch_state(self, key: str, error_state="unknown") -> str:
        """'on'/'off' like get_state(input_boolean....)."""
        if not self.has_setting(key):
            return error_state
        return "on" if self._snap.settings[key] else "off"

    def number_state(self, key: str, error_state=None):
        value = float_or_none(self.setting(key))
        return error_state if value is None else value

    def datetime_state(self, key: str, error_state=None):
        """input_datetime value; None (not set) is the old 1900-01-01 00:00."""
        if not self.has_setting(key):
            return error_state
        value = self._snap.settings[key]
        if value is None:
            return reset_datetime()
        if isinstance(value, str):
            return parse_datetime(value)
        return value

    def set_setting(self, key: str, value) -> None:
        """Change a setting entity (published and fired as a state change by the coordinator)."""
        if self._snap is None:
            return
        if isinstance(value, datetime.datetime) and value == reset_datetime():
            value = None
        self._snap.settings[key] = value
        self._out.settings[key] = value

    def set_setting_limit(self, key: str, maximum: float) -> None:
        if self._out is not None:
            self._out.setting_limits[key] = {"max": maximum}

    # own sensors ------------------------------------------------------- #

    def set_state(self, key: str, value) -> None:
        self.sensor_states[key] = value
        self._changed_sensors.add(key)

    def own_state(self, key: str, float_type=False, error_state="unknown"):
        value = self.sensor_states.get(key)
        if value in ENTITY_UNAVAILABLE_STATES:
            return error_state
        if float_type:
            try:
                return float(value)
            except (TypeError, ValueError):
                return error_state
        return value

    def set_attr(self, key: str, attr: str, value) -> None:
        self.sensor_attrs.setdefault(key, {})[attr] = value
        self._changed_sensors.add(key)

    def del_attr(self, key: str, attr: str) -> None:
        if attr in self.sensor_attrs.get(key, {}):
            del self.sensor_attrs[key][attr]
            self._changed_sensors.add(key)

    def own_attr(self, key: str, attr: str | None = None, error_state="unknown"):
        attributes = self.sensor_attrs.get(key)
        if attributes is None:
            return error_state
        if attr is None:
            return dict(attributes)
        return attributes.get(attr, error_state)

    # notifications / events -------------------------------------------- #

    def persistent_notification(self, message: str, title: str = "", notification_id: str | None = None) -> None:
        if self._out is not None:
            self._out.notifications.append((message, title, notification_id or f"{self.name}_notification"))

    def my_notify(self, message="", title="", data=None, admin_only=False, always=False, persistent_notification=False, persistent_notification_id=None) -> None:
        """mynotify.my_notify: notify the configured notify services (deduplicated for an hour)."""
        now = self.now()
        self.notify_history = {until: text for until, text in self.notify_history.items() if until > now}
        key = f"{title} {message}"
        if key in self.notify_history.values() and not always:
            _LOGGER.warning("Notify message not send, because its already been send in the last hour: '%s'", key)
            return
        if self._out is not None:
            self._out.mobile_notifications.append({"message": message, "title": title, "data": dict(data or {})})
        if persistent_notification:
            self.persistent_notification(message, title, persistent_notification_id)
        self.notify_history[now + NOTIFY_SUPPRESS] = key

    def fire_ev_request(self, request: str, **data) -> None:
        if self._out is not None:
            _LOGGER.info("EV request: %s %s", request, data or "")
            self._out.events.append((request, data))

    # ------------------------------------------------------------------ #
    # Sun
    # ------------------------------------------------------------------ #

    def get_sun_events(self, dt: datetime.datetime | None = None) -> dict:
        """hass_manager.get_sun_events: naive local dawn/sunrise/noon/sunset/midnight."""
        dt = dt or self.now()
        cache = self._job_cache.setdefault("sun_events", {})
        if dt.date() in cache:
            return cache[dt.date()]
        output = {"dawn": None, "sunrise": None, "noon": None, "sunset": None, "midnight": None}
        try:
            tz = self._snap.tz
            output = {
                name: getattr(astral.sun, name)(self._snap.astral, dt).astimezone(tz).replace(tzinfo=None)
                for name in output
            }
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Can't get sun events: %s", err)
        cache[dt.date()] = output
        return output

    def is_day(self, timestamp: datetime.datetime | None = None) -> bool:
        """mytime.is_day: sunrise <= timestamp <= sunset.

        Like the PyScript version the sun events are UTC with the timezone removed and
        compared with the local timestamp.
        """
        if not timestamp:
            timestamp = self.now()
        cache = self._job_cache.setdefault("utc_sun", {})
        date = timestamp.date()
        if date not in cache:
            events = []
            for name in ("sunrise", "sunset"):
                try:
                    value = getattr(astral.sun, name)(self._snap.astral, date)
                except ValueError:
                    value = None
                events.append(value.replace(tzinfo=None) if value else None)
            cache[date] = events
        sunrise, sunset = cache[date]
        if not sunrise or not sunset:
            raise ValueError(f"Could find sunrise or sunset: timestamp: {timestamp} sunrise:{sunrise}, sunset:{sunset}")
        return sunrise <= timestamp.replace(tzinfo=None) <= sunset

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #

    def is_charger_configured(self) -> bool:
        return self.charger_configured

    def is_solar_configured(self) -> bool:
        return self.solar_configured

    def is_powerwall_configured(self) -> bool:
        return self.powerwall_configured

    def is_ev_configured(self) -> bool:
        return self.ev_configured

    @staticmethod
    def is_entity_configured(entity) -> bool:
        if isinstance(entity, str):
            return entity.strip() != ""
        if isinstance(entity, list):
            return len(entity) > 0
        _LOGGER.warning("Invalid entity type: entity %s %s", entity, type(entity))
        return False

    def is_entity_available(self, entity):
        """Available check that warns once an integration has been offline for 30 minutes."""
        if not self.is_entity_configured(entity):
            return None
        integration = self.get_integration(entity)
        entity_state = str(self.get_state(entity, error_state="unknown"))
        if entity_state not in ENTITY_UNAVAILABLE_STATES:
            if integration is not None and integration in self.integration_offline_since:
                del self.integration_offline_since[integration]
                _LOGGER.warning("Removing %s from offline timestamp", integration)
            return True
        _LOGGER.warning("Entity %s not available: Entity state is %s", entity, entity_state)
        if integration is not None:
            if integration not in self.integration_offline_since:
                self.integration_offline_since[integration] = self.now()
                _LOGGER.warning("Adding %s to offline timestamp", integration)
            if minutes_between(self.integration_offline_since[integration], self.now()) > 30:
                fmt = {"entity": entity, "timestamp": self.integration_offline_since[integration]}
                self.persistent_notification(
                    f"⛔{self.t('ui.entity_unavailable.message', **fmt)}\n\n{self.t('ui.entity_unavailable.restart_integration')}",
                    title=f"{self.title} {self.t('ui.entity_unavailable.title')}",
                    notification_id=f"{self.name}_is_entity_available_{entity}",
                )
        return False

    @staticmethod
    def is_powerwall_charging(charge_watt=0.0) -> bool:
        if not isinstance(charge_watt, (int, float)):
            _LOGGER.error("Invalid discharge_watt value: %s", charge_watt)
            return False
        return charge_watt > POWERWALL_CHARGING_TRIGGER

    @staticmethod
    def is_powerwall_discharging(discharge_watt=0.0) -> bool:
        if not isinstance(discharge_watt, (int, float)):
            _LOGGER.error("Invalid discharge_watt value: %s", discharge_watt)
            return False
        return discharge_watt > POWERWALL_DISCHARGING_TRIGGER

    @staticmethod
    def is_solar_production_available(watt=0.0) -> bool:
        if not isinstance(watt, (int, float)):
            _LOGGER.error("Invalid watt value: %s", watt)
            return False
        return watt > SOLAR_PRODUCTION_TRIGGER

    def configured_entity_ids(self) -> list[str]:
        entity_ids = []
        for value in self.config.values():
            if not isinstance(value, dict) or "entity_ids" not in value:
                continue
            for entity in value["entity_ids"].values():
                if isinstance(entity, list):
                    entity_ids.extend(entity)
                else:
                    entity_ids.append(entity)
        return entity_ids

    def get_all_unavailable_entities(self) -> list[str]:
        return [e for e in self.configured_entity_ids() if self.is_entity_configured(e) and not self.is_entity_available(e)]

    # ------------------------------------------------------------------ #
    # Charging rule
    # ------------------------------------------------------------------ #

    def set_charging_rule(self, text: str = "", fresh: bool = True) -> None:
        if self.restarting_charger_count >= 3:
            return
        self.rule_set_this_run = self.rule_set_this_run or fresh
        self.last_charging_rule_text = text
        extra = f"\n🚧{self.powerwall_charging_text}" if self.powerwall_charging_text else ""
        if self.site_charging_text:
            extra += f"\n🚧{self.site_charging_text}"
        self.set_state("current_charging_rule", format_charging_rule(text, self.testing, extra))

    # ------------------------------------------------------------------ #
    # Settings (ev.py get_* functions)
    # ------------------------------------------------------------------ #

    def _number_or_config(self, key: str, path: tuple[str, str], what: str) -> float:
        value = self.number_state(key)
        if value is None:
            fallback = self.config[path[0]][path[1]]
            _LOGGER.warning("Cant get %s, using config data %s", what, fallback)
            return fallback
        return float(value)

    def get_entity_daily_distance(self, day_text=None, date=None, ignore_realistic_estimated_range=False):
        try:
            if day_text is None and date is None:
                distance = float(self.number_state("typical_daily_distance"))
            else:
                day_text = day_text if day_text in WEEKDAYS else day_of_week_text(self.now() if date is None else date)
                workday = self.switch_state(f"workday_{day_text}", error_state="off")
                distance = float(self.number_state(f"workday_distance_needed_{day_text}", error_state=0.0))
                if distance == 0.0 or workday == "off":
                    distance = float(self.number_state("typical_daily_distance"))
            if self.is_ev_configured() and not ignore_realistic_estimated_range:
                estimated_range_attr = self.own_attr("estimated_range", error_state={})
                if "total" in estimated_range_attr and float(estimated_range_attr["total"]) > 0.0:
                    estimated_range_per_percent = float(estimated_range_attr["total"]) / 100
                    daily_available_battery_level = self.get_max_recommended_charge_limit_battery_level() - self.get_min_daily_battery_level()
                    distance = min(daily_available_battery_level * estimated_range_per_percent, distance)
            return distance
        except Exception as err:  # noqa: BLE001
            fallback = self.config["ev_car"]["typical_daily_distance_non_working_day"]
            _LOGGER.warning("Cant get daily distance, using config data %s: %s", fallback, err)
            return fallback

    def get_min_daily_battery_level(self) -> float:
        return self._number_or_config("min_daily_battery_level", ("ev_car", "min_daily_battery_level"), "daily battery level")

    def get_min_trip_battery_level(self) -> float:
        return self._number_or_config("min_trip_battery_level", ("ev_car", "min_trip_battery_level"), "min trip battery level")

    def get_min_charge_limit_battery_level(self) -> float:
        return self._number_or_config("min_charge_limit_battery_level", ("ev_car", "min_charge_limit_battery_level"), "min charge limit battery level")

    def get_max_recommended_charge_limit_battery_level(self) -> float:
        return self._number_or_config(
            "max_recommended_charge_limit_battery_level", ("ev_car", "max_recommended_charge_limit_battery_level"), "max recommended charge limit battery level"
        )

    def get_very_cheap_grid_charging_max_battery_level(self) -> float:
        return self._number_or_config(
            "very_cheap_grid_charging_max_battery_level", ("ev_car", "very_cheap_grid_charging_max_battery_level"), "very cheap grid charging max battery level"
        )

    def get_ultra_cheap_grid_charging_max_battery_level(self) -> float:
        return self._number_or_config(
            "ultra_cheap_grid_charging_max_battery_level", ("ev_car", "ultra_cheap_grid_charging_max_battery_level"), "ultra cheap grid charging max battery level"
        )

    def get_powerwall_discharge_above_needed(self):
        if not self.is_powerwall_configured():
            return False
        return self.switch_state("powerwall_discharge_above_needed", error_state="off") == "on"

    def get_powerwall_battery_level(self) -> float:
        if not self.is_powerwall_configured():
            return 100.0
        entity_id = self.config["home"]["entity_ids"]["powerwall_battery_level_entity_id"]
        try:
            return float(self.get_state(entity_id, float_type=True, error_state=None))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall battery level from %s, using default value 100.0: %s", entity_id, err)
            return 100.0

    def get_ev_charge_after_powerwall_battery_level(self) -> float:
        if not self.is_powerwall_configured():
            return 100.0
        value = self.number_state("ev_charge_after_powerwall_battery_level")
        if value is None:
            try:
                return float(self.config["solar"]["ev_charge_after_powerwall_battery_level"])
            except Exception:  # noqa: BLE001
                return 100.0
        return float(value)

    def get_completed_battery_level(self) -> float:
        try:
            if not self.is_ev_configured():
                return float(self.number_state("completed_battery_level"))
            return 100.0
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Using default charge completed battery level 100.0: %s", err)
            return 100.0

    def get_estimated_total_range(self) -> float:
        estimated_total_range = 0.0
        try:
            if self.is_ev_configured():
                calc = self.get_forecast_value(self.km_kwh_efficiency_db)
                if calc == 0.0:
                    raise ValueError(f"calc is invalid: {calc}")
                estimated_total_range = self.km_kwh_to_km_percentage(calc) * 100.0
            else:
                try:
                    estimated_total_range = float(self.number_state("estimated_total_range"))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("estimated_total_range is not set to a total range: %s", err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Using default estimated total range 0.0: %s", err)
        return estimated_total_range

    def get_trip_date_time(self) -> datetime.datetime:
        return self.datetime_state("trip_date_time", error_state=reset_datetime())

    def get_trip_homecoming_date_time(self) -> datetime.datetime:
        return self.datetime_state("trip_homecoming_date_time", error_state=reset_datetime())

    def get_trip_range(self) -> float:
        value = self.number_state("trip_range_needed")
        if value is None:
            _LOGGER.error("trip_range_needed is not set to a number")
            self.set_setting("trip_range_needed", 0.0)
            return 0.0
        return float(value)

    def get_trip_target_level(self) -> float:
        value = self.number_state("trip_charge_procent")
        if value is None:
            _LOGGER.error("trip_charge_procent is not set to a number")
            self.set_setting("trip_charge_procent", 0.0)
            return 0.0
        return float(value)

    def get_public_charging_session_done(self) -> bool:
        if not self.is_ev_configured():
            return False
        return self.sensor_states.get("public_charging_session_done") == "on"

    def get_forecast_type(self) -> str:
        forecast_type = str(self.setting("forecast_type") or "ema").split(" ")[0].lower()
        self.forecast_type = forecast_type if forecast_type in ("average", "ema", "trend") else "ema"
        return self.forecast_type

    def get_forecast_value(self, data=None, forecast_type: str | None = None) -> float:
        """The forecast of the selected type, or of forecast_type (the forecast accuracy analysis)."""
        forecast_type = forecast_type or self.forecast_type
        if not data or not isinstance(data, list) or len(data) == 0:
            return 0.0
        if forecast_type == "ema":
            return round(calculate_ema(reverse_list(get_list_values(data))), 2)
        if forecast_type == "average":
            return round(average(get_list_values(data)), 2)
        if forecast_type == "trend":
            return round(calculate_trend(reverse_list(get_list_values(data))), 2)
        return 0.0

    def is_trip_planned(self) -> bool:
        for key in ("trip_charge_procent", "trip_range_needed", "trip_date_time", "trip_homecoming_date_time"):
            if not self.has_setting(key):
                return False
        if self.get_trip_range() == 0.0 and self.get_trip_target_level() == 0.0:
            return False
        if self.get_trip_date_time() == reset_datetime() or self.get_trip_homecoming_date_time() == reset_datetime():
            return False
        return True

    def fill_up_charging_enabled(self):
        return True if self.switch_state("fill_up") == "on" else None

    def cheap_grid_charging_enabled(self):
        return True if self.switch_state("cheap_grid_charging") == "on" else None

    def workplan_charging_enabled(self):
        return True if self.switch_state("workplan_charging") == "on" else None

    def solar_charging_enabled(self):
        return True if self.is_solar_configured() and self.switch_state("solar_charging") == "on" else None

    def manual_charging_enabled(self):
        return True if self.switch_state("allow_manual_charging_now") == "on" else None

    def manual_charging_solar_enabled(self):
        return True if self.switch_state("allow_manual_charging_solar") == "on" else None

    def is_calculating_charging_loss(self) -> bool:
        return self.switch_state("calculate_charging_loss", error_state="off") == "on"

    def no_charging_modes_active(self) -> bool:
        return not (
            self.is_trip_planned()
            or self.solar_charging_enabled()
            or self.workplan_charging_enabled()
            or self.fill_up_charging_enabled()
            or self.cheap_grid_charging_enabled()
            or self.is_calculating_charging_loss()
            or self.manual_charging_enabled()
            or self.manual_charging_solar_enabled()
        )

    # ------------------------------------------------------------------ #
    # Battery math
    # ------------------------------------------------------------------ #

    def charging_loss_factor(self, kw: float | None = None) -> float:
        """kWh from the charger = kWh in the car * (1 + this).

        The configured charging loss, or with use_measured_charging_loss the measured one
        (engine_loss.py) once there are measurements: at kw when given and the model knows
        more than one power, else at the max charging power. Calculated once per job and kw.
        """
        if self._snap is not None and self.switch_state("use_measured_charging_loss", error_state="off") == "on":
            cache = self._job_cache.setdefault("charging_loss_factor", {})
            key = None if kw is None else round(kw, 2)
            if key not in cache:
                cache[key] = self.measured_charging_loss_factor(kw)
            if cache[key] is not None:
                return cache[key]
        return abs(self.config["charger"]["charging_loss"])

    def kwh_to_percentage(self, kwh, include_charging_loss=False, kw: float | None = None):
        effective_kwh = kwh
        if include_charging_loss:
            effective_kwh = kwh / (1 + self.charging_loss_factor(kw))
        return effective_kwh / self.config["ev_car"]["battery_size"] * 100

    def percentage_to_kwh(self, percentage, include_charging_loss=False, kw: float | None = None):
        kwh = percentage * self.config["ev_car"]["battery_size"] / 100
        if include_charging_loss:
            kwh = kwh * (1 + self.charging_loss_factor(kw))
        return kwh

    def solar_charging_kw(self) -> float:
        """A typical solar charging power: the middle of the 1 phase solar charging range."""
        solar = self.config["solar"]
        amps = (float(solar["charging_single_phase_min_amp"]) + float(solar["charging_single_phase_max_amp"])) / 2
        return amps * float(self.config["charger"]["power_voltage"]) / 1000

    def km_kwh_to_km_percentage(self, kwh):
        return self.percentage_to_kwh(kwh, include_charging_loss=False)

    def km_percentage_to_km_kwh(self, percentage):
        return self.kwh_to_percentage(percentage, include_charging_loss=False)

    def distance_per_percentage(self) -> float:
        output = 3.0
        if not self.is_ev_configured():
            try:
                output = round(self.get_estimated_total_range() / 100, 2)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(err)
        else:
            try:
                if self.km_kwh_efficiency_db == {}:
                    raise ValueError("No data yet")
                calc = self.get_forecast_value(self.km_kwh_efficiency_db)
                if calc == 0.0:
                    raise ValueError(f"calc is invalid: {calc}")
                output = self.km_kwh_to_km_percentage(calc)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Using default value 3.0: %s", err)
        return output

    def calc_distance_to_battery_level(self, distance):
        return distance / self.distance_per_percentage()

    def calc_battery_level_to_distance(self, battery_level):
        return battery_level * self.distance_per_percentage()

    def battery_level(self) -> float:
        try:
            if not self.is_ev_configured():
                return float(self.number_state("battery_level", error_state=0.0))
            entity_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
            if not self.is_entity_available(entity_id):
                return 0.0
            return float(self.get_state(entity_id, float_type=True, error_state=0.0))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(err)
            return 0.0

    def battery_range(self) -> float:
        distance = 0.0
        if not self.is_ev_configured():
            distance = self.distance_per_percentage() * self.battery_level()
        else:
            entity_id = self.config["ev_car"]["entity_ids"]["estimated_battery_range_entity_id"]
            try:
                if not self.is_entity_available(entity_id):
                    return distance
                distance = float(self.get_state(entity_id, float_type=True, error_state=0.0))
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(err)
        return distance

    def range_to_battery_level(self, extraRange=None, batteryBuffer=None, date=None):  # noqa: N803 - names from ev.py
        min_range_in_battery_level = self.get_min_charge_limit_battery_level()
        extraRange = extraRange if extraRange is not None else self.get_entity_daily_distance(day_text=None, date=date)
        batteryBuffer = batteryBuffer if batteryBuffer is not None else self.get_min_daily_battery_level()
        try:
            if not isinstance(extraRange, (int, float)):
                raise ValueError(f"extraRange should be a number, but got: {extraRange}")
            min_range_in_battery_level = round_up(self.calc_distance_to_battery_level(extraRange)) + batteryBuffer
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating battery level - extraRange: %s, batteryBuffer: %s: %s", extraRange, batteryBuffer, err)
        return min(min_range_in_battery_level, 100.0)

    def kwh_needed_for_charging(self, targetLevel=None, battery=None):  # noqa: N803
        targetLevel = targetLevel if targetLevel is not None else self.get_min_daily_battery_level()
        battery = battery if battery is not None else self.battery_level()
        return max(round(self.percentage_to_kwh(targetLevel - battery, include_charging_loss=True), 2), 0.0)

    def verify_charge_limit(self, limit):
        entity_id = self.config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
        if not self.is_entity_configured(entity_id):
            return limit
        try:
            if self.get_integration(entity_id) in ("kia_uvo", "cupra_we_connect"):
                limit = float(round_up(limit / 10) * 10)
            limit = min(max(limit, self.get_min_charge_limit_battery_level()), 100.0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error verifying charge limit: %s", err)
            limit = self.get_max_recommended_charge_limit_battery_level()
        return limit

    def is_ev_home(self) -> bool:
        current_location = "home"
        try:
            location_entity = self.config["ev_car"]["entity_ids"]["location_entity_id"]
            if self.is_entity_configured(location_entity):
                current_location = self.get_state(location_entity, error_state="home")
            else:
                # no location: home while the charger status or the charger cable shows the car connected,
                # or did so within the last 15 minutes (just unplugged)
                now = self.now()
                charger = self.config["charger"]["entity_ids"]
                sources = (
                    # completed counts too: a car that finished charging hours ago is unplugged from completed
                    (charger["status_entity_id"], CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS, CHARGER_NOT_READY_STATUS),
                    (charger["cable_connected_entity_id"], EV_PLUGGED_STATES, EV_UNPLUGGED_STATES),
                )
                configured = False
                for entity_id, connected_states, disconnected_states in sources:
                    if not self.is_entity_configured(entity_id):
                        continue
                    configured = True
                    state = self.get_state(entity_id, error_state="unknown")
                    if state in ENTITY_UNAVAILABLE_STATES:
                        continue
                    if state not in disconnected_states:
                        break
                    values = self.history.get_values(entity_id, now - datetime.timedelta(minutes=15), now, include_timestamps=False, error_state=[connected_states[0]])
                    if set(connected_states) & set(values):
                        break
                else:
                    if configured:
                        current_location = "away"
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error getting current location: %s", err)
            current_location = "home"
        return current_location == "home"

    # ------------------------------------------------------------------ #
    # Prices
    # ------------------------------------------------------------------ #

    def update_grid_prices(self, initial_run=False) -> None:
        try:
            if self.price_provider is None:
                raise ValueError("No supported price provider configured")
            self.price_provider.update_prices()
            self.get_grid_prices(update_prices=True)
            self.get_sell_prices(update_prices=True)
            if not initial_run:
                self.append_kwh_prices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in update_grid_prices: %s %s", err, type(err))
            self.persistent_notification(f"Error in update_grid_prices: {err} {type(err)}", f"{self.title} error", f"{self.name}_update_grid_prices_error")

    def get_hour_prices(self, update_prices=False, sell_prices=False) -> dict:
        """Prices per period. Without online prices, offline prices from the price history."""
        now = self.now()
        database = self.sell_price_cache if sell_prices else self.grid_price_cache
        local_database_type = "history_sell" if sell_prices else "history"
        hour_prices: dict = {}
        # forecasts corrected with their measured bias per day ahead (the analysis); a change recalculates the prices
        corrections = self.price_forecast_corrections()
        try:
            if "prices" in database and database.get("using_offline_prices") is False and update_prices is False and database.get("corrections") == corrections:
                hour_prices = deepcopy(database["prices"])
            else:
                provider = self.price_provider
                if provider is None:
                    raise ValueError("No price provider")
                if sell_prices:
                    hour_prices = provider.get_sell_prices()
                    sources = provider.sell_price_sources
                else:
                    hour_prices = provider.get_buy_prices()
                    sources = provider.buy_price_sources
                if not hour_prices:
                    raise ValueError(f"No {'sell' if sell_prices else 'grid'} prices returned from {type(provider).__name__}")
                hour_prices = deepcopy(hour_prices)
                sources = deepcopy(sources)
                if corrections:
                    current_hour = self.current_hour()
                    hour_prices = {
                        timestamp: round(price + corrections.get(days_between(current_hour, timestamp), 0.0), 2)
                        if str(sources.get(timestamp, "")).endswith(":forecast") else price
                        for timestamp, price in hour_prices.items()
                    }
                # hours of the plan (today + 7 days) the provider has no price for are filled from the price history
                missing_hours = self._database_prices(local_database_type, hour_prices, 8)
                if missing_hours:
                    _LOGGER.debug("No %s price for %s hours, using the price history", "sell" if sell_prices else "grid", len(missing_hours))
                    hour_prices = dict(sorted({**hour_prices, **missing_hours}.items()))
                    sources.update({timestamp: "database" for timestamp in missing_hours})
                database["missing_hours"] = missing_hours
                database["last_update"] = now
                database["prices"] = deepcopy(hour_prices)
                database["sources"] = sources
                database["corrections"] = corrections
                database["using_offline_prices"] = False
        except Exception as err:  # noqa: BLE001
            if "last_update" in database and minutes_between(database["last_update"], now) <= 120:
                hour_prices = deepcopy(database["prices"])
                _LOGGER.warning("Can't get online %s prices, using last successful: %s", "sell" if sell_prices else "grid", err)
            else:
                _LOGGER.warning("Can't get online %s prices, using database: %s", "sell" if sell_prices else "grid", err)
                database["last_update"] = now
                database["using_offline_prices"] = True
                try:
                    if local_database_type not in self.kwh_avg_prices_db and self.kwh_avg_prices_db:
                        raise KeyError(f"Missing {local_database_type} in KWH_AVG_PRICES_DB")
                    missing_hours = self._database_prices(local_database_type, {}, 7, from_start_of_day=True)
                    if not missing_hours:
                        raise KeyError(f"No prices in {local_database_type} of KWH_AVG_PRICES_DB")
                    hour_prices = dict(missing_hours)
                    database["sources"] = {timestamp: "offline" for timestamp in missing_hours}
                    database["missing_hours"] = missing_hours
                    database["prices"] = deepcopy(hour_prices)
                except Exception as ex:  # noqa: BLE001
                    message = f"Can't get offline prices: {ex} {type(ex)}"
                    _LOGGER.error(message)
                    self.persistent_notification(message, f"{self.title} error", f"{self.name}_get_hour_prices_offline_prices_error")
                    raise ValueError(f"Offline prices error: {ex} {type(ex)}") from ex
        return hour_prices

    def _database_prices(self, history_key: str, prices: dict, days: int, from_start_of_day: bool = False) -> dict:
        """Prices from the price history for the hours from now (or midnight) until the end of days that have none in prices.

        The forecast of the same hour and weekday, plus the same small daily adder as the
        provider forecasts. Hours without any history are left out.
        """
        if len(self.kwh_avg_prices_db) == 0:
            self.load_kwh_prices(None)
        history = self.kwh_avg_prices_db.get(history_key, {})
        current_hour = self.current_hour()
        end = start_of_day(self.now()) + datetime.timedelta(days=days)
        priced_hours = {reset_time_to_hour(timestamp) for timestamp in prices}
        missing = {}
        timestamp = start_of_day(self.now()) if from_start_of_day else current_hour
        while timestamp < end:
            values = history.get(timestamp.hour, {}).get(timestamp.weekday(), [])
            if timestamp not in priced_hours and values:
                adder = days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
                missing[timestamp] = round(self.get_forecast_value(values) + adder, 2)
            timestamp += datetime.timedelta(hours=1)
        return missing

    def sell_value(self, price: float) -> float:
        """What a sold kWh is worth at this hourly sell price.

        With a fixed solar sell price (a production agreement that settles the difference to the
        spot price) it is the fixed price minus the hourly price - negative when the hourly price
        is above the fixed price. Without one (-1) it is the hourly price itself.
        """
        fixed = float(self.config["solar"]["production_price"])
        return price if fixed == -1.0 else fixed - price

    def get_grid_prices(self, update_prices=False) -> dict:
        return self.get_hour_prices(update_prices=update_prices)

    def get_sell_prices(self, update_prices=False) -> dict:
        return self.get_hour_prices(update_prices=update_prices, sell_prices=True)

    def get_expensive_hours(self, day=0) -> dict:
        count = 0
        expensive = {}
        day_timestamp = self.now().date() + datetime.timedelta(days=day)
        for timestamp, price in sorted(self.get_grid_prices().items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
            if timestamp.date() == day_timestamp:
                if count < 4:
                    expensive[timestamp] = price
                else:
                    break
                count += 1
        return expensive

    # ------------------------------------------------------------------ #
    # Skip unchanged plans
    # ------------------------------------------------------------------ #

    def get_hash_dict(self) -> dict:
        return {
            "CURRENT_HOUR": self.current_hour(),
            "LAST_DRIVE_EFFICIENCY_DATA": self.last_drive_efficiency_data,
            "LAST_SUCCESSFUL_GRID_PRICES": self.grid_price_cache,
            "BATTERY_LEVEL": self.battery_level(),
            "BATTERY_LEVEL_EXPENSES": self.battery_level_expenses,
            "SETTINGS": (
                self.is_ev_home(),
                self.is_calculating_charging_loss(),
                self.get_min_daily_battery_level(),
                self.get_min_trip_battery_level(),
                self.get_min_charge_limit_battery_level(),
                self.get_max_recommended_charge_limit_battery_level(),
                self.get_very_cheap_grid_charging_max_battery_level(),
                self.get_ultra_cheap_grid_charging_max_battery_level(),
                self.get_powerwall_discharge_above_needed(),
                self.get_powerwall_battery_level(),
                self.get_ev_charge_after_powerwall_battery_level(),
                self.get_completed_battery_level(),
                self.get_estimated_total_range(),
                self.get_trip_date_time(),
                self.get_trip_homecoming_date_time(),
                self.get_trip_range(),
                self.get_trip_target_level(),
                self.get_public_charging_session_done(),
                self.is_trip_planned(),
                self.fill_up_charging_enabled(),
                self.workplan_charging_enabled(),
                self.solar_charging_enabled(),
                self.manual_charging_enabled(),
                self.manual_charging_solar_enabled(),
            ),
        }

    @staticmethod
    def make_hashable_snapshot(data) -> str:
        def key_of(key):
            return key.isoformat() if isinstance(key, (datetime.datetime, datetime.date, datetime.time)) else str(key)

        def normalize(obj):
            if isinstance(obj, dict):
                return {key_of(key): normalize(value) for key, value in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [normalize(value) for value in obj]
            if isinstance(obj, set):
                return sorted(normalize(value) for value in obj)
            if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
                return obj.isoformat()
            return obj

        json_data = json.dumps(normalize(data), sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.blake2b(json_data.encode(), digest_size=16).hexdigest()

    def save_hashable_snapshot(self) -> None:
        for key, item in self.get_hash_dict().items():
            self.last_hash_results[key] = self.make_hashable_snapshot(item)

    def should_skip_calculation(self) -> bool:
        skip = True
        for key, item in self.get_hash_dict().items():
            if key not in self.last_hash_results:
                skip = False
                continue
            item_hash = self.make_hashable_snapshot(item)
            if item_hash != self.last_hash_results[key]:
                skip = False
                _LOGGER.debug("Hash snapshot changed for %s: %s != %s", key, self.last_hash_results[key], item_hash)
        return skip

    # ------------------------------------------------------------------ #
    # Static entities
    # ------------------------------------------------------------------ #

    def append_overview_output(self, title=None, timestamp=None) -> None:
        if timestamp is None:
            timestamp = self.now().strftime("%Y-%m-%d %H:%M")
        key = f"{timestamp} {title}"
        if key in self.overview_history:
            return
        self.overview_history[key] = [self.own_attr("overview", "overview", error_state="")]
        self.overview_history = limit_dict_size(self.overview_history, 10)

    def set_entity_friendlynames(self) -> None:
        """The 'name' attribute of the sensors that belong to a charging type."""
        for key, entry in CHARGING_TYPES.items():
            if "entity_name" in entry:
                self.set_attr(entry["entity_name"], "name", f"{entry['emoji']} {self.t(f'charging_type.{key}.description', default='No description')}")

    def emoji_description(self) -> None:
        lines = [f"## {self.t('ui.emoji_description.header')} ##"]
        for group in EMOJI_DESCRIPTION_GROUPS:
            entries = sorted(((key, entry) for key, entry in CHARGING_TYPES.items() if entry.get("group") == group), key=lambda kv: float(kv[1].get("priority", 0)))
            items = [f"**{entry.get('emoji', '❓')} {self.t(f'charging_type.{key}.description', default='No description')}**" for key, entry in entries]
            if items:
                lines.append(f"### {self.t(f'ui.emoji_description.groups.{group}')}")
                lines.extend(f"* {item}" for item in items)
        self.set_state("emoji_description", self.t("ui.emoji_description.markdown_hint", name=self.name))
        self.set_attr("emoji_description", "description", "\n".join(lines))

    def set_default_entity_states(self) -> None:
        self.set_state("overview", self.t("ui.set_default_entity_states.markdown_guide", name=self.name))
        self.set_attr("overview", "overview", f"<center>\n\n**{self.t('ui.set_default_entity_states.no_overview')}**\n\n</center>")

    # ------------------------------------------------------------------ #
    # Debug info
    # ------------------------------------------------------------------ #

    @classmethod
    def _plain(cls, value):
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, datetime.timedelta):
            return str(value)
        if isinstance(value, dict):
            return {cls._plain(k): cls._plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._plain(v) for v in value]
        return value

    def debug_info(self, extra: dict | None = None) -> str:
        extra = extra or {}
        sections = {
            "System Status": (
                {"PREHEATING": self.preheating, "TESTING": self.testing, "PUBLIC_CHARGING_SESSION": self.public_charging_sessions, **extra.get("status", {})},
                None,
            ),
            "System Configuration": (
                {
                    "CHARGER_CONFIGURED": self.charger_configured,
                    "SOLAR_CONFIGURED": self.solar_configured,
                    "POWERWALL_CONFIGURED": self.powerwall_configured,
                    "EV_CONFIGURED": self.ev_configured,
                    "FORECAST_TYPE": self.forecast_type,
                },
                {"CONFIG": self.config},
            ),
            "Worker": (extra.get("worker", {}), None),
            "Database": (
                None,
                {
                    "POWER_VALUES_DB": self.power_values_db,
                    "SOLAR_PRODUCTION_AVAILABLE_DB": self.solar_available_db,
                    "KWH_AVG_PRICES_DB": self.kwh_avg_prices_db,
                    "DRIVE_EFFICIENCY_DB": self.drive_efficiency_db,
                    "KM_KWH_EFFICIENCY_DB": self.km_kwh_efficiency_db,
                },
            ),
            "Charging Plan & Price Logic": (
                {"CURRENT_CHARGING_AMPS": self.current_charging_amps, "CURRENT_CIRCUIT_AMPS": self.current_circuit_amps, "CHARGER_ACTION": self.charger_action},
                {
                    "CHARGING_PLAN": self.charging_plan,
                    "CHARGE_HOURS": self.charge_hours,
                    "LAST_SUCCESSFUL_GRID_PRICES": self.grid_price_cache,
                    "LAST_SUCCESSFUL_SELL_PRICES": self.sell_price_cache,
                },
            ),
            "Charging Expenses": (None, {"BATTERY_LEVEL_EXPENSES": self.battery_level_expenses}),
            "Local Energy Forecast": (
                None,
                {
                    "LOCAL_ENERGY_PRICES": self.local_energy_prices,
                    "LOCAL_ENERGY_PREDICTION_DB": self.local_energy_prediction_db,
                    "MAX_LOCAL_ENERGY_AVAILABLE_HISTORY": self.max_local_energy_available_history,
                },
            ),
            "Charging Loss Metrics": (
                {
                    "CHARGING_LOSS_CAR_BEGIN_KWH": self.charging_loss_car_begin_kwh,
                    "CHARGING_LOSS_CAR_BEGIN_BATTERY_LEVEL": self.charging_loss_car_begin_battery_level,
                    "CHARGING_LOSS_CHARGER_BEGIN_KWH": self.charging_loss_charger_begin_kwh,
                    "CHARGING_LOSS_CHARGING_COMPLETED": self.charging_loss_charging_completed,
                },
                None,
            ),
            "Charging Sessions & History": (
                {
                    "LAST_WAKE_UP_DATETIME": self.last_wake_up_datetime,
                    "LAST_TRIP_CHANGE_DATETIME": self.last_trip_change_datetime,
                    "INTEGRATION_OFFLINE_TIMESTAMP": self.integration_offline_since,
                    "CHARGING_HISTORY_ENDING_BYTE_SIZE": self.charging_history_ending_byte_size,
                },
                {"CURRENT_CHARGING_SESSION": self.current_charging_session, "CHARGING_HISTORY_QUEUE_LAST_RESULT": self.charging_history_last_result},
            ),
            "Runtime Counters & Errors": (
                {
                    "CHARGING_IS_BEGINNING": self.charging_is_beginning,
                    "CHARGING_NO_RULE_COUNT": self.charging_no_rule_count,
                    "ERROR_COUNT": self.error_count,
                    "RESTARTING_CHARGER": self.restarting_charger,
                    "RESTARTING_CHARGER_COUNT": self.restarting_charger_count,
                },
                {"LAST_HASH_RESULTS": self.last_hash_results},
            ),
            "Driving Efficiency": (None, {"LAST_DRIVE_EFFICIENCY_DATA": self.last_drive_efficiency_data}),
        }
        lines = ["<center>\n", f"#### Debug info runtime {self.now()} ####\n", "</center>\n"]
        for section, (table, details) in sections.items():
            lines.append(f"<center>\n\n### {section}\n</center>\n")
            if table:
                lines += ["| Variable | Value |", "|---:|:---|"]
                lines += [f"| {key}: | {self._plain(value)} |" for key, value in table.items()]
            if table and details:
                lines.append("<br>\n")
            for key, value in (details or {}).items():
                lines += ["<details>", f"<summary>{key}: Show dictionary</summary>\n", f"```\n{pformat(self._plain(value))}\n```", "</details>\n"]
                if len(details) > 1:
                    lines.append("<br>\n")
            lines.append("---")
        if self.overview_history:
            lines.append("<center>\n\n### Overview History\n</center>\n")
            for title, overview in sorted(self.overview_history.items(), reverse=True):
                lines += ["<details>", f"<summary>{title}: Show snapshot</summary>\n", *overview, "</details>\n"]
        output = "\n".join(lines)
        self.persistent_notification(output, title=f"{self.title} debug info", notification_id=f"{self.name}_debug_info")
        return output

    def debug_state(self) -> dict:
        """The runtime state for the debug export (debug_export.py), encoded like the databases."""
        skip = {"config", "texts", "price_provider"}
        return {key: encode(value) for key, value in vars(self).items() if not key.startswith("_") and key not in skip}

    # ------------------------------------------------------------------ #
    # Convenience for the mixins
    # ------------------------------------------------------------------ #

    def start_of_day(self, date=None) -> datetime.datetime:
        return start_of_day(date or self.now())

    def end_of_day(self, date=None) -> datetime.datetime:
        return end_of_day(date or self.now())

    def time_plus_days(self, days: int) -> datetime.datetime:
        return self.now() + datetime.timedelta(days=days)

    @staticmethod
    def default_number(key: str) -> float:
        return float(NUMBER_SETTINGS[key]["default"])
