"""Cable Juice Planner's algorithm without Home Assistant: the worker jobs on a hand made Snapshot.

The scenarios set the time of day, the car's battery level, the charger status and the
dashboard settings directly, so a whole day can be checked in milliseconds.
"""
from __future__ import annotations

import datetime
import json
import zoneinfo
from pathlib import Path

import pytest
from astral import LocationInfo
from astral.location import Location

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.codec import decode
from custom_components.cable_juice_planner.engine import CJPEngine, Snapshot

TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")
ASTRAL = (Location(LocationInfo("", "", "Europe/Copenhagen", 57.02, 8.68)), 0)
DAY = datetime.date(2026, 10, 22)  # a Thursday
EXPENSIVE_HOURS = set(range(17, 21))

OPTIONS = {
    **const.default_options(),
    "status_entity_id": "sensor.charger_status",
    "power_consumtion_entity_id": "sensor.charger_power",
    "kwh_meter_entity_id": "sensor.charger_kwh",
    "lifetime_kwh_meter_entity_id": "sensor.charger_lifetime_kwh",
    "usable_battery_level_entity_id": "sensor.ev_battery_level",
    "estimated_battery_range_entity_id": "sensor.ev_range",
    "odometer_entity_id": "sensor.ev_odometer",
    "charge_port_door_entity_id": "binary_sensor.ev_charge_port",
    "charging_limit_entity_id": "number.ev_charge_limit",
    "location_entity_id": "device_tracker.ev",
    "power_consumption_entity_id": "sensor.consumption",
    "energidataservice_buy_entity_id": "sensor.price",
    "power_voltage": 230.0,
    "charging_phases": 3.0,
    "charging_max_amp": 16.0,
    "battery_size": 75.0,
}
MAX_WATT = 230 * 3 * 16


def _texts() -> dict[str, str]:
    path = Path(__file__).parents[1] / "custom_components" / "cable_juice_planner" / "texts" / "da.json"
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    flatten(json.loads(path.read_text(encoding="utf-8")))
    return texts


TEXTS = _texts()


def default_settings() -> dict:
    """The values of the setting entities right after the first setup (car configured, no solar)."""
    settings: dict = {key: info["default"] for key, info in const.SWITCH_SETTINGS.items() if info.get("requires") is None}
    settings.update(
        {key: float(info["default"]) for key, info in const.NUMBER_SETTINGS.items() if info.get("requires") in (None, "ev")}
    )
    settings.update({key: info["default"] for key, info in const.SELECT_SETTINGS.items()})
    settings.update({key: datetime.time.fromisoformat(info["default"]) for key, info in const.TIME_SETTINGS.items()})
    settings.update(dict.fromkeys(const.DATETIME_SETTINGS))
    return settings


def _price_rows(day: datetime.date, base: float) -> list[dict]:
    rows = []
    for hour in range(24):
        price = base + (1.5 if hour in EXPENSIVE_HOURS else 0.6 if 7 <= hour <= 16 else 0.0) + hour / 100
        rows.append({"hour": datetime.datetime.combine(day, datetime.time(hour), tzinfo=TZ).isoformat(), "price": round(price, 3)})
    return rows


def snapshot(
    now: datetime.datetime,
    *,
    level: float = 40,
    status: str = "disconnected",
    port: str = "off",
    settings: dict | None = None,
    site: dict | None = None,
) -> Snapshot:
    tomorrow = now.date() + datetime.timedelta(days=1)
    price = {
        "raw_today": _price_rows(now.date(), 1.0),
        "raw_tomorrow": _price_rows(tomorrow, 0.8),
        "tomorrow_valid": True,
        "forecast": _price_rows(tomorrow + datetime.timedelta(days=1), 0.9) + _price_rows(tomorrow + datetime.timedelta(days=2), 1.1),
    }
    states = {
        "sensor.charger_status": status,
        "sensor.charger_power": "0",
        "sensor.charger_kwh": "0",
        "sensor.charger_lifetime_kwh": "1000",
        "sensor.ev_battery_level": str(level),
        "sensor.ev_range": str(level * 4.5),
        "sensor.ev_odometer": "25000",
        "binary_sensor.ev_charge_port": port,
        "number.ev_charge_limit": "80",
        "device_tracker.ev": "home",
        "sensor.consumption": "650",
        "sensor.price": "1.2",
    }
    return Snapshot(
        now=now,
        tz=TZ,
        settings={**default_settings(), **(settings or {})},
        states=states,
        attributes={"sensor.price": price},
        units={"sensor.charger_power": "W", "sensor.consumption": "W"},
        price_data={"attributes": {"sensor.price": price}, "values": {}},
        astral=ASTRAL,
        site=site,
    )


def run(hour: int, **kwargs) -> tuple[CJPEngine, object]:
    """Start an engine and plan once at <hour>:30 on DAY."""
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    started = engine.run_job(snapshot(now, **kwargs), engine.startup, {})
    assert started.error is None, started.error
    result = engine.run_job(snapshot(now, **kwargs), engine.charge_if_needed, True)
    assert result.error is None, result.error
    return engine, result


def charge_hours(engine: CJPEngine) -> list[datetime.datetime]:
    return sorted(ts for ts in engine.charge_hours if isinstance(ts, datetime.datetime))


PLUGGED_IN = {"status": "awaiting_start", "port": "on"}


def test_no_charging_mode_charges_at_max() -> None:
    """ev.py's fallback: every charging mode off -> charge like a normal charger."""
    engine, result = run(3)
    assert engine.charger_action == "charge"
    assert result.states["charging_watts"] == MAX_WATT
    assert result.states["current_charging_rule"].startswith("⛔")
    assert result.attributes["charger_action"]["mode"] == "normal"


def test_unplugged_stops() -> None:
    engine, result = run(3, settings={"fill_up": True})
    assert engine.charger_action == "stop"
    assert result.states["charger_phase_1_amps"] == 0


@pytest.mark.parametrize("hour", [3, 12, 18])
def test_fill_up_only_charges_in_planned_hours(hour: int) -> None:
    engine, result = run(hour, settings={"fill_up": True}, **PLUGGED_IN)
    hours = charge_hours(engine)
    assert hours, "fill up from 40 % should plan charging hours"
    assert not [ts for ts in hours if ts.hour in EXPENSIVE_HOURS]
    in_plan = any(ts.date() == DAY and ts.hour == hour for ts in hours)
    assert (engine.charger_action == "charge") is in_plan


def test_fill_up_plans_the_cheapest_night() -> None:
    """Tomorrow's night (base 0.8) is cheaper than the rest of tonight (base 1.0)."""
    engine, _ = run(3, settings={"fill_up": True}, **PLUGGED_IN)
    first = charge_hours(engine)[0]
    assert first.date() == DAY + datetime.timedelta(days=1)
    assert first.hour < 7


def test_low_battery_charges_unless_expensive() -> None:
    settings = {"fill_up": True, "forced_charging_daily_battery_level": True}
    engine, result = run(12, level=10, settings=settings, **PLUGGED_IN)
    assert engine.charger_action == "charge"
    assert result.states["charging_limit"] == pytest.approx(90.0)

    engine, _ = run(18, level=10, settings=settings, **PLUGGED_IN)
    assert engine.charger_action == "stop"


def test_manual_charging() -> None:
    engine, result = run(12, settings={"allow_manual_charging_now": True}, **PLUGGED_IN)
    assert engine.charger_action == "charge"
    assert result.states["charging_watts"] == MAX_WATT


def test_deactivated_does_not_control_the_charger() -> None:
    engine, result = run(3, settings={"fill_up": True, "deactivate_script": True}, **PLUGGED_IN)
    assert result.attributes["charger_action"]["mode"] == "deactivated"
    assert result.states["current_charging_rule"].startswith("⛔")


def test_workday_departure_plans_before_departure() -> None:
    """Workplan: Friday 07:00 departure with 150 km needs charging before Friday morning."""
    settings = {
        "workplan_charging": True,
        "workday_friday": True,
        "workday_distance_needed_friday": 150.0,
    }
    engine, _ = run(12, level=20, settings=settings, **PLUGGED_IN)
    friday_morning = datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(7))
    assert [ts for ts in charge_hours(engine) if ts < friday_morning]


def test_site_live_capacity_limits_amps() -> None:
    """Load balancing: only 10 A per phase left on the grid connection."""
    site = {"live": [10.0, 10.0, 10.0], "hour_capacity": {}, "urgent_hours": 12.0}
    engine, result = run(12, settings={"allow_manual_charging_now": True}, site=site, **PLUGGED_IN)
    assert engine.charger_action == "charge"
    assert [result.states[f"charger_phase_{i}_amps"] for i in (1, 2, 3)] == [10, 10, 10]
    assert result.site["request"] == [10, 10, 10]


def test_site_without_capacity_waits() -> None:
    site = {"live": [0.0, 0.0, 0.0], "hour_capacity": {}, "urgent_hours": 12.0}
    _, result = run(12, settings={"allow_manual_charging_now": True}, site=site, **PLUGGED_IN)
    assert [result.states[f"charger_phase_{i}_amps"] for i in (1, 2, 3)] == [0, 0, 0]


def test_site_urgent_when_under_daily_minimum() -> None:
    site = {"live": [16.0, 16.0, 16.0], "hour_capacity": {}, "urgent_hours": 12.0}
    _, result = run(12, level=10, settings={"fill_up": True}, site=site, **PLUGGED_IN)
    assert result.site["urgent"] is True
    _, result = run(12, level=60, settings={"fill_up": True}, site=site, **PLUGGED_IN)
    assert result.site["urgent"] is False


def test_hour_end_records_power_values_without_solar() -> None:
    """ev.py only recorded them with solar + Solcast; now the house consumption entity is enough."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 59))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {}).error is None
    hour_end = snapshot(now)
    # the house used 650 W the whole last two hours (recorder buffer: timestamp, state)
    two_hours_ago = (now - datetime.timedelta(hours=2)).replace(tzinfo=TZ).timestamp()
    hour_end.buffers = {"sensor.consumption": [(two_hours_ago, "650")]}
    result = engine.run_job(hour_end, engine.hour_end)
    assert result.error is None, result.error
    assert "power_values" in result.db_saves
    hour = decode(result.db_saves["power_values"])[12]
    assert hour["power_consumption"][0] == [now, 650.0]
    assert hour["solar_production"][0][1] == 0.0


def test_hour_end_without_consumption_entity_records_nothing() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 59))
    engine = CJPEngine(const.build_config({**OPTIONS, "power_consumption_entity_id": ""}), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {}).error is None
    result = engine.run_job(snapshot(now), engine.hour_end)
    assert result.error is None, result.error
    assert "power_values" not in result.db_saves


def test_existing_power_values_are_loaded_without_solar() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    stamp = now - datetime.timedelta(days=1)
    database = {"version": 1.0, 12: {"power_consumption": [[stamp, 900.0]]}}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {"power_values": database}).error is None
    assert engine.power_values_db[12]["power_consumption"] == [[stamp, 900.0]]


def test_startup_saves_every_database() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(now), engine.startup, {})
    assert set(result.db_saves) == set(const.DB_NAMES)
    assert decode(result.db_saves["runtime"])["charging_loss"] == OPTIONS["charging_loss"]
