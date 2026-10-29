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

from custom_components.cable_juice_planner.management.blueprint_install import SOURCE
from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.sensor import charger_decision

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

    for name in ("enable", "disable", "fail_amps", "charger_start", "fail_charger_stop", "car_start", "car_stop", "car_limit", "circuit"):
        hass.services.async_register("test", name, handler)
    return called


def _set_action(
    hass: HomeAssistant,
    state: str,
    circuit: list[int] | None = None,
    amps: list[int] | None = None,
    enabled: bool = True,
    limit: int = 0,
) -> None:
    """The charger action as the integration publishes it, incl. the decision the blueprint triggers on."""
    attributes: dict[str, Any] = {
        "mode": "normal",
        "charger_enabled": enabled,
        **{f"phase_{i}_amps": value for i, value in enumerate(amps or [16 if state == "charge" else 0, 0, 0], start=1)},
        **{f"circuit_phase_{i}_amps": value for i, value in enumerate(circuit or [], start=1)},
        "charging_limit": limit,
        "updated": dt_util.now().isoformat(),
    }
    attributes["decision"] = charger_decision(state, attributes)
    hass.states.async_set(ACTION, state, attributes)


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

    # one run for the new action and its amps
    assert calls == ["enable", "fail_amps", "charger_start", "car_start"]


async def test_car_is_stopped_after_a_failing_charger_stop(hass: HomeAssistant, calls: list[str]) -> None:
    _set_action(hass, "charge")
    await _setup(hass)
    calls.clear()

    _set_action(hass, "stop")
    await hass.async_block_till_done()

    assert calls == ["enable", "fail_charger_stop", "car_stop", "fail_amps"]


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
    assert calls == ["enable", "fail_amps", "charger_start", "car_start"]


async def _run_loop(hass: HomeAssistant) -> None:
    """Let the automation run until it waits (async_block_till_done would sit out the real delay)."""
    for _ in range(50):
        await asyncio.sleep(0)


async def test_charger_commands_wait_for_each_other(hass: HomeAssistant, calls: list[str]) -> None:
    """Wait between charger commands: a pause between two charger commands, none before the car."""
    _set_action(hass, "stop", circuit=[0, 0, 0])
    await _setup(hass, command_delay=5, circuit_actions=_service("circuit"), car_amps_actions=_service("car_amps"))
    hass.services.async_register("test", "car_amps", lambda call: calls.append(call.service))
    calls.clear()

    _set_action(hass, "charge", circuit=[16, 0, 0])
    await _run_loop(hass)
    assert calls == ["enable"]  # waiting before the next charger command

    # (firing the time also runs the later pauses' timers)
    await async_advance(hass, 20)
    assert calls == ["enable", "circuit", "fail_amps", "car_amps", "charger_start", "car_start"]


def _service(name: str) -> list[dict[str, Any]]:
    return [{"action": f"test.{name}"}]


async def test_one_run_carries_out_everything_that_changed(hass: HomeAssistant, calls: list[str]) -> None:
    """Amps on all phases, the circuit and the charge limit change in one calculation: one run, each once."""
    _set_action(hass, "charge", circuit=[16, 0, 0])
    await _setup(hass, circuit_actions=_service("circuit"), car_limit_actions=_service("car_limit"))
    calls.clear()

    _set_action(hass, "charge", circuit=[10, 10, 10], amps=[10, 10, 10], limit=80)
    await hass.async_block_till_done()
    assert calls == ["car_limit", "circuit", "fail_amps"]

    # a change on phase 2 alone
    calls.clear()
    _set_action(hass, "charge", circuit=[10, 10, 10], amps=[10, 6, 10], limit=80)
    await hass.async_block_till_done()
    assert calls == ["fail_amps"]

    # only the heartbeat: no run at all
    calls.clear()
    last_triggered = hass.states.get("automation.charger_control").attributes["last_triggered"]
    _set_action(hass, "charge", circuit=[10, 10, 10], amps=[10, 6, 10], limit=80)
    await hass.async_block_till_done()
    assert calls == []
    assert hass.states.get("automation.charger_control").attributes["last_triggered"] == last_triggered


async def test_disabled_charger_is_switched_off(hass: HomeAssistant, calls: list[str]) -> None:
    _set_action(hass, "charge")
    await _setup(hass, disable_actions=_service("disable"))
    calls.clear()

    _set_action(hass, "charge", enabled=False)
    await hass.async_block_till_done()
    assert calls == ["disable"]

    calls.clear()
    _set_action(hass, "charge")
    await hass.async_block_till_done()
    assert calls == ["enable"]


async def test_master_sets_the_shared_circuit(hass: HomeAssistant, calls: list[str]) -> None:
    """Another car on the circuit starts: only the circuit limit of the master's automation changes."""
    circuits: list[dict[str, Any]] = []

    def circuit(call: ServiceCall) -> None:
        calls.append(call.service)
        circuits.append(dict(call.data))

    hass.services.async_remove("test", "circuit")
    hass.services.async_register("test", "circuit", circuit)
    _set_action(hass, "stop", circuit=[0, 0, 0])
    circuit_action = [{"action": "test.circuit", "data": {f"p{i}": f"{{{{ circuit_phase_{i} }}}}" for i in (1, 2, 3)}}]
    await _setup(hass, circuit_actions=circuit_action)
    calls.clear()
    circuits.clear()

    _set_action(hass, "stop", circuit=[16, 16, 16])
    await hass.async_block_till_done()
    assert calls == ["circuit"]
    assert [int(circuits[0][f"p{i}"]) for i in (1, 2, 3)] == [16, 16, 16]

    # its own start sets the circuit before the charging current
    calls.clear()
    circuits.clear()
    _set_action(hass, "charge", circuit=[32, 16, 16])
    await hass.async_block_till_done()
    assert calls[:3] == ["enable", "circuit", "fail_amps"]
    assert [int(circuits[0][f"p{i}"]) for i in (1, 2, 3)] == [32, 16, 16]
