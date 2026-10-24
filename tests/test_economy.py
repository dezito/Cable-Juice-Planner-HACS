"""Charging economy: achieved price compared with average, daily charging and the lowest possible."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine_economy import average_price, fill_cheapest

from .test_engine import DAY, OPTIONS, TEXTS, TZ, snapshot

KW = 3 * 16 * 230 / 1000  # 11.04 kW, the test charger


def _ts(day: datetime.date, hour: int) -> float:
    return datetime.datetime.combine(day, datetime.time(hour), tzinfo=TZ).timestamp()


def test_fill_cheapest_takes_the_cheapest_hours_first() -> None:
    prices = {_ts(DAY, h): p for h, p in [(0, 1.0), (1, 0.5), (2, 2.0)]}
    # 15 kWh at 11.04 kW: the 0.5 hour full, the rest at 1.0
    assert fill_cheapest(prices, _ts(DAY, 0), _ts(DAY, 3), 15.0, KW) == pytest.approx(KW * 0.5 + (15 - KW) * 1.0)
    # more than fits in the window: the rest at the average price
    assert fill_cheapest(prices, _ts(DAY, 1), _ts(DAY, 2), 15.0, KW) == pytest.approx(KW * 0.5 + (15 - KW) * 0.5)
    assert average_price(prices, _ts(DAY, 0), _ts(DAY, 3)) == pytest.approx(3.5 / 3)
    assert fill_cheapest({}, 0.0, 3600.0, 1.0, KW) is None


def _engine(settings: dict | None = None) -> tuple[CJPEngine, object]:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings=settings)
    assert engine.run_job(snap, engine.startup, {}).error is None
    return engine, snap


def test_month_compares_with_daily_and_lowest() -> None:
    """Plugged in Tuesday 17:00 to Thursday 08:00, departure Wednesday 07:00 in the work plan.
    CJP charged 22 kWh in Wednesday night's cheap hours (after the departure, the car stayed home)."""
    tuesday, wednesday = DAY - datetime.timedelta(days=2), DAY - datetime.timedelta(days=1)
    prices = {}
    for day, night in ((tuesday, 1.0), (wednesday, 0.4)):
        for hour in range(24):
            prices[_ts(day, hour)] = night if hour < 6 else 2.0
    engine, snap = _engine({"workplan_charging": True, "workday_wednesday": True, "workday_departure_wednesday": datetime.time(7, 0)})
    # CJP's charging history: two sessions in Wednesday night at 0.4 kr
    wednesday_night = datetime.datetime.combine(wednesday, datetime.time(0))
    engine.charging_history_db.update({
        wednesday_night.replace(hour=1): {"kWh": 11.0, "cost": 4.4, "kWh_from_grid": 11.0},
        wednesday_night.replace(hour=2): {"kWh": 11.0, "cost": 4.4, "kWh_from_grid": 11.0},
    })
    window = (_ts(tuesday, 17), _ts(DAY, 8))
    result = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [window], "odometer_months": {DAY.strftime("%Y-%m"): 440.0}})
    assert result.error is None, result.error
    month = result.attributes["economy"]["months"][-1]
    assert month["kwh"] == pytest.approx(22.0)
    assert month["achieved"] == pytest.approx(8.8)
    # daily charging: Wednesday's departure at 07:00, so Wednesday night 00-06 at 0.4 as well
    assert month["daily"] == pytest.approx(22.0 * 0.4)
    assert month["lowest"] == pytest.approx(22.0 * 0.4)
    assert month["average_unit"] > month["achieved_unit"]
    assert month["price_per_km"] == pytest.approx(8.8 / 440)
    assert result.states["economy"] == pytest.approx(0.0)


def test_daily_charging_cannot_wait_for_a_cheaper_night() -> None:
    """Departure Wednesday 07:00: daily charging must charge Tuesday night (1.0 kr). The car stayed
    home on Wednesday and CJP waited for Wednesday night (0.4 kr): saving and score 100 %."""
    tuesday, wednesday = DAY - datetime.timedelta(days=2), DAY - datetime.timedelta(days=1)
    prices = {}
    for day in (tuesday, wednesday, DAY):
        for hour in range(24):
            prices[_ts(day, hour)] = 2.0
    for hour in (22, 23):
        prices[_ts(tuesday, hour)] = 1.0
        prices[_ts(wednesday, hour)] = 0.4
    for hour in range(6):
        prices[_ts(wednesday, hour)] = 1.0
        prices[_ts(DAY, hour)] = 0.4
    engine, snap = _engine({"workplan_charging": True, "workday_wednesday": True, "workday_departure_wednesday": datetime.time(7, 0)})
    engine.charging_history_db[datetime.datetime.combine(wednesday, datetime.time(23))] = {"kWh": 11.0, "cost": 4.4, "kWh_from_grid": 11.0}
    window = (_ts(tuesday, 17), _ts(DAY, 8))
    month = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [window]}).attributes["economy"]["months"][-1]
    assert month["daily"] == pytest.approx(11.0 * 1.0)
    assert month["lowest"] == pytest.approx(11.0 * 0.4)
    assert month["saving_daily"] == pytest.approx(11.0 - 4.4)
    assert month["score"] == pytest.approx(1.0)


def test_sources_per_month_and_public_charging() -> None:
    engine, snap = _engine()
    day = datetime.datetime.combine(DAY, datetime.time(2))
    engine.charging_history_db.update({
        day: {"kWh": 10.0, "cost": 5.0, "kWh_from_grid": 6.0, "kWh_from_local_energy": 4.0, "solar_kwh_of_local_energy": 3.0, "powerwall_kwh_of_local_energy": 1.0},
        day.replace(hour=14): {"kWh": 20.0, "cost": 60.0, "kWh_from_public": 20.0, "charging_type": "public_charging"},
    })
    month = engine.run_job(snap, engine.economy_update, {}).attributes["economy"]["months"][-1]
    assert (month["grid"], month["solar"], month["powerwall"], month["public"]) == (6.0, 3.0, 1.0, 20.0)
    assert month["public_cost"] == 60.0 and month["cost"] == 65.0
    # without connection windows there is nothing to compare
    assert month["score"] is None and month["saving_daily"] is None


def test_windows_are_recorded_from_the_charger_status() -> None:
    engine, snap = _engine()
    assert engine.run_job(snap, engine.charger_status_changed, "awaiting_start", "disconnected").error is None
    assert engine.economy_db["windows"][-1]["end"] is None
    assert engine.run_job(snap, engine.charger_status_changed, "disconnected", "completed").error is None
    assert engine.economy_db["windows"][-1]["end"] is not None


def test_own_prices_are_stored_and_used() -> None:
    """hour_end keeps the plan's prices of the passed hours; they win over the price sensor's history."""
    engine, snap = _engine()
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    stored = engine.economy_db["prices"]
    assert stored and max(stored) <= _ts(DAY, 12)
    assert all(ts <= _ts(DAY, 12) for ts in stored)
    yesterday = DAY - datetime.timedelta(days=1)
    engine.charging_history_db[datetime.datetime.combine(DAY, datetime.time(2))] = {"kWh": 11.0, "cost": 11.0, "kWh_from_grid": 11.0}
    window = (_ts(yesterday, 20), _ts(DAY, 8))
    # the sensor history says 9 kr, the plan's own prices (from the test prices) win where they exist
    sensor_prices = {_ts(yesterday, h): 9.0 for h in range(20, 24)} | {_ts(DAY, h): 9.0 for h in range(8)}
    month = engine.run_job(snap, engine.economy_update, {"prices": sensor_prices, "windows": [window]}).attributes["economy"]["months"][-1]
    assert month["lowest"] < 11.0 * 9.0


def test_score_when_the_window_ends_at_the_departure() -> None:
    """Plugged in in the evening, unplugged at the departure: daily = lowest, the score still shows
    how close CJP came to the cheapest hours, measured from the average."""
    yesterday = DAY - datetime.timedelta(days=1)
    prices = {_ts(yesterday, h): 2.0 for h in range(17, 24)} | {_ts(DAY, h): (0.5 if h < 4 else 1.5) for h in range(7)}
    engine, snap = _engine({"workplan_charging": True, "workday_thursday": True, "workday_departure_thursday": datetime.time(7, 0)})
    # CJP charged 11 kWh at 1.5 kr (not the cheapest 0.5 kr hours)
    engine.charging_history_db[datetime.datetime.combine(DAY, datetime.time(5))] = {"kWh": 11.0, "cost": 16.5, "kWh_from_grid": 11.0}
    month = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [(_ts(yesterday, 17), _ts(DAY, 7))]}).attributes["economy"]["months"][-1]
    assert month["daily"] == pytest.approx(month["lowest"])
    average = sum(prices.values()) / len(prices)
    assert month["score"] == pytest.approx((average * 11 - 16.5) / (average * 11 - 0.5 * 11), abs=0.001)


def test_daily_without_prices_is_not_free() -> None:
    """Hours without a price after the departure were counted as 0 kr: daily came out below the lowest."""
    yesterday = DAY - datetime.timedelta(days=1)
    # prices only until the departure at 01:00, the car stayed plugged in until 09:00 (no prices after)
    prices = {_ts(yesterday, h): 2.0 for h in range(22, 24)} | {_ts(DAY, 0): 2.0}
    engine, snap = _engine({"workplan_charging": True, "workday_thursday": True, "workday_departure_thursday": datetime.time(1, 0)})
    engine.charging_history_db[datetime.datetime.combine(DAY, datetime.time(3))] = {"kWh": 40.0, "cost": 80.0, "kWh_from_grid": 40.0}
    month = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [(_ts(yesterday, 22), _ts(DAY, 9))]}).attributes["economy"]["months"][-1]
    assert month["daily"] >= month["lowest"] > 0
    assert month["daily_unit"] == pytest.approx(2.0)


def test_old_results_are_calculated_again() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)))
    old = {"windows": [{"start": 1.0, "end": 2.0, "kwh": 5.0, "achieved": 1.0, "daily": 0.1, "lowest": 0.5, "average": 2.0}]}
    assert engine.run_job(snap, engine.startup, {"economy": old}).error is None
    assert "daily" not in engine.economy_db["windows"][0] and engine.economy_db["version"] == 2
