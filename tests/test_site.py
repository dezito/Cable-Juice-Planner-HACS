"""Load balancing: several cars behind one main fuse (site config entry)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cable_juice_planner.const import DOMAIN, SITE_SENSORS
from custom_components.cable_juice_planner.site import CJPSite

from .conftest import MOCK_OPTIONS, async_advance, async_setup, async_unload_and_join, make_entry, make_site_entry, set_source_states

EV_B_LEVEL = "sensor.ev_b_battery_level"


def _amps(hass: HomeAssistant, prefix: str) -> list[int]:
    return [int(hass.states.get(f"sensor.cjp_{prefix}_charger_phase_{i}_amps").state) for i in (1, 2, 3)]


async def _settle(hass: HomeAssistant) -> None:
    """Let the site recalculate the cars behind a changed plan (15 s delay) until nothing changes."""
    for _ in range(4):
        await async_advance(hass, 16)
        await hass.async_block_till_done(wait_background_tasks=True)


async def _setup_two_cars(hass: HomeAssistant, b_level: float = 60) -> tuple[MockConfigEntry, MockConfigEntry, MockConfigEntry]:
    set_source_states(hass)
    hass.states.async_set(EV_B_LEVEL, str(b_level), {"unit_of_measurement": "%", "device_class": "battery"})
    site = make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1)
    await async_setup(hass, site)
    car_a = make_entry("a", {**MOCK_OPTIONS, "site_priority": 1})
    car_b = make_entry("b", {**MOCK_OPTIONS, "site_priority": 2, "usable_battery_level_entity_id": EV_B_LEVEL})
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


# --------------------------------------------------------------------------- #
# CJPSite logic with stand-in cars (deterministic, no worker jobs)
# --------------------------------------------------------------------------- #


class FakeCar:
    def __init__(self, entry_id: str, priority: int) -> None:
        self.entry = SimpleNamespace(entry_id=entry_id)
        self.prefix = entry_id
        self.site_priority = priority
        self.site_phase_order = "L1-L2-L3"
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
