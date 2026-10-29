"""Load balancing: several cars behind one main fuse (site config entry)."""
from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.const import CONF_CIRCUITS, DOMAIN, SITE_SENSORS
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.site import CJPSite, update_circuits

from .conftest import (
    CHARGER_STATUS,
    MOCK_OPTIONS,
    async_advance,
    async_setup,
    async_unload_and_join,
    make_entry,
    make_site_entry,
    register_charger,
    set_source_states,
)

EV_B_LEVEL = "sensor.ev_b_battery_level"
CHARGER_B_STATUS = "sensor.charger_b_status"


def _amps(hass: HomeAssistant, prefix: str) -> list[int]:
    return [int(hass.states.get(f"sensor.cjp_{prefix}_charger_phase_{i}_amps").state) for i in (1, 2, 3)]


async def _settle(hass: HomeAssistant) -> None:
    """Let the site recalculate the cars behind a changed plan (15 s delay) until nothing changes."""
    for _ in range(4):
        await async_advance(hass, 16)
        await hass.async_block_till_done(wait_background_tasks=True)


async def _setup_two_cars(
    hass: HomeAssistant, b_level: float = 60, circuits: list | None = None
) -> tuple[MockConfigEntry, MockConfigEntry, MockConfigEntry]:
    set_source_states(hass)
    hass.states.async_set(EV_B_LEVEL, str(b_level), {"unit_of_measurement": "%", "device_class": "battery"})
    hass.states.async_set(CHARGER_B_STATUS, "disconnected")
    site = make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **({CONF_CIRCUITS: circuits} if circuits else {}))
    await async_setup(hass, site)
    car_a = make_entry("a", {**MOCK_OPTIONS, "site_priority": 1})
    car_b = make_entry("b", {**MOCK_OPTIONS, "site_priority": 2, "usable_battery_level_entity_id": EV_B_LEVEL, "status_entity_id": CHARGER_B_STATUS})
    await async_setup(hass, car_a)
    await async_setup(hass, car_b)
    await _settle(hass)
    return site, car_a, car_b


async def _unload(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    for entry in entries:
        await async_unload_and_join(hass, entry)


async def test_site_sensors(hass: HomeAssistant) -> None:
    set_source_states(hass)
    site = make_site_entry()
    await async_setup(hass, site)
    for key in SITE_SENSORS:
        assert hass.states.get(f"sensor.cjp_{key}") is not None, key
    # fuse 25 A - 1 A margin, no house sensors, no cars
    assert float(hass.states.get("sensor.cjp_site_available_l1").state) == 24.0
    await hass.config_entries.async_unload(site.entry_id)


async def test_cars_register_with_the_site(hass: HomeAssistant) -> None:
    site, car_a, car_b = await _setup_two_cars(hass)
    members = hass.data[DOMAIN]["site"].members
    assert set(members) == {car_a.entry_id, car_b.entry_id}
    assert hass.data[DOMAIN]["site"].order() == [car_a.entry_id, car_b.entry_id]
    await _unload(hass, car_a, car_b, site)


async def test_lower_priority_gets_what_is_left(hass: HomeAssistant) -> None:
    """Both cars want 3 x 16 A (no charging modes = max), the fuse allows 24 A per phase."""
    site, car_a, car_b = await _setup_two_cars(hass)
    assert _amps(hass, "a") == [16, 16, 16]
    assert _amps(hass, "b") == [8, 8, 8]
    assert float(hass.states.get("sensor.cjp_site_available_l1").state) == 0.0
    await _unload(hass, car_a, car_b, site)


async def test_urgent_car_overtakes(hass: HomeAssistant) -> None:
    """Car b under its daily minimum (30 %) charges first despite the lower priority."""
    site, car_a, car_b = await _setup_two_cars(hass, b_level=10)
    assert hass.data[DOMAIN]["site"].order() == [car_b.entry_id, car_a.entry_id]
    assert _amps(hass, "b") == [16, 16, 16]
    assert _amps(hass, "a") == [8, 8, 8]
    await _unload(hass, car_a, car_b, site)


async def test_fuse_budget_respected_after_cron(hass: HomeAssistant) -> None:
    """Both cars recalculate in the same cron minute; the sum must stay within 24 A per phase."""
    site, car_a, car_b = await _setup_two_cars(hass, b_level=10)
    for cron in range(1, 4):
        # the cron trigger is rescheduled from the fired time, so each run fires 5 min later
        await async_advance(hass, cron * 5 * 60)
        await _settle(hass)
        total = [a + b for a, b in zip(_amps(hass, "a"), _amps(hass, "b"))]
        assert max(total) <= 24, (cron, total)
    assert _amps(hass, "b") == [16, 16, 16]
    await _unload(hass, car_a, car_b, site)


async def test_unloading_the_site_releases_the_limit(hass: HomeAssistant) -> None:
    site, car_a, car_b = await _setup_two_cars(hass)
    await hass.config_entries.async_unload(site.entry_id)
    await _settle(hass)
    await hass.services.async_call(DOMAIN, "enforce_planning", {"config_entry_id": car_b.entry_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _amps(hass, "b") == [16, 16, 16]
    await _unload(hass, car_a, car_b)


def _circuit(hass: HomeAssistant, prefix: str) -> tuple[list[int], list[str] | None]:
    attributes = hass.states.get(f"sensor.cjp_{prefix}_charger_action").attributes
    amps = [attributes[f"circuit_phase_{i}_amps"] for i in (1, 2, 3)]
    # the blueprint triggers on the decision: it must carry the circuit sum too
    assert f"circuit {'/'.join(map(str, amps))} A" in attributes["decision"]
    return amps, attributes.get("circuit_chargers")


async def test_cars_on_one_circuit_publish_the_sum(hass: HomeAssistant) -> None:
    """Two chargers behind one circuit (Easee master): both cars publish the sum, so the master's
    automation sets the circuit for both (16 A + 8 A per phase)."""
    easee_a = register_charger(hass, CHARGER_STATUS, "Easee A")
    easee_b = register_charger(hass, CHARGER_B_STATUS, "Easee B")
    site, car_a, car_b = await _setup_two_cars(hass, circuits=[[easee_a, easee_b]])
    assert _amps(hass, "a") == [16, 16, 16]
    assert _amps(hass, "b") == [8, 8, 8]
    assert _circuit(hass, "a") == ([24, 24, 24], ["a", "b"])
    assert _circuit(hass, "b") == ([24, 24, 24], ["a", "b"])
    cars = hass.states.get("sensor.cjp_site_available_l1").attributes["cars"]
    assert [car["circuit"] for car in cars] == [["a", "b"], ["a", "b"]]
    await _unload(hass, car_a, car_b, site)


async def test_without_a_circuit_each_car_keeps_its_own_limit(hass: HomeAssistant) -> None:
    site, car_a, car_b = await _setup_two_cars(hass)
    assert _circuit(hass, "a") == ([16, 16, 16], None)
    assert _circuit(hass, "b") == ([8, 8, 8], None)
    await _unload(hass, car_a, car_b, site)


# --------------------------------------------------------------------------- #
# CJPSite logic with stand-in cars (deterministic, no worker jobs)
# --------------------------------------------------------------------------- #


class FakeCar:
    def __init__(self, entry_id: str, priority: int, phases: int = 3) -> None:
        self.entry = SimpleNamespace(entry_id=entry_id)
        self.prefix = entry_id
        self.site_priority = priority
        self.site_phase_order = "L1-L2-L3"
        self.config = {"charger": {"charging_phases": phases, "entity_ids": {}}}
        self.attributes: dict = {}
        self.signal = f"test_site_car_{entry_id}"
        self.recalculated = 0

    def async_site_recalculate(self) -> None:
        self.recalculated += 1


@pytest.fixture
async def fake_site(hass: HomeAssistant):
    site = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1))
    cars = {"a": FakeCar("a", 1), "b": FakeCar("b", 2)}
    for car in cars.values():
        site.register(car)
    yield site, cars
    await site.async_stop()


async def test_site_order_by_priority_then_urgency(fake_site) -> None:
    site, cars = fake_site
    assert site.order() == ["a", "b"]
    site.update_from_result(cars["b"], {"reservation": {}, "request": [8, 8, 8], "urgent": True})
    assert site.order() == ["b", "a"]
    assert site.behind("b") == ["a"]


async def test_site_signals_when_initial_calculation_is_ready(fake_site) -> None:
    site, _cars = fake_site
    waiter = asyncio.create_task(site.async_wait_started())
    await asyncio.sleep(0)
    assert not waiter.done()

    await site.async_start()
    await waiter
    assert waiter.done()


async def test_site_capacity_for_the_car_behind(fake_site) -> None:
    site, cars = fake_site
    site.update_from_result(cars["a"], {"reservation": {}, "request": [16, 16, 16], "urgent": False})
    now = dt_util.now().replace(tzinfo=None)
    assert site.snapshot_for(cars["b"], now)["live"] == [8.0, 8.0, 8.0]
    assert site.snapshot_for(cars["a"], now)["live"] == [24.0, 24.0, 24.0]


async def test_changed_plan_recalculates_the_cars_behind(fake_site) -> None:
    site, cars = fake_site
    hour = dt_util.now().replace(tzinfo=None, minute=0, second=0, microsecond=0)
    site.update_from_result(cars["a"], {"reservation": {hour: [16, 16, 16]}, "request": [0, 0, 0], "urgent": False})
    assert "b" in site._pending


async def test_car_turning_urgent_is_recalculated_itself(fake_site) -> None:
    site, cars = fake_site
    site.update_from_result(cars["b"], {"reservation": {}, "request": [8, 8, 8], "urgent": True})
    assert "b" in site._pending


async def test_changed_request_recalculates_the_cars_behind(fake_site) -> None:
    site, cars = fake_site
    site.update_from_result(cars["a"], {"reservation": {}, "request": [8, 8, 8], "urgent": False})
    for cancel in site._pending.values():
        cancel()
    site._pending.clear()
    site.update_from_result(cars["a"], {"reservation": {}, "request": [16, 16, 16], "urgent": False})
    assert "b" in site._pending


async def test_unchanged_result_recalculates_nobody(fake_site) -> None:
    """Recalculations stop when the results stop changing (no ping-pong between the cars)."""
    site, cars = fake_site
    result = {"reservation": {}, "request": [16, 16, 16], "urgent": False}
    site.update_from_result(cars["a"], result)
    for cancel in site._pending.values():
        cancel()
    site._pending.clear()
    site.update_from_result(cars["a"], dict(result))
    assert site._pending == {}


# --------------------------------------------------------------------------- #
# Shared circuit (chargers behind one circuit limit, e.g. an Easee master)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("circuits", "own", "selected", "expected"),
    [
        # the car's circuit is replaced by the new choice
        ([["a", "b"], ["c", "d"]], {"a"}, ["a", "e"], [["c", "d"], ["a", "e"]]),
        # a charger moved from another circuit leaves it (a charger is on one circuit only)
        ([["c", "d", "e"]], {"a"}, ["a", "c"], [["d", "e"], ["a", "c"]]),
        ([["c", "d"]], {"a"}, ["a", "c"], [["a", "c"]]),
        # cleared from one car: gone for every car of it
        ([["a", "b"]], {"b"}, [], []),
        # a circuit needs two chargers
        ([], {"a"}, ["a"], []),
        # a car without a circuit changes nothing
        ([["c", "d"]], {"a"}, [], [["c", "d"]]),
    ],
)
def test_update_circuits(circuits, own, selected, expected) -> None:
    assert update_circuits(circuits, own, selected) == expected


@pytest.fixture
async def circuit_site(hass: HomeAssistant):
    """Cars a and b share a circuit, c has its own charger."""
    site = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **{CONF_CIRCUITS: [["easee_a", "easee_b"]]}))
    cars = {"a": FakeCar("a", 1), "b": FakeCar("b", 2), "c": FakeCar("c", 3)}
    for car in cars.values():
        site.register(car)
    site.devices.update({"a": {"easee_a"}, "b": {"easee_b", "shelly_b"}, "c": {"easee_c"}})
    yield site, cars
    await site.async_stop()


def _result(request: list) -> dict:
    return {"reservation": {}, "request": request, "urgent": False}


async def test_circuit_sum_reaches_every_car_of_it(hass: HomeAssistant, circuit_site) -> None:
    site, cars = circuit_site
    signals = []

    @callback
    def _signal(key) -> None:
        signals.append(key)

    async_dispatcher_connect(hass, cars["a"].signal, _signal)
    cars["b"].site_phase_order = "L2-L3-L1"  # b's L1 is grid L2
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    site.update_from_result(cars["b"], _result([6, 0, 0]))
    # car a was patched when b changed, so the master's automation sees it at once
    assert signals == ["charger_action"]
    assert [cars["a"].attributes["charger_action"][f"circuit_phase_{i}_amps"] for i in (1, 2, 3)] == [16, 22, 16]
    assert cars["a"].attributes["charger_action"]["circuit_chargers"] == ["a", "b"]
    assert site.circuit_patch(cars["b"]) is True
    assert cars["b"].attributes["charger_action"]["circuit_phase_2_amps"] == 22
    assert site.circuit_patch(cars["b"]) is False  # unchanged
    assert site.circuit_members("c") == []
    assert site.circuit_patch(cars["c"]) is False
    assert cars["c"].attributes == {}


async def test_circuit_is_zero_when_nobody_on_it_charges(circuit_site) -> None:
    site, cars = circuit_site
    site.update_from_result(cars["a"], _result([0, 0, 0]))
    site.update_from_result(cars["b"], _result([0, 0, 0]))
    site.update_from_result(cars["c"], _result([16, 16, 16]))
    site.circuit_patch(cars["a"])
    assert [cars["a"].attributes["charger_action"][f"circuit_phase_{i}_amps"] for i in (1, 2, 3)] == [0, 0, 0]


async def test_circuit_with_one_loaded_car_is_no_circuit(circuit_site) -> None:
    site, cars = circuit_site
    site.unregister("b")
    assert site.circuit_members("a") == []


async def test_first_charging_car_on_the_circuit_decides_the_phases(circuit_site) -> None:
    site, cars = circuit_site
    now = dt_util.now().replace(tzinfo=None)
    assert site.snapshot_for(cars["b"], now)["circuit_phases"] is None  # nobody ahead charges
    site.update_from_result(cars["a"], _result([6, 0, 0]))
    assert site.snapshot_for(cars["b"], now)["circuit_phases"] == 1
    assert site.circuit_phases("a") is None  # nobody ahead of a
    assert site.circuit_phases("c") is None  # not on the circuit
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert site.circuit_phases("b") == 3
    # a charger that only charges on one phase is not switched by the circuit
    cars["a"].config["charger"]["charging_phases"] = 1
    site.update_from_result(cars["a"], _result([16, 0, 0]))
    assert site.circuit_phases("b") is None


def _clamp_engine(live: list, circuit_phases: int | None, charging_phases: int = 3) -> CJPEngine:
    options = {
        **const.default_options(),
        "charging_phases": float(charging_phases),
        "charging_max_amp": 16.0,
        "power_voltage": 230.0,
        "charging_single_phase_min_amp": 6.0,
        "charging_single_phase_max_amp": 16.0,
        "charging_three_phase_min_amp": 6.0,
    }
    engine = CJPEngine(const.build_config(options), "cjp_test", {})
    engine._snap = SimpleNamespace(site={"live": live, "circuit_phases": circuit_phases})
    return engine


@pytest.mark.parametrize(
    ("requested", "circuit_phases", "charging_phases", "expected"),
    [
        ([16, 16, 16], None, 3, [16, 16, 16]),
        # the car ahead charges on 1 phase: only 1 phase for this car
        ([16, 16, 16], 1, 3, [16, 0, 0]),
        # 1 phase solar while the car ahead charges on 3 phases: 3 x 6 A is more than asked, so wait
        ([6, 0, 0], 3, 3, [0, 0, 0]),
        ([10, 10, 10], 3, 3, [10, 10, 10]),
        # a 1 phase charger is not switched by the circuit
        ([16, 0, 0], 3, 1, [16, 0, 0]),
    ],
)
def test_site_clamp_keeps_to_the_circuit_phases(requested, circuit_phases, charging_phases, expected) -> None:
    engine = _clamp_engine([24.0, 24.0, 24.0], circuit_phases, charging_phases)
    assert engine.site_clamp(requested) == expected


@pytest.mark.parametrize(("meta_unit", "expected"), [("W", 1150 / 230), ("kW", 1150 * 1000 / 230), ("A", 1150), (None, None)])
async def test_house_forecast_uses_the_recorded_unit(hass: HomeAssistant, meta_unit, expected) -> None:
    """Right after a restart the phase sensors have no state yet: the unit comes from the statistics.
    Watts read as amps (the bug) left no current for the cars."""
    from unittest.mock import patch  # noqa: PLC0415

    phases = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
    site = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **phases))
    site.house_forecast = {3: [1.0, 1.0, 1.0]}
    hass.config.components.add("recorder")
    start = dt_util.utcnow().replace(hour=3, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)
    stats = {entity_id: [{"start": start, "mean": 1150.0}] for entity_id in phases.values()}
    metadata = {entity_id: (1, {"unit_of_measurement": meta_unit}) for entity_id in phases.values()} if meta_unit else {}
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", return_value=stats),
        patch("homeassistant.components.recorder.statistics.get_metadata", return_value=metadata),
    ):
        await site._async_refresh_forecast()
    hour = dt_util.as_local(start).hour
    if expected is None:
        # unknown unit: the previous forecast is kept
        assert site.house_forecast == {3: [1.0, 1.0, 1.0]}
    else:
        assert site.house_forecast[hour] == [round(expected, 2)] * 3
