"""The charger control blueprint: a failing charger action must not keep the car from being told."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from homeassistant.components.automation import DOMAIN as AUTOMATION
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.cable_juice_planner.blueprint_install import SOURCE
from custom_components.cable_juice_planner.const import DOMAIN

from .conftest import async_advance

ACTION = "sensor.cjp_test_charger_action"

# the watchdog time_pattern trigger keeps its timer until Home Assistant stops
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """The default test config folder is shared, keep the blueprints out of it."""
    hass.config.config_dir = str(tmp_path)
    target = tmp_path / "blueprints" / "automation" / DOMAIN
    target.mkdir(parents=True)
    shutil.copy(SOURCE / "charger_control.yaml", target)


@pytest.fixture
def calls(hass: HomeAssistant) -> list[str]:
    """test.<name> services; the ones named fail_* raise like a charger cloud that does not answer."""
    called: list[str] = []

    def handler(call: ServiceCall) -> None:
        called.append(call.service)
        if call.service.startswith("fail_"):
            raise HomeAssistantError("charger cloud timeout")

    for name in ("enable", "fail_amps", "charger_start", "fail_charger_stop", "car_start", "car_stop"):
        hass.services.async_register("test", name, handler)
    return called


def _set_action(hass: HomeAssistant, state: str) -> None:
    hass.states.async_set(
        ACTION,
        state,
        {
            "mode": "normal",
            "charger_enabled": True,
            "phase_1_amps": 16 if state == "charge" else 0,
            "phase_2_amps": 0,
            "phase_3_amps": 0,
            "charging_limit": 0,
            "updated": dt_util.now().isoformat(),
        },
    )


async def _setup(hass: HomeAssistant, **inputs: Any) -> None:
    def service(name: str) -> list[dict[str, Any]]:
        return [{"action": f"test.{name}"}]

    assert await async_setup_component(
        hass,
        AUTOMATION,
        {
            AUTOMATION: {
                "alias": "charger control",
                "use_blueprint": {
                    "path": f"{DOMAIN}/charger_control.yaml",
                    "input": {
                        "action_sensor": ACTION,
                        "enable_actions": service("enable"),
                        "amps_actions": service("fail_amps"),
                        "start_actions": service("charger_start"),
                        "stop_actions": service("fail_charger_stop"),
                        "car_start_actions": service("car_start"),
                        "car_stop_actions": service("car_stop"),
                        **inputs,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()


async def test_car_is_started_after_a_failing_charger_action(hass: HomeAssistant, calls: list[str]) -> None:
    _set_action(hass, "stop")
    await _setup(hass)
    calls.clear()

    _set_action(hass, "charge")
    await hass.async_block_till_done()

    # the last fail_amps is the amps trigger, queued behind the action trigger
    assert calls == ["enable", "fail_amps", "charger_start", "car_start", "fail_amps"]


async def test_car_is_stopped_after_a_failing_charger_stop(hass: HomeAssistant, calls: list[str]) -> None:
    _set_action(hass, "charge")
    await _setup(hass)
    calls.clear()

    _set_action(hass, "stop")
    await hass.async_block_till_done()

    assert calls == ["enable", "fail_charger_stop", "car_stop", "fail_amps", "fail_amps"]


async def test_car_waits_for_the_charger(hass: HomeAssistant, calls: list[str]) -> None:
    _set_action(hass, "stop")
    await _setup(hass, car_start_delay=20)
    calls.clear()

    _set_action(hass, "charge")
    # async_block_till_done would sit out the real delay
    for _ in range(50):
        await asyncio.sleep(0)
    assert calls == ["enable", "fail_amps", "charger_start"]

    await async_advance(hass, 21)
    assert calls == ["enable", "fail_amps", "charger_start", "car_start", "fail_amps"]
