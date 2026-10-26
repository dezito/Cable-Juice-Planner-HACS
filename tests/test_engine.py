"""Cable Juice Planner's algorithm without Home Assistant: the worker jobs on a hand made Snapshot.

The scenarios set the time of day, the car's battery level, the charger status and the
dashboard settings directly, so a whole day can be checked in milliseconds.
"""
from __future__ import annotations

import datetime
import json
import zoneinfo
from copy import deepcopy
from pathlib import Path

import pytest
from astral import Observer

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.codec import decode
from custom_components.cable_juice_planner.engine import CJPEngine, Snapshot

TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")
ASTRAL = Observer(57.02, 8.68, 0)
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


def run(hour: int, databases: dict | None = None, **kwargs) -> tuple[CJPEngine, object]:
    """Start an engine and plan once at <hour>:30 on DAY."""
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    started = engine.run_job(snapshot(now, **kwargs), engine.startup, databases or {})
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


def test_workday_departure_plans_before_departure() -> None:
    """Workplan: Friday 07:00 departure with 150 km needs charging before Friday morning."""
    settings = {
        "workplan_charging": True,
        "workday_friday": True,
        "workday_distance_needed_friday": 150.0,
    }
    engine, result = run(12, level=20, settings=settings, **PLUGGED_IN)
    friday_morning = datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(7))
    assert [ts for ts in charge_hours(engine) if ts < friday_morning]
    # the dashboard chart: the 150 km on Friday, each planned charging with its end
    plan = result.attributes["current_charging_rule"]
    friday = [d for d in plan["distance_forecast"] if d["time"].startswith(str(DAY + datetime.timedelta(days=1)))]
    assert [d["km"] for d in friday] == [150]
    assert all(p["end"] > p["time"] for p in plan["charge_plan"])


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


def test_startup_migrates_price_history_before_provider_is_available(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(now), engine.startup, {"kwh_avg_prices": {}})
    assert result.error is None, result.error
    assert "Can't append KWH avg prices" not in caplog.text


def _loss_job(engine: CJPEngine, hour: int, source: str, *args, level: float, kwh: float, status: str = "charging"):
    now = datetime.datetime.combine(DAY, datetime.time(hour, 0))
    snap = snapshot(now, level=level, status=status, port="on", settings={"calculate_charging_loss": True})
    snap.states["sensor.charger_kwh"] = str(kwh)
    result = engine.run_job(snap, engine.calculate_charging_loss, source, *args)
    assert result.error is None, result.error
    return result


def _loss_engine() -> CJPEngine:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(1, 0))), engine.startup, {}).error is None
    return engine


@pytest.mark.parametrize("final_source", ["kwh_meter", "timeout"])
def test_charging_loss_measurement(final_source: str) -> None:
    """20% -> 80% of 75 kWh = 45 kWh in the car, 50 kWh from the charger: 11.1% loss."""
    engine = _loss_engine()
    _loss_job(engine, 1, "switch", True, level=20, kwh=100)
    assert _loss_job(engine, 6, "status", "completed", "charging", level=80, kwh=148, status="completed").value == "wait_for_meter"
    # the slow meter reports the rest later (or the coordinator's timeout reads it)
    args = ("150", "148") if final_source == "kwh_meter" else ()
    result = _loss_job(engine, 6, final_source, *args, level=80, kwh=150, status="completed")
    assert result.config_updates["charging_loss"] == pytest.approx(-0.1111, abs=1e-4)
    assert result.settings["calculate_charging_loss"] is False
    # the stored loss converts the car's 45 kWh back into the 50 kWh from the charger
    assert engine.percentage_to_kwh(60, include_charging_loss=True) == pytest.approx(50, abs=0.01)


def test_charging_loss_resumed_charging_keeps_measuring() -> None:
    engine = _loss_engine()
    _loss_job(engine, 1, "switch", True, level=20, kwh=100)
    _loss_job(engine, 5, "status", "completed", "charging", level=79, kwh=147, status="completed")
    _loss_job(engine, 5, "status", "charging", "completed", level=79, kwh=147)
    # a meter update while charging again does not finish the measurement
    assert "charging_loss" not in _loss_job(engine, 5, "kwh_meter", "148", "147", level=80, kwh=148).config_updates
    assert engine.charging_loss_car_begin_battery_level == 20


def test_charging_loss_needs_enough_charged() -> None:
    engine = _loss_engine()
    _loss_job(engine, 1, "switch", True, level=60, kwh=100)
    _loss_job(engine, 3, "status", "completed", "charging", level=80, kwh=117, status="completed")
    result = _loss_job(engine, 3, "timeout", level=80, kwh=117, status="completed")
    assert "charging_loss" not in result.config_updates
    assert result.settings["calculate_charging_loss"] is False
    assert engine.charging_loss_charger_begin_kwh == 0.0


def test_charging_loss_charges_to_the_car_limit() -> None:
    """A car limit above the recommended max is published, not the daily range level."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(now, settings={"calculate_charging_loss": True}, **PLUGGED_IN)
    snap.states["number.ev_charge_limit"] = "100"
    assert engine.run_job(snap, engine.startup, {}).error is None
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None, result.error
    assert engine.charger_action == "charge"
    assert result.states["charging_limit"] == 100


def test_cheap_grid_charging_alone_charges_to_the_cheap_levels() -> None:
    """Only "charge on cheap power": cheap hours up to the very/ultra cheap level, nothing else."""
    settings = {"cheap_grid_charging": True, "very_cheap_grid_charging_max_battery_level": 70.0, "ultra_cheap_grid_charging_max_battery_level": 80.0}
    engine, result = run(12, settings=settings, **PLUGGED_IN)
    hours = charge_hours(engine)
    assert hours, "cheap hours should be planned from 40 %"
    assert not [ts for ts in hours if ts.hour in EXPENSIVE_HOURS]
    for ts in hours:
        hour = engine.charge_hours[ts]
        assert "under_min_avg_price" in hour or "half_min_avg_price" in hour, (ts, hour)
        assert "fill_up" not in hour
    # no hour is ultra cheap in the test prices: exactly up to the very cheap level, also
    # with the hours just after midnight (they once went uncounted and overcharged)
    assert 40 + engine.charge_hours["total_procent"] == pytest.approx(70, abs=0.5)
    assert not result.states["current_charging_rule"].startswith("⛔")


def test_cheap_rules_do_not_stick_to_later_hours() -> None:
    """The price rules of one hour used to leak into every later fill up hour."""
    engine, _ = run(3, settings={"fill_up": True}, **PLUGGED_IN)
    prices = sorted(engine.get_grid_prices().values())[: engine.config["database"]["kwh_avg_prices_db_data_to_save"]]
    very_cheap = round(sum(prices) / len(prices), 3)
    for ts in charge_hours(engine):
        hour = engine.charge_hours[ts]
        assert ("under_min_avg_price" in hour or "half_min_avg_price" in hour) == (round(hour["Price"], 3) <= very_cheap), (ts, hour)


def _distance_engine(daily_km: dict[int, float], weeks: int = 4) -> CJPEngine:
    """An engine with odometer readings: daily_km per weekday (0 = monday) for <weeks> weeks up to DAY."""
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    odometer = 20000.0
    first = DAY - datetime.timedelta(weeks=weeks)
    for offset in range((DAY - first).days + 1):
        date = first + datetime.timedelta(days=offset)
        odometer += daily_km.get(date.weekday(), 0.0) if offset else 0.0
        engine.daily_odometer[date.isoformat()] = odometer
    return engine


def _distance_run(engine: CJPEngine, settings: dict | None = None):
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings=settings), engine.update_learned_distances)
    assert result.error is None, result.error
    return result


def test_learned_distance_is_only_suggested_by_default() -> None:
    engine = _distance_engine({0: 42, 1: 42, 2: 42, 3: 42, 4: 120, 5: 10, 6: 0})
    result = _distance_run(engine)
    assert result.attributes["workday_distance_needed_monday"]["learned_distance"] == 45
    assert result.attributes["workday_distance_needed_friday"]["learned_distance"] == 120
    assert result.attributes["workday_distance_needed_sunday"]["learned_distance"] == 0
    # 75th percentile of all days as the buffered typical day
    assert result.attributes["typical_daily_distance"]["learned_distance"] == 45
    assert not result.settings


def test_learned_distance_sets_the_numbers_when_enabled() -> None:
    engine = _distance_engine({0: 42, 1: 42, 2: 42, 3: 42, 4: 120, 5: 10, 6: 0})
    result = _distance_run(engine, {"learn_driving_distance": True})
    assert result.settings["workday_distance_needed_friday"] == 120
    assert result.settings["typical_daily_distance"] == 45


def test_learned_distance_median_ignores_one_long_trip() -> None:
    engine = _distance_engine({0: 30})
    # one monday with a 400 km trip
    monday = min(date for date in engine.daily_odometer if datetime.date.fromisoformat(date).weekday() == 0 and date > (DAY - datetime.timedelta(weeks=4)).isoformat())
    for date in engine.daily_odometer:
        if date >= monday:
            engine.daily_odometer[date] += 370
    result = _distance_run(engine)
    assert result.attributes["workday_distance_needed_monday"]["learned_distance"] == 30


def test_learned_distance_needs_enough_days() -> None:
    engine = _distance_engine({0: 42}, weeks=1)
    result = _distance_run(engine, {"learn_driving_distance": True})
    assert result.attributes["workday_distance_needed_monday"]["learned_days"] == 1
    assert "workday_distance_needed_monday" not in result.settings
    assert "typical_daily_distance" not in result.settings


def test_daily_odometer_is_recorded_at_the_end_of_the_day() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(23, 59))), engine.hour_end)
    assert result.error is None, result.error
    assert engine.daily_odometer == {DAY.isoformat(): 25000.0}
    assert "runtime" in result.db_saves


def test_fill_up_fills_to_max_in_the_cheapest_night_and_keeps_the_floor() -> None:
    """40 %: tonight only the floor would be needed, the cheap night tomorrow fills up to 90 %."""
    engine, _ = run(3, settings={"fill_up": True}, **PLUGGED_IN)
    plan = engine.charging_plan
    tomorrow_night = [ts for ts in charge_hours(engine) if ts.date() == DAY + datetime.timedelta(days=1) and ts.hour < 7]
    assert tomorrow_night
    # day 1 reaches the max recommended level before its driving
    assert plan[1]["battery_level_at_midnight_sum"] + engine.charging_plan[1]["offday_battery_level_needed"] == pytest.approx(90, abs=0.5)
    # never below the daily minimum at midnight (the test has prices for 4 days)
    for day in range(4):
        assert plan[day]["battery_level_at_midnight_sum"] >= 30 - 0.5, day


def test_fill_up_follows_the_prices_not_the_clock() -> None:
    """Solar days: tomorrow's cheapest hours are at noon, not at night."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    snap = snapshot(now, settings={"fill_up": True}, **PLUGGED_IN)
    price = snap.attributes["sensor.price"]
    for row in price["raw_tomorrow"]:
        hour = datetime.datetime.fromisoformat(row["hour"]).hour
        row["price"] = round((0.3 if 10 <= hour <= 15 else 1.2) + hour / 100, 3)
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    hours = charge_hours(engine)
    today = [ts for ts in hours if ts.date() == DAY]
    tomorrow = [ts for ts in hours if ts.date() == DAY + datetime.timedelta(days=1)]
    # today only the floor: 40 % does not cover today's 16.7 % driving + the 30 % minimum
    assert sum(engine.charge_hours[ts]["battery_level"] for ts in today) == pytest.approx(46.67 - 40, abs=0.5)
    # filling up waits for tomorrow's cheap noon
    assert tomorrow and all(10 <= ts.hour <= 15 for ts in tomorrow), tomorrow


def test_full_charge_recommended_charges_to_100() -> None:
    """Every 7 days, last full charge 10 days ago: tomorrow's plan reaches 100 %, above the max recommended 90 %."""
    last = datetime.datetime.combine(DAY - datetime.timedelta(days=10), datetime.time(8, 0))
    engine, _ = run(12, {"runtime": {"sensors": {"last_full_charge": last}}}, settings={"full_charge_recommended": 7.0}, **PLUGGED_IN)
    hours = charge_hours(engine)
    assert hours and all("battery_health" in engine.charge_hours[ts] for ts in hours)
    assert 40 + engine.charge_hours["total_procent"] == pytest.approx(100, abs=0.5)


def test_full_charge_not_needed_after_a_recent_one() -> None:
    last = datetime.datetime.combine(DAY - datetime.timedelta(days=2), datetime.time(8, 0))
    engine, _ = run(12, {"runtime": {"sensors": {"last_full_charge": last}}}, settings={"full_charge_recommended": 7.0}, **PLUGGED_IN)
    assert not [ts for ts in charge_hours(engine) if "battery_health" in engine.charge_hours[ts]]


def test_full_charge_is_registered_at_100() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(6, 0)), level=100), engine.is_battery_fully_charged)
    assert result.error is None
    assert "last_full_charge" in result.states
    assert "runtime" in result.db_saves
    assert "last_full_charge" not in result.settings


def test_full_charge_at_99_requires_30_minutes_stopped() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    started = datetime.datetime.combine(DAY, datetime.time(6, 0))
    first = engine.run_job(snapshot(started, level=99, status="awaiting_start"), engine.is_battery_fully_charged)
    assert first.error is None
    assert "last_full_charge" not in first.states
    assert engine.full_charge_99_since == started
    assert decode(first.db_saves["runtime"])["full_charge_99_since"] == started

    after_29_minutes = started + datetime.timedelta(minutes=29)
    waiting = engine.run_job(snapshot(after_29_minutes, level=99, status="awaiting_start"), engine.is_battery_fully_charged)
    assert waiting.error is None
    assert "last_full_charge" not in waiting.states

    after_30_minutes = started + datetime.timedelta(minutes=30)
    completed = engine.run_job(snapshot(after_30_minutes, level=99, status="awaiting_start"), engine.is_battery_fully_charged)
    assert completed.error is None
    assert completed.states["last_full_charge"] == after_30_minutes
    assert engine.full_charge_99_since is None


def test_full_charge_at_99_does_not_start_wait_while_charging() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    engine.full_charge_99_since = datetime.datetime.combine(DAY, datetime.time(5, 45))
    result = engine.run_job(
        snapshot(datetime.datetime.combine(DAY, datetime.time(6, 0)), level=99, status="charging"),
        engine.is_battery_fully_charged,
    )
    assert result.error is None
    assert "last_full_charge" not in result.states
    assert engine.full_charge_99_since is None


def test_full_charge_at_99_is_not_retried_during_wait() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    since = now - datetime.timedelta(minutes=15)
    last = now - datetime.timedelta(days=10)
    engine, _ = run(
        12,
        {
            "runtime": {
                "sensors": {"last_full_charge": last},
                "full_charge_99_since": since,
            }
        },
        level=99,
        status="awaiting_start",
        settings={"full_charge_recommended": 7.0},
    )
    assert engine.full_charge_99_since == since
    assert not [ts for ts in charge_hours(engine) if "battery_health" in engine.charge_hours[ts]]
    assert engine.charger_action == "stop"


@pytest.mark.parametrize(("location", "hour", "expected_km"), [("not_home", 12, 50.0), ("not_home", 6, 50.0), ("home", 6, 100.0), ("home", 12, None)])
def test_away_on_a_workday_counts_half_the_distance(location: str, hour: int, expected_km: float | None) -> None:
    """Thursday 07:00-16:00, 100 km: away from home only the way back is left for today."""
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    settings = {"workplan_charging": True, "workday_thursday": True, "workday_distance_needed_thursday": 100.0}
    snap = snapshot(now, level=60, settings=settings)
    snap.states["device_tracker.ev"] = location
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    today = engine.charging_plan[0]
    if expected_km is None:
        # home again after the departure time: no driving left today (as before)
        assert not today["workday"]
    else:
        assert today["workday"]
        assert today["work_range_needed"] == expected_km
        # the day's driving is taken from the level before work (incl. charging planned before the departure)
        assert today["battery_level_after_work_sum"] == pytest.approx(today["battery_level_before_work_sum"] - today["work_battery_level_needed"], abs=0.1)


def test_virtual_car_reports_the_drive_after_unplugging_from_completed() -> None:
    """Without a car integration: unplugged long after charging completed, plugged in again and the level entered."""
    options = {**OPTIONS, **{key: "" for key in ("usable_battery_level_entity_id", "estimated_battery_range_entity_id", "odometer_entity_id", "charge_port_door_entity_id", "charging_limit_entity_id", "location_entity_id")}}
    options["battery_size"] = 9.9
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)

    def at(hour: int) -> datetime.datetime:
        return datetime.datetime.combine(DAY, datetime.time(hour, 0))

    def snap(hour: int, status: str, level: float, history: list[tuple[int, str]]) -> Snapshot:
        result = snapshot(at(hour), status=status, settings={"battery_level": level, "estimated_total_range": 60.0})
        result.buffers = {"sensor.charger_status": [(at(h).replace(tzinfo=TZ).timestamp(), value) for h, value in history]}
        return result

    def drive_notifications(result) -> list[dict]:
        return [n for n in result.mobile_notifications if TEXTS["ui.drive_efficiency.title"] in n["title"]]

    # first drive, unplugged while the charger still waited for the plan: 90 % -> 70 %
    history = [(0, "awaiting_start")]
    assert engine.run_job(snap(1, "awaiting_start", 90.0, history), engine.startup, {}).error is None
    assert engine.run_job(snap(1, "disconnected", 90.0, history), engine.charger_status_changed, "disconnected", "awaiting_start").error is None
    assert drive_notifications(engine.run_job(snap(2, "awaiting_start", 70.0, history), engine.emulated_battery_level_changed, 70.0, 0.0))
    assert engine.last_drive_efficiency_data["reported"] is True

    # charged to 80 %, completed at 05:00 and unplugged at 07:00: the level before the drive is still saved
    history = [(2, "awaiting_start"), (4, "charging"), (5, "completed")]
    assert engine.run_job(snap(7, "disconnected", 80.0, history), engine.charger_status_changed, "disconnected", "completed").error is None
    assert engine.sensor_states["drive_efficiency_last_battery_level"] == 80.0

    plugged = engine.run_job(snap(16, "awaiting_start", 50.0, history), engine.emulated_battery_level_changed, 50.0, 0.0)
    assert plugged.error is None
    assert drive_notifications(plugged)
    assert engine.last_drive_efficiency_data["usedBattery"] == 30.0
    assert engine.last_drive_efficiency_data["distance"] == 18.0

    # a level above the saved one is no drive
    history = [(16, "awaiting_start")]
    assert engine.run_job(snap(17, "disconnected", 50.0, history), engine.charger_status_changed, "disconnected", "awaiting_start").error is None
    higher = engine.run_job(snap(18, "awaiting_start", 60.0, history), engine.emulated_battery_level_changed, 60.0, 0.0)
    assert higher.error is None
    assert not drive_notifications(higher)


@pytest.mark.parametrize(
    ("status", "status_history", "cable", "cable_history", "home"),
    [
        ("awaiting_start", [], None, [], True),
        ("disconnected", [(10, "completed")], None, [], True),  # unplugged 2 hours after charging completed
        ("disconnected", [(11, "charging"), (11.95, "disconnected")], None, [], True),  # unplugged 3 minutes ago
        ("disconnected", [(10, "charging"), (11, "disconnected")], None, [], False),
        ("unavailable", [], None, [], False),
        ("disconnected", [(10, "disconnected")], "on", [], True),  # the cable says plugged in
        ("disconnected", [(10, "disconnected")], "off", [(11, "on"), (11.95, "off")], True),
        ("disconnected", [(10, "disconnected")], "off", [(10, "off")], False),
        (None, [], "off", [(10, "off")], False),  # cable only
        (None, [], "on", [], True),
        (None, [], None, [], True),  # nothing to go by
    ],
)
def test_without_location_home_follows_charger_status_and_cable(status, status_history, cable, cable_history, home: bool) -> None:
    options = {**OPTIONS, "location_entity_id": "", "status_entity_id": "sensor.charger_status" if status else "", "cable_connected_entity_id": "binary_sensor.charger_cable" if cable else ""}
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))

    def rows(history: list[tuple[float, str]]) -> list[tuple[float, str]]:
        return [(now.replace(tzinfo=TZ).timestamp() - (12 - hour) * 3600, value) for hour, value in history]

    snap = snapshot(now, status=status or "disconnected")
    snap.states.pop("device_tracker.ev")
    if status is None:
        snap.states.pop("sensor.charger_status")
    if cable:
        snap.states["binary_sensor.charger_cable"] = cable
    snap.buffers = {"sensor.charger_status": rows(status_history), "binary_sensor.charger_cable": rows(cable_history)}
    result = engine.run_job(snap, engine.is_ev_home)
    assert result.error is None
    assert result.value is home


def _price_history() -> dict:
    """Price history where the price tells the weekday: 2.<weekday> in every hour."""
    history = {hour: {dow: [2.0 + dow / 10] * 3 for dow in range(7)} for hour in range(24)}
    return {"history": history, "history_sell": deepcopy(history), "max": [3.0], "mean": [2.0], "min": [1.0], "version": 2.0}


def test_missing_hours_are_filled_from_the_price_history() -> None:
    """Only today's prices are published: the rest of the plan (today + 7 days) uses the history of that weekday."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    snap.price_data["attributes"]["sensor.price"].update(raw_tomorrow=[], tomorrow_valid=False, forecast=[])
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None

    prices = engine.grid_price_cache["prices"]
    sources = engine.grid_price_cache["sources"]
    assert prices[now.replace(hour=17)] == pytest.approx(1.0 + 1.5 + 0.17)  # published price kept
    assert sources[now.replace(hour=17)] == "energidataservice:real"
    for days in (1, 4, 7):
        timestamp = now.replace(hour=17) + datetime.timedelta(days=days)
        assert sources[timestamp] == "database"
        assert prices[timestamp] == pytest.approx(round(2.0 + timestamp.weekday() / 10 + days / 30, 2))
    assert max(prices) == datetime.datetime.combine(DAY + datetime.timedelta(days=7), datetime.time(23))
    assert engine.grid_price_cache["using_offline_prices"] is False


def test_offline_prices_use_the_same_weekday() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    snap.price_data = {"attributes": {}, "values": {}}
    snap.attributes = {}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()})
    engine.run_job(snap, engine.get_grid_prices, True)
    prices = engine.grid_price_cache["prices"]
    assert engine.grid_price_cache["using_offline_prices"] is True
    for days in range(7):
        timestamp = now + datetime.timedelta(days=days)
        assert prices[timestamp] == pytest.approx(round(2.0 + timestamp.weekday() / 10 + days / 30, 2))


def test_price_history_never_stores_the_filled_hours() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    rows = snap.price_data["attributes"]["sensor.price"]["raw_today"]
    snap.price_data["attributes"]["sensor.price"]["raw_today"] = [row for row in rows if row["hour"][11:13] not in ("14", "15", "16")]
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()}).error is None
    assert engine.run_job(snap, engine.update_grid_prices).error is None
    assert engine.grid_price_cache["sources"][now.replace(hour=15)] == "database"
    # 21 real hours: the day is not stored, so the filled hours never become history
    assert engine.kwh_avg_prices_db["history"][15][DAY.weekday()] == _price_history()["history"][15][DAY.weekday()]


def test_sell_value_with_fixed_solar_sell_price() -> None:
    """A fixed price settles the difference to the hourly price: fixed - hourly, also negative."""
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.sell_value(5.0) == pytest.approx(5.0)
    engine = CJPEngine(const.build_config({**OPTIONS, "solar_sell_fixed_price": 1.0}), "cjp_test", TEXTS)
    assert engine.sell_value(5.0) == pytest.approx(-4.0)
    assert engine.sell_value(0.5) == pytest.approx(0.5)


@pytest.mark.parametrize("fixed", [-1.0, 1.0])
def test_solar_sell_price_with_fixed_price(fixed: float) -> None:
    """The solar kWh is worth what selling it would earn: the hourly sell price, or fixed - hourly."""
    options = {**OPTIONS, "production_entity_id": "sensor.solar", "solar_sell_fixed_price": fixed}
    now = datetime.datetime.combine(DAY, datetime.time(18, 0))
    snap = snapshot(now)
    snap.states["sensor.solar"] = "0"
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    result = engine.run_job(snap, engine.get_solar_sell_price)
    assert result.error is None, result.error
    hourly = engine.price_provider.get_sell_price(now)["price"]
    assert result.value == pytest.approx(hourly if fixed == -1.0 else 1.0 - hourly, abs=0.001)
    if fixed != -1.0:
        assert result.value < 0.0  # 18:00 sells for more than the fixed price
