"""Export / import of the Cable Juice Planner data (backup.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError

from custom_components.cable_juice_planner.backup import BACKUP_DIR
from custom_components.cable_juice_planner.const import CONF_ENTITY_PREFIX, DB_NAMES, DOMAIN

from .conftest import OBJECT_PREFIX, async_unload_and_join, form_defaults

SWITCH = f"switch.{OBJECT_PREFIX}_workplan_charging"
TIME = f"time.{OBJECT_PREFIX}_workday_departure_monday"


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The backups are written to <config>/cable_juice_planner_backups, keep them out of the shared test config."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path / BACKUP_DIR


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_export_and_import_service(hass: HomeAssistant, config_dir: Path, loaded_entry, hass_storage) -> None:
    entry_id = loaded_entry.entry_id
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await hass.services.async_call("time", "set_value", {"entity_id": TIME, "time": "06:15:00"}, blocking=True)
    await _settle(hass)

    response = await hass.services.async_call(DOMAIN, "export_data", {}, blocking=True, return_response=True)
    file = response["files"][OBJECT_PREFIX]
    backup = json.loads((config_dir.parent / file).read_text(encoding="utf-8"))
    assert backup["integration"] == DOMAIN
    assert backup["settings"]["switch.workplan_charging"] == "on"
    assert backup["settings"]["time.workday_departure_monday"] == "06:15:00"
    assert {"charging_history", "power_values", "kwh_avg_prices", "drive_efficiency", "runtime"} <= set(backup["databases"])
    assert set(backup["databases"]) <= set(DB_NAMES)

    # change the data, then import the backup again
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    await hass.services.async_call("time", "set_value", {"entity_id": TIME, "time": "07:30:00"}, blocking=True)
    await _settle(hass)
    old_coordinator = loaded_entry.runtime_data
    await hass.services.async_call(
        DOMAIN, "import_data", {"config_entry_id": entry_id, "file": file}, blocking=True
    )
    await _settle(hass)
    await hass.async_add_executor_job(old_coordinator._executor.shutdown, True)

    assert loaded_entry.state is ConfigEntryState.LOADED
    assert loaded_entry.runtime_data is not old_coordinator
    assert hass.states.get(SWITCH).state == "on"
    assert hass.states.get(TIME).state == "06:15:00"
    # the engine changes the databases right after the start (the open charging session ends), so
    # the restored list is checked in the notification
    notification = persistent_notification._async_get_or_create_notifications(hass)[f"{OBJECT_PREFIX}_backup"]
    assert ", ".join(sorted(backup["databases"])) in notification["message"]
    # the replaced data was exported first
    assert any(name.endswith("_before_import.json") for name in (p.name for p in config_dir.iterdir()))


async def test_import_rejects_unknown_file(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "import_data", {"config_entry_id": loaded_entry.entry_id, "file": "../secrets.json"}, blocking=True
        )
    assert loaded_entry.state is ConfigEntryState.LOADED


async def test_deleted_entry_is_restored_by_the_config_flow(
    hass: HomeAssistant, config_dir: Path, loaded_entry, hass_storage
) -> None:
    entry_id = loaded_entry.entry_id
    await hass.services.async_call("time", "set_value", {"entity_id": TIME, "time": "06:15:00"}, blocking=True)
    await _settle(hass)
    databases = {name: hass_storage[f"{DOMAIN}.{entry_id}.{name}"]["data"] for name in ("charging_history", "drive_efficiency")}
    coordinator = loaded_entry.runtime_data
    await hass.config_entries.async_remove(entry_id)
    await _settle(hass)
    await hass.async_add_executor_job(coordinator._executor.shutdown, True)

    backups = [p.name for p in config_dir.iterdir()]
    assert len(backups) == 1 and backups[0].endswith("_deleted.json")
    assert f"{DOMAIN}.{entry_id}.power_values" not in hass_storage

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "instance"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: "restored", "language": "da"})
    assert result["step_id"] == "restore_backup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backup": backups[0]})
    # the pages are prefilled with the configuration of the backup
    assert result["step_id"] == "charger"
    while result["type"] is FlowResultType.FORM:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass)

    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    assert "restore_backup" not in entry.data  # only restored once
    assert hass.states.get("time.cjp_restored_workday_departure_monday").state == "06:15:00"
    for name, data in databases.items():
        assert hass_storage[f"{DOMAIN}.{entry.entry_id}.{name}"]["data"] == data
    await async_unload_and_join(hass, entry)


async def test_options_flow_export_and_import(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    exported = hass.states.get(TIME).state
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert "backup" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "backup"})
    assert result["step_id"] == "backup"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"backup": "export"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "backup_exported"
    name = result["description_placeholders"]["file"].split("/")[-1]

    await hass.services.async_call("time", "set_value", {"entity_id": TIME, "time": "05:05:00"}, blocking=True)
    await _settle(hass)
    old_coordinator = loaded_entry.runtime_data
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "backup"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"backup": name})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "backup_imported"
    await _settle(hass)
    await hass.async_add_executor_job(old_coordinator._executor.shutdown, True)
    assert hass.states.get(TIME).state == exported != "05:05:00"
