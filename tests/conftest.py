"""Shared fixtures: a simulated charger, car and price sensor plus loaded Cable Juice Planner entries."""
from __future__ import annotations

import datetime
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.cable_juice_planner.const import CONF_ENTITY_PREFIX, CONF_SITE, DOMAIN, SITE_DEFAULTS, default_options

PREFIX = "test"
OBJECT_PREFIX = f"cjp_{PREFIX}"

# charger
CHARGER_STATUS = "sensor.charger_status"
CHARGER_POWER = "sensor.charger_power"
CHARGER_KWH = "sensor.charger_session_energy"
CHARGER_LIFETIME_KWH = "sensor.charger_lifetime_energy"
# car
EV_LEVEL = "sensor.ev_battery_level"
EV_RANGE = "sensor.ev_range"
EV_ODOMETER = "sensor.ev_odometer"
EV_PORT = "binary_sensor.ev_charge_port"
EV_LIMIT = "number.ev_charge_limit"
EV_LOCATION = "device_tracker.ev_location"
# home
CONSUMPTION = "sensor.house_consumption"
PRICE = "sensor.energi_data_service"

CHARGER_OPTIONS: dict[str, Any] = {
    "status_entity_id": CHARGER_STATUS,
    "power_consumtion_entity_id": CHARGER_POWER,
    "kwh_meter_entity_id": CHARGER_KWH,
    "lifetime_kwh_meter_entity_id": CHARGER_LIFETIME_KWH,
    "power_voltage": 230.0,
    "charging_phases": 3.0,
    "charging_max_amp": 16.0,
    "charging_loss": -0.05,
}
EV_OPTIONS: dict[str, Any] = {
    "usable_battery_level_entity_id": EV_LEVEL,
    "estimated_battery_range_entity_id": EV_RANGE,
    "odometer_entity_id": EV_ODOMETER,
    "charge_port_door_entity_id": EV_PORT,
    "charging_limit_entity_id": EV_LIMIT,
    "location_entity_id": EV_LOCATION,
    "battery_size": 75.0,
}

# Options as the config flow stores them (charger + car, energidataservice, no solar)
MOCK_OPTIONS: dict[str, Any] = {
    **default_options(),
    "language": "da",
    **CHARGER_OPTIONS,
    **EV_OPTIONS,
    "power_consumption_entity_id": CONSUMPTION,
    "energidataservice_buy_entity_id": PRICE,
    "dashboard": False,
}
# Charger only: the battery level is entered in number.cjp_<prefix>_battery_level
MOCK_OPTIONS_NO_EV: dict[str, Any] = {**MOCK_OPTIONS, **{key: "" for key in EV_OPTIONS if key.endswith("_entity_id")}}


def price_rows(day: datetime.date, base: float) -> list[dict[str, Any]]:
    """24 hourly Energi Data Service rows: cheap at night, expensive at 17-20."""
    tz = dt_util.get_default_time_zone()
    rows = []
    for hour in range(24):
        price = base + (1.5 if 17 <= hour <= 20 else 0.6 if 7 <= hour <= 16 else 0.0) + hour / 100
        start = datetime.datetime.combine(day, datetime.time(hour), tzinfo=tz)
        rows.append({"hour": start.isoformat(), "price": round(price, 3)})
    return rows


def set_price_state(hass: HomeAssistant, entity_id: str = PRICE) -> None:
    today = dt_util.now().date()
    tomorrow = today + datetime.timedelta(days=1)
    hass.states.async_set(
        entity_id,
        "1.2",
        {
            "unit_of_measurement": "kr/kWh",
            "raw_today": price_rows(today, 1.0),
            "raw_tomorrow": price_rows(tomorrow, 0.8),
            "tomorrow_valid": True,
            "forecast": [
                *price_rows(tomorrow + datetime.timedelta(days=1), 0.9),
                *price_rows(tomorrow + datetime.timedelta(days=2), 1.1),
            ],
        },
    )


def set_source_states(
    hass: HomeAssistant,
    *,
    charger_status: str = "disconnected",
    ev_level: float = 40,
    port: str = "off",
    ev: bool = True,
) -> None:
    """Create the entities a real installation gets from the charger/car/price integrations."""
    hass.states.async_set(CHARGER_STATUS, charger_status)
    hass.states.async_set(CHARGER_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(CHARGER_KWH, "0.0", {"unit_of_measurement": "kWh", "device_class": "energy"})
    hass.states.async_set(CHARGER_LIFETIME_KWH, "1234.5", {"unit_of_measurement": "kWh", "device_class": "energy"})
    hass.states.async_set(CONSUMPTION, "650", {"unit_of_measurement": "W", "device_class": "power"})
    if ev:
        hass.states.async_set(EV_LEVEL, str(ev_level), {"unit_of_measurement": "%", "device_class": "battery"})
        hass.states.async_set(EV_RANGE, str(ev_level * 4.5), {"unit_of_measurement": "km", "device_class": "distance"})
        hass.states.async_set(EV_ODOMETER, "25000", {"unit_of_measurement": "km", "device_class": "distance"})
        hass.states.async_set(EV_PORT, port)
        hass.states.async_set(EV_LIMIT, "80", {"unit_of_measurement": "%", "min": 50, "max": 100})
        hass.states.async_set(EV_LOCATION, "home")
    set_price_state(hass)


def form_defaults(result) -> dict[str, Any]:
    """The values a user submits by just pressing the button (defaults + suggested values)."""
    values = {}
    for key in result["data_schema"].schema:
        if (suggested := (key.description or {}).get("suggested_value")) not in (None, ""):
            values[str(key)] = suggested
        elif callable(key.default):
            values[str(key)] = key.default()
    return values


async def async_advance(hass: HomeAssistant, seconds: float) -> None:
    """Run the timers due within <seconds> (debounce, retry sleeps) without freezing the clock.

    The freezer fixture would turn datetime.now() into a FakeDatetime subclass, which the
    planner's strict type checks (in_between, ported unchanged from PyScript) reject.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=seconds))
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow custom_components/cable_juice_planner to be loaded in every test."""
    yield


def make_entry(prefix: str = PREFIX, options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title=f"Cable Juice Planner ({prefix})",
        data={CONF_ENTITY_PREFIX: prefix, "legacy_prefix": None, "initial_settings": {}},
        options=dict(options or MOCK_OPTIONS),
    )


def make_site_entry(**options: Any) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, version=2, title="site", data={CONF_SITE: True}, options={**SITE_DEFAULTS, **options})


def register_charger(hass: HomeAssistant, entity_id: str, name: str) -> str:
    """A charger device (like an Easee charger) owning the entity; returns the device id.

    Register before the entity gets a state, else the registry picks another entity id.
    """
    source = MockConfigEntry(domain="easee", title=name)
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=source.entry_id, identifiers={("easee", name)}, name=name)
    domain, object_id = entity_id.split(".")
    entry = er.async_get(hass).async_get_or_create(domain, "easee", object_id, suggested_object_id=object_id, config_entry=source, device_id=device.id)
    assert entry.entity_id == entity_id
    return device.id


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    return make_entry()


async def async_setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def async_unload_and_join(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unload and wait for the worker thread (the HA test harness fails on lingering threads)."""
    coordinator = entry.runtime_data
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    if hasattr(coordinator, "_executor"):
        await hass.async_add_executor_job(coordinator._executor.shutdown, True)


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, mock_entry: MockConfigEntry) -> AsyncGenerator[MockConfigEntry]:
    """Cable Juice Planner set up against the simulated charger and car, startup finished."""
    set_source_states(hass)
    await async_setup(hass, mock_entry)
    yield mock_entry
    if mock_entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, mock_entry)
