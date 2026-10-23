"""Setup, entities, triggers, services and unload of a Cable Juice Planner entry."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

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

    async def _record(job_name, *args, **kwargs):
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
