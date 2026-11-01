"""Debug export for GitHub issues (management/debug_export.py) and its replay (tests/debug_replay.py)."""
from __future__ import annotations

import datetime
import gzip
import json
import logging
from pathlib import Path

import pytest

from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cable_juice_planner.const import CONF_CAR_NAME, DOMAIN
from custom_components.cable_juice_planner.management.debug_export import DEBUG_DIR

from . import debug_replay
from .conftest import CONSUMPTION, EV_LOCATION, EV_LEVEL, OBJECT_PREFIX, make_entry

CAR_NAME = "Rasmussens Bil"


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    """The entry of conftest with a car name."""
    entry = make_entry()
    return MockConfigEntry(
        domain=entry.domain, version=entry.version, title=entry.title, data={**entry.data, CONF_CAR_NAME: CAR_NAME}, options=dict(entry.options)
    )


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    return tmp_path / DEBUG_DIR


def _read(path: Path) -> tuple[dict, str]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        text = file.read()
    return json.loads(text), text


async def test_export_is_anonymised(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    hass.states.async_set(EV_LOCATION, "Arbejde", {"latitude": 55.6761, "longitude": 12.5683, "friendly_name": f"{CAR_NAME} location"})
    coordinator = loaded_entry.runtime_data
    await coordinator.async_run_job("cron", include_forecast=True)
    logging.getLogger("custom_components.cable_juice_planner.engine").warning("%s reads %s", CAR_NAME, CONSUMPTION)

    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    data, text = _read(config_dir.parent / response["files"][OBJECT_PREFIX])

    assert data["integration"] == DOMAIN and data["prefix"] == OBJECT_PREFIX
    assert data["jobs"] and data["snapshots"] and data["engine"]
    for secret in (CONSUMPTION, EV_LOCATION, EV_LEVEL, "Rasmussen", "55.67", "12.56"):
        assert secret not in text, secret
    location = data["snapshots"][-1]["snapshot"]["attributes"]["device_tracker.location"]
    assert location == {"latitude": 56, "longitude": 13, "friendly_name": "<redacted>"}
    assert data["snapshots"][-1]["snapshot"]["astral"]["latitude"] == round(hass.config.latitude)
    assert any(record["message"] == "<redacted> reads sensor.power_consumption" for record in data["log"])
    assert f"sensor.{OBJECT_PREFIX}_" in text
    # time settings stay times for the replay, the prices are there even when the snapshots have none
    assert any(isinstance(value, dict) and "__time__" in value for value in data["settings"].values())
    assert data["price_data"]["attributes"]

    item = hass.data["persistent_notification"][f"{OBJECT_PREFIX}_debug_export"]
    assert "Cable-Juice-Planner-Integration/issues/new?template=bug_report.yml" in item["message"]


async def test_download_view(hass: HomeAssistant, config_dir: Path, loaded_entry, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    await hass.services.async_call("button", "press", {"entity_id": f"button.{OBJECT_PREFIX}_export_debug"}, blocking=True)
    name = next(config_dir.iterdir()).name
    client = await hass_client_no_auth()
    assert (await client.get(f"/api/{DOMAIN}/debug/{name}")).status == 401
    download = await client.get(async_sign_path(hass, f"/api/{DOMAIN}/debug/{name}", datetime.timedelta(minutes=5), use_content_user=True))
    assert download.status == 200
    assert gzip.decompress(await download.read()).startswith(b"{")


async def test_replay_reproduces_the_plan(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    await coordinator.async_run_job("cron", include_forecast=True)
    recorded = coordinator.data["current_charging_rule"]
    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    export = debug_replay.load_export(str(config_dir.parent / response["files"][OBJECT_PREFIX]))
    assert export["snapshots"][-1]["job"] == "cron"
    assert export["planning_job"]["job"] == "cron"

    engine = debug_replay.build_engine(export)
    engine.sensor_states.pop("current_charging_rule", None)
    result = await hass.async_add_executor_job(debug_replay.replay, export, None, engine)
    assert result.error is None
    assert engine.sensor_states["current_charging_rule"] == recorded
