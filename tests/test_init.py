"""Setup, entities, triggers, services and unload of a Cable Juice Planner entry."""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.cable_juice_planner import _async_run_after_home_assistant_started
from custom_components.cable_juice_planner.const import (
    BUTTON_KEYS,
    DOMAIN,
    NUMBER_SETTINGS,
    SELECT_SETTINGS,
    SWITCH_SETTINGS,
    TIME_SETTINGS,
)

from .conftest import (
    CHARGER_KWH,
    CHARGER_POWER,
    EV_LEVEL as EV_LEVEL_ID,
    CHARGER_STATUS,
    CONSUMPTION,
    EV_PORT,
    MOCK_OPTIONS_NO_EV,
    OBJECT_PREFIX,
    async_advance,
    async_setup,
    async_unload_and_join,
    make_entry,
    set_source_states,
)


def _state(hass: HomeAssistant, platform: str, key: str) -> str:
    return hass.states.get(f"{platform}.{OBJECT_PREFIX}_{key}").state


def _expected_settings(requires_ok) -> list[str]:
    return [
        *(f"switch.{OBJECT_PREFIX}_{key}" for key, info in SWITCH_SETTINGS.items() if requires_ok(info.get("requires"))),
        *(f"number.{OBJECT_PREFIX}_{key}" for key, info in NUMBER_SETTINGS.items() if requires_ok(info.get("requires"))),
        *(f"select.{OBJECT_PREFIX}_{key}" for key in SELECT_SETTINGS),
        *(f"time.{OBJECT_PREFIX}_{key}" for key in TIME_SETTINGS),
        *(f"button.{OBJECT_PREFIX}_{key}" for key in BUTTON_KEYS),
    ]


async def test_post_setup_work_waits_until_home_assistant_started(hass: HomeAssistant, mock_entry) -> None:
    """Every car dashboard and the site must wait, not only the first entry."""
    action = AsyncMock()
    second_action = AsyncMock()
    second_entry = make_entry("second")
    hass.set_state(CoreState.starting)

    await _async_run_after_home_assistant_started(hass, mock_entry, action, "cjp_test_startup")
    await _async_run_after_home_assistant_started(hass, second_entry, second_action, "cjp_second_test_startup")
    action.assert_not_awaited()
    second_action.assert_not_awaited()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    action.assert_awaited_once_with()
    second_action.assert_awaited_once_with()

    action.reset_mock()
    await _async_run_after_home_assistant_started(hass, mock_entry, action, "cjp_test_reload")
    action.assert_awaited_once_with()


async def test_unload_removes_the_startup_listener_only_once(hass: HomeAssistant, mock_entry, caplog) -> None:
    """A reload after startup must not remove the fired one-time listener again (HA logs an error)."""
    action = AsyncMock()
    hass.set_state(CoreState.starting)
    with patch.object(mock_entry, "async_on_unload") as on_unload:
        await _async_run_after_home_assistant_started(hass, mock_entry, action, "cjp_test_startup")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    action.assert_awaited_once_with()

    on_unload.call_args.args[0]()
    assert "Unable to remove unknown job listener" not in caplog.text


async def test_unload_before_startup_cancels_the_work(hass: HomeAssistant, mock_entry) -> None:
    action = AsyncMock()
    hass.set_state(CoreState.starting)
    with patch.object(mock_entry, "async_on_unload") as on_unload:
        await _async_run_after_home_assistant_started(hass, mock_entry, action, "cjp_test_startup")
    on_unload.call_args.args[0]()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    action.assert_not_awaited()


async def test_setup_creates_entities(hass: HomeAssistant, loaded_entry) -> None:
    assert loaded_entry.state is ConfigEntryState.LOADED
    coordinator = loaded_entry.runtime_data
    assert coordinator.started
    assert coordinator.last_job["error"] is None

    ent_reg = er.async_get(hass)
    expected = [
        *_expected_settings(lambda requires: requires in (None, "ev")),
        f"sensor.{OBJECT_PREFIX}_current_charging_rule",
        f"sensor.{OBJECT_PREFIX}_charger_action",
        f"event.{OBJECT_PREFIX}_ev_request",
    ]
    missing = [entity_id for entity_id in expected if ent_reg.async_get(entity_id) is None or hass.states.get(entity_id) is None]
    assert not missing
    # charger only entities are not created with a car integration
    assert hass.states.get(f"number.{OBJECT_PREFIX}_battery_level") is None
    # solar entities need a production entity
    assert hass.states.get(f"switch.{OBJECT_PREFIX}_solar_charging") is None


async def test_no_charging_mode_charges_like_a_dumb_charger(hass: HomeAssistant, loaded_entry) -> None:
    """With every charging mode off ev.py charged at max amps (⛔ no charging rules active)."""
    assert _state(hass, "sensor", "charger_action") == "charge"
    assert _state(hass, "sensor", "current_charging_rule").startswith("⛔")
    assert _state(hass, "sensor", "charger_phase_1_amps") == "16"


async def test_fill_up_stops_charging_when_unplugged(hass: HomeAssistant, loaded_entry) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": f"switch.{OBJECT_PREFIX}_fill_up"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data.last_job["job"] == "charging_rule_changed"
    # the new plan follows after the 5 s settings debounce
    await async_advance(hass, 6)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data.last_job["job"] == "recalculate"
    assert _state(hass, "sensor", "charger_action") == "stop"
    assert _state(hass, "sensor", "charger_phase_1_amps") == "0"


async def test_fill_up_and_workplan_exclude_each_other(hass: HomeAssistant, loaded_entry) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": f"switch.{OBJECT_PREFIX}_fill_up"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": f"switch.{OBJECT_PREFIX}_workplan_charging"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, "switch", "workplan_charging") == "on"
    assert _state(hass, "switch", "fill_up") == "off"


async def test_number_setting_debounces_recalculation(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    await hass.services.async_call(
        "number", "set_value", {"entity_id": f"number.{OBJECT_PREFIX}_min_daily_battery_level", "value": 45}, blocking=True
    )
    assert coordinator.settings["min_daily_battery_level"] == 45
    assert coordinator._settings_debounce is not None
    await async_advance(hass, 6)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.last_job["job"] == "recalculate"
    assert coordinator.last_job["error"] is None


async def test_plugging_in_runs_the_charger_jobs(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    jobs: list[str] = []
    original = coordinator.async_run_job

    async def _record(job_name, *args, **kwargs):
        jobs.append(job_name)
        return await original(job_name, *args, **kwargs)

    with patch.object(coordinator, "async_run_job", _record):
        hass.states.async_set(EV_PORT, "on")
        hass.states.async_set(CHARGER_STATUS, "awaiting_start")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "ev_port_changed" in jobs
    assert jobs.index("charger_status_changed") < jobs.index("charger_connected")
    assert coordinator.last_job["error"] is None


async def test_kwh_meter_change_is_ignored_without_charging_loss(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    coordinator.last_job.clear()
    hass.states.async_set(CHARGER_KWH, "1.5", {"unit_of_measurement": "kWh", "device_class": "energy"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.last_job == {}


async def test_enforce_planning_and_debug_info(hass: HomeAssistant, loaded_entry) -> None:
    await hass.services.async_call(DOMAIN, "enforce_planning", {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data.last_job["job"] == "charge_trigger"
    response = await hass.services.async_call(DOMAIN, "debug_info", {"config_entry_id": loaded_entry.entry_id}, blocking=True, return_response=True)
    assert response["debug_info"][OBJECT_PREFIX]


async def test_enforce_planning_button(hass: HomeAssistant, loaded_entry) -> None:
    await hass.services.async_call("button", "press", {"entity_id": f"button.{OBJECT_PREFIX}_enforce_planning"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data.last_job["job"] == "charge_trigger"


async def test_manual_public_charging_session(hass: HomeAssistant, loaded_entry) -> None:
    start = dt_util.now().replace(microsecond=0) - dt_util.dt.timedelta(days=1)
    response = await hass.services.async_call(
        DOMAIN,
        "manual_add_public_charging_session",
        {"charging_session_start": start.isoformat(), "battery_level_begin": 20, "battery_level_end": 60, "total_charging_cost": 150},
        blocking=True,
        return_response=True,
    )
    assert response["status"] == "success", response
    assert response["data"]["Added Percentage"] == 40.0
    assert response["data"]["Total Charging Cost"] == 150.0
    # 40 % of 75 kWh, charging loss included
    assert response["data"]["Added kWh"] == pytest.approx(30 / 0.95, rel=0.02)


async def test_settings_and_databases_survive_reload(hass: HomeAssistant, loaded_entry, hass_storage) -> None:
    entry_id = loaded_entry.entry_id
    await hass.services.async_call("switch", "turn_on", {"entity_id": f"switch.{OBJECT_PREFIX}_workplan_charging"}, blocking=True)
    await hass.services.async_call("time", "set_value", {"entity_id": f"time.{OBJECT_PREFIX}_workday_departure_monday", "time": "06:15:00"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    for name in ("charging_history", "power_values", "kwh_avg_prices", "drive_efficiency", "runtime"):
        assert f"{DOMAIN}.{entry_id}.{name}" in hass_storage, name

    old_coordinator = loaded_entry.runtime_data
    assert await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_add_executor_job(old_coordinator._executor.shutdown, True)

    assert loaded_entry.runtime_data is not old_coordinator
    assert _state(hass, "switch", "workplan_charging") == "on"
    assert _state(hass, "time", "workday_departure_monday") == "06:15:00"


async def test_entity_names_follow_the_language_of_the_entry(hass: HomeAssistant, loaded_entry) -> None:
    """HA names translation_key entities in its UI language (en), CJP in the entry's (da, then en)."""
    assert hass.config.language == "en"
    entity_id = f"number.{OBJECT_PREFIX}_trip_range_needed"
    # {distance} is filled in like HA does for its own translations
    assert er.async_get(hass).async_get(entity_id).original_name == "Tur km forbrug"

    old_coordinator = loaded_entry.runtime_data
    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "language": "en"})
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_add_executor_job(old_coordinator._executor.shutdown, True)

    assert loaded_entry.runtime_data is not old_coordinator
    assert er.async_get(hass).async_get(entity_id).original_name == "Trip km needed"
    assert hass.states.get(entity_id).attributes["friendly_name"].endswith("Trip km needed")


async def test_charger_only_instance_without_total_range(hass: HomeAssistant, caplog) -> None:
    """number.cjp_<prefix>_estimated_total_range is 0 until entered: 3 km per % is used, not a division by zero."""
    set_source_states(hass, ev=False)
    entry = make_entry(options=MOCK_OPTIONS_NO_EV)
    await async_setup(hass, entry)
    coordinator = entry.runtime_data
    assert float(_state(hass, "number", "estimated_total_range")) == 0.0
    assert coordinator.engine.charger_mode != "error"
    assert "division by zero" not in caplog.text
    assert "Error running charge_if_needed" not in caplog.text
    await async_unload_and_join(hass, entry)


async def test_charger_only_instance(hass: HomeAssistant) -> None:
    """Without a car integration CJP waits (max 30 min) for the battery level entered in number.cjp_<prefix>_battery_level."""
    set_source_states(hass, ev=False)
    entry = make_entry(options=MOCK_OPTIONS_NO_EV)
    await async_setup(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.started
    assert coordinator.last_job["error"] is None
    assert hass.states.get(f"number.{OBJECT_PREFIX}_battery_level") is not None
    assert hass.states.get(f"event.{OBJECT_PREFIX}_ev_request") is None

    jobs: list[str] = []
    original = coordinator.async_run_job

    # the clock is real: advancing it can pass a cron time (every 5 minutes, hour end, ...),
    # only the jobs of plugging in count here
    timed = {"cron", "hour_end", "price_update", "append_prices", "preheat_cron", "charging_loss_update", "economy_update", "analysis_update"}

    async def _record(job_name, *args, **kwargs):
        if job_name not in timed:
            jobs.append(job_name)
        return await original(job_name, *args, **kwargs)

    async def _tick(times: int) -> None:
        for _ in range(times):
            await async_advance(hass, 5)

    with patch.object(coordinator, "async_run_job", _record):
        hass.states.async_set(CHARGER_STATUS, "awaiting_start")
        await _tick(3)  # charger_status_changed, then the wait loop polls every 5 s
        assert jobs == ["charger_status_changed"]

        await hass.services.async_call("number", "set_value", {"entity_id": f"number.{OBJECT_PREFIX}_battery_level", "value": 35}, blocking=True)
        await _tick(3)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "charger_connected" in jobs
    assert coordinator.settings["battery_level"] == 35
    assert coordinator.last_job["error"] is None
    await async_unload_and_join(hass, entry)


async def test_missing_entity_waits_and_notifies(hass: HomeAssistant, mock_entry) -> None:
    set_source_states(hass)
    hass.states.async_remove(CONSUMPTION)
    mock_entry.add_to_hass(hass)
    with patch.object(persistent_notification, "async_create", wraps=persistent_notification.async_create) as create:
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        assert not mock_entry.runtime_data.started
        await async_advance(hass, 121)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_entry.runtime_data.started
    calls = {call.kwargs.get("notification_id"): call for call in create.call_args_list}
    assert CONSUMPTION in calls[f"{mock_entry.data['entity_prefix']}_validation_procedure_missing_entity_ids"].args[1]
    await async_unload_and_join(hass, mock_entry)


async def test_unload(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    await async_unload_and_join(hass, loaded_entry)
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert not coordinator._unsubs
    assert _state(hass, "sensor", "charger_action") == "unavailable"


async def test_migration_moves_solar_sell_price_to_options(hass: HomeAssistant, mock_entry) -> None:
    """2.1 -> 2.2: the number value becomes an option, the removed entities leave the registry."""
    ent_reg = er.async_get(hass)
    mock_entry.add_to_hass(hass)
    removed = {}
    for platform, key in (("number", "solar_sell_fixed_price"), ("switch", "deactivate_script"), ("button", "restart_script")):
        removed[key] = ent_reg.async_get_or_create(platform, DOMAIN, f"{mock_entry.entry_id}_{key}", suggested_object_id=f"{OBJECT_PREFIX}_{key}").entity_id
    mock_restore_cache(hass, [State(removed["solar_sell_fixed_price"], "0.85")])
    set_source_states(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_entry.minor_version == 3  # and on through the later migrations
    assert mock_entry.options["solar_sell_fixed_price"] == 0.85
    assert mock_entry.runtime_data.config["solar"]["production_price"] == 0.85
    assert all(ent_reg.async_get(entity_id) is None for entity_id in removed.values())
    await async_unload_and_join(hass, mock_entry)


async def test_migration_moves_last_full_charge_to_the_sensor(hass: HomeAssistant, mock_entry) -> None:
    """2.2 -> 2.3: the datetime entity becomes a sensor the user cannot change, its value is kept."""
    ent_reg = er.async_get(hass)
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_entry, minor_version=2)
    old = ent_reg.async_get_or_create("datetime", DOMAIN, f"{mock_entry.entry_id}_last_full_charge", suggested_object_id=f"{OBJECT_PREFIX}_last_full_charge").entity_id
    mock_restore_cache(hass, [State(old, "2026-10-14T08:00:00+00:00")])
    set_source_states(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_entry.minor_version == 3
    assert ent_reg.async_get(old) is None
    sensor = hass.states.get(f"sensor.{OBJECT_PREFIX}_last_full_charge")
    assert sensor is not None and dt_util.parse_datetime(sensor.state) == dt_util.parse_datetime("2026-10-14T08:00:00+00:00")
    await async_unload_and_join(hass, mock_entry)


async def test_charging_loss_reads_the_recorder(hass: HomeAssistant, loaded_entry) -> None:
    """The first time: raw history and hourly statistics, kW and Wh converted, the sensor published."""
    from types import SimpleNamespace  # noqa: PLC0415

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    from .test_charging_loss_model import _session  # noqa: PLC0415

    coordinator = loaded_entry.runtime_data
    start = dt_util.utcnow().timestamp() - 2 * 86400
    sessions = [_session(start, 11.04, 3, 3), _session(start + 43200, 4.14, 3, 4)]

    def state(entity_id, ts, value):
        moment = dt_util.utc_from_timestamp(ts)
        return State(entity_id, str(value), last_changed=moment, last_updated=moment)

    states = {
        EV_LEVEL_ID: [state(EV_LEVEL_ID, ts, v) for soc, _, _ in sessions for ts, v in soc],
        CHARGER_KWH: [state(CHARGER_KWH, ts, v * 1000) for _, meter, _ in sessions for ts, v in meter],  # in Wh
    }
    power = {CHARGER_POWER: [{"start": ts, "mean": m / 1000, "min": lo / 1000, "max": hi / 1000} for _, _, rows in sessions for ts, m, lo, hi in rows]}  # in kW
    metadata = {CHARGER_POWER: (1, {"unit_of_measurement": "kW"}), CHARGER_KWH: (2, {"unit_of_measurement": "Wh"})}

    def statistics(_hass, _start, _end, _ids, period, *_args):
        return power if period == "5minute" else {}

    hass.config.components.add("recorder")
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job, keep_days=30)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.history.get_significant_states", return_value=states),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics),
        patch("homeassistant.components.recorder.statistics.get_metadata", return_value=metadata),
    ):
        await coordinator._async_charging_loss_refresh(backfill=True)
        await hass.async_block_till_done()
    sensor = hass.states.get(f"sensor.{OBJECT_PREFIX}_charging_loss_model")
    assert sensor.attributes["status"] == "model"
    assert sensor.attributes["backfilled"] is True
    assert {s["phases"] for s in sensor.attributes["segments"]} == {3}
    assert 0.15 <= sensor.attributes["fixed_kw"] <= 0.25
    assert coordinator.engine.charging_loss_backfilled()



async def test_charging_loss_statistics_only_as_far_back_as_needed(hass: HomeAssistant, loaded_entry) -> None:
    """30 days at a time: it stops when every group is full or the statistics end."""
    from types import SimpleNamespace  # noqa: PLC0415

    coordinator = loaded_entry.runtime_data
    calls: list[str] = []

    def statistics(_hass, start, _end, _ids, period, *_args):
        if period == "5minute":
            return {}
        calls.append(start.date().isoformat())
        # the statistics of the power sensor began 3 chunks back
        return {CHARGER_POWER: [{"start": start.timestamp(), "mean": 0.0, "min": 0.0, "max": 0.0}]} if len(calls) <= 3 else {}

    hass.config.components.add("recorder")
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job, keep_days=30)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.history.get_significant_states", return_value={}),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics),
        patch("homeassistant.components.recorder.statistics.get_metadata", return_value={}),
    ):
        await coordinator.async_recalculate_charging_loss()
        await hass.async_block_till_done()
    assert len(calls) == 4  # three chunks with data, the fourth is empty
    assert coordinator.engine.charging_loss_backfilled()
    assert hass.states.get(f"button.{OBJECT_PREFIX}_recalculate_charging_loss") is not None


async def test_economy_reads_the_recorder(hass: HomeAssistant, loaded_entry) -> None:
    """Hourly prices, the odometer per month and (the first time) the connection windows."""
    from types import SimpleNamespace  # noqa: PLC0415

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    coordinator = loaded_entry.runtime_data
    now = dt_util.utcnow()
    plugged, unplugged = now - datetime.timedelta(hours=30), now - datetime.timedelta(hours=10)
    states = {CHARGER_STATUS: [
        State(CHARGER_STATUS, "disconnected", last_changed=plugged - datetime.timedelta(hours=1)),
        State(CHARGER_STATUS, "awaiting_start", last_changed=plugged),
        State(CHARGER_STATUS, "disconnected", last_changed=unplugged),
    ]}
    hour = now.replace(minute=0, second=0, microsecond=0)

    def statistics(_hass, _start, _end, ids, period, *_args):
        if period == "hour":
            return {next(iter(ids)): [{"start": (hour - datetime.timedelta(hours=h)).timestamp(), "mean": 1.0 + h / 100} for h in range(48)]}
        return {next(iter(ids)): [{"start": hour.replace(day=1).timestamp(), "change": 812.0}]}

    hass.config.components.add("recorder")
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job, keep_days=30)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.history.get_significant_states", return_value=states),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics),
    ):
        await coordinator._async_economy_refresh()
        await hass.async_block_till_done()
    windows = coordinator.engine.economy_db["windows"]
    assert (windows[0]["start"], windows[0]["end"]) == (plugged.timestamp(), unplugged.timestamp())
    assert coordinator.engine.economy_db["backfilled"]
    months = hass.states.get(f"sensor.{OBJECT_PREFIX}_economy").attributes["months"]
    assert months and 812.0 in coordinator.engine.economy_db["odometer_months"].values()


async def test_analysis_reads_the_recorder(hass: HomeAssistant, loaded_entry) -> None:
    """The first time the drives of the raw history, every time the hourly outdoor temperature."""
    from types import SimpleNamespace  # noqa: PLC0415

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    from .conftest import EV_ODOMETER  # noqa: PLC0415

    coordinator = loaded_entry.runtime_data
    coordinator.config["forecast"]["entity_ids"]["outdoor_temp_entity_id"] = "sensor.outdoor"
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    away, home = hour - datetime.timedelta(hours=10), hour - datetime.timedelta(hours=2)

    def state(entity_id, moment, value):
        return State(entity_id, str(value), last_changed=moment, last_updated=moment)

    states = {
        CHARGER_STATUS: [state(CHARGER_STATUS, away - datetime.timedelta(hours=8), "completed"), state(CHARGER_STATUS, away, "disconnected"), state(CHARGER_STATUS, home, "awaiting_start")],
        EV_ODOMETER: [state(EV_ODOMETER, away - datetime.timedelta(hours=9), 20000), state(EV_ODOMETER, home - datetime.timedelta(minutes=5), 20090)],
        EV_LEVEL_ID: [state(EV_LEVEL_ID, away - datetime.timedelta(hours=9), 80), state(EV_LEVEL_ID, home - datetime.timedelta(minutes=5), 62)],
    }

    def statistics(_hass, _start, _end, ids, *_args):
        return {next(iter(ids)): [{"start": (hour - datetime.timedelta(hours=h)).timestamp(), "mean": -2.0} for h in range(30)]}

    hass.config.components.add("recorder")
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job, keep_days=30)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.history.get_significant_states", return_value=states),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics),
    ):
        await coordinator._async_analysis_refresh()
        await hass.async_block_till_done()
    drives = hass.states.get(f"sensor.{OBJECT_PREFIX}_analysis").attributes["drives"]
    assert len(drives) == 1 and drives[0]["km"] == 90.0 and drives[0]["temp"] == -2.0
    assert coordinator.engine.analysis_backfilled()


async def test_economy_uses_the_raw_price_history_without_statistics(hass: HomeAssistant, loaded_entry) -> None:
    """A price sensor without state_class has no statistics: its raw history gives the hourly prices."""
    from types import SimpleNamespace  # noqa: PLC0415

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    from .conftest import PRICE  # noqa: PLC0415

    coordinator = loaded_entry.runtime_data
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    raw = {PRICE: [State(PRICE, str(1.0 + h / 10), last_changed=hour - datetime.timedelta(hours=h), last_updated=hour - datetime.timedelta(hours=h)) for h in range(5, 0, -1)]}
    calls = []

    def history_states(_hass, _start, _end, ids, *_args, **_kwargs):
        calls.append(list(ids))
        return raw if PRICE in ids else {}

    hass.config.components.add("recorder")
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job, keep_days=30)
    captured = {}
    original = coordinator.async_run_job

    async def run_job(name, *args, **kwargs):
        if name == "economy_update":
            captured.update(args[0])
        return await original(name, *args, **kwargs)

    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.history.get_significant_states", side_effect=history_states),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", return_value={}),
        patch.object(coordinator, "async_run_job", side_effect=run_job),
    ):
        await coordinator._async_economy_refresh()
    assert [PRICE] in calls
    prices = captured["prices"]
    assert prices[(hour - datetime.timedelta(hours=3)).timestamp()] == pytest.approx(1.3)
