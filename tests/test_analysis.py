"""Analysis: drives against the temperature, ready at departure, planned against charged, capacity."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine_analysis import evaluate_price_day, find_drives, linear_fit, temperature_bins

from .test_engine import DAY, OPTIONS, PLUGGED_IN, TEXTS, TZ, _price_rows, snapshot

BATTERY = 75.0


def _ts(day: datetime.date, hour: int, minute: int = 0) -> float:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=TZ).timestamp()


def _engine(hour: int = 12, settings: dict | None = None, **kwargs):
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(hour, 0)), settings=settings, **kwargs)
    assert engine.run_job(snap, engine.startup, {}).error is None
    return engine, snap


def test_find_drives_between_unplug_and_plug_in() -> None:
    status = [(_ts(DAY, 6), True), (_ts(DAY, 7), False), (_ts(DAY, 16), True), (_ts(DAY, 17), False), (_ts(DAY, 17, 10), True)]
    odometer = [(_ts(DAY, 0), 1000.0), (_ts(DAY, 8), 1040.0), (_ts(DAY, 15), 1080.0), (_ts(DAY, 17, 5), 1082.0)]
    soc = [(_ts(DAY, 0), 80.0), (_ts(DAY, 15), 64.0), (_ts(DAY, 17, 5), 63.0)]
    drives = find_drives(status, odometer, soc)
    # the short drive at 17:00 (2 km, 1 %) is not a drive
    assert drives == [{"start": _ts(DAY, 7), "end": _ts(DAY, 16), "km": 80.0, "used": 16.0, "temp": None}]


def test_temperature_fit_shows_what_cold_costs() -> None:
    # 6 km/kWh at 20 °C, 0.05 km/kWh less per degree colder
    points = [{"temp": t, "km": 50.0, "kwh": 50.0 / (5.0 + 0.05 * t), "km_per_kwh": 5.0 + 0.05 * t} for t in (-5, 0, 5, 10, 15, 20)]
    fit = linear_fit(points)
    assert fit["per_degree"] == pytest.approx(0.05)
    assert fit["at_0"] == pytest.approx(5.0) and fit["at_20"] == pytest.approx(6.0)
    assert fit["cold_cost"] == pytest.approx(1 - 5.0 / 6.0, abs=0.001)
    bins = temperature_bins(points)
    assert [b["from"] for b in bins] == [-5.0, 0.0, 5.0, 10.0, 15.0, 20.0]
    assert linear_fit(points[:3]) is None


def test_drives_get_the_outdoor_temperature() -> None:
    engine, snap = _engine()
    yesterday = DAY - datetime.timedelta(days=1)
    data = {
        "backfill": True,
        "status": [(_ts(yesterday, 7), False), (_ts(yesterday, 16), True)],
        "odometer": [(_ts(yesterday, 0), 1000.0), (_ts(yesterday, 15), 1080.0)],
        "soc": [(_ts(yesterday, 0), 80.0), (_ts(yesterday, 15), 64.0)],
        "temps": {_ts(yesterday, h): float(h) for h in range(24)},
    }
    result = engine.run_job(snap, engine.analysis_update, data)
    assert result.error is None, result.error
    drive = result.attributes["analysis"]["drives"][0]
    assert drive["temp"] == pytest.approx(sum(range(7, 17)) / 10)
    assert drive["km_per_kwh"] == pytest.approx(80 / (16 / 100 * BATTERY), abs=0.01)
    assert engine.analysis_backfilled()


def test_departure_is_saved_with_target_and_requirement() -> None:
    """Thursday 07:00, 50 km: before the departure the plan's target is remembered, after it saved."""
    settings = {"workplan_charging": True, "workday_thursday": True, "workday_distance_needed_thursday": 50.0}
    engine, snap = _engine(5, settings, level=60, **PLUGGED_IN)
    assert engine.run_job(snap, engine.charge_trigger).error is None
    pending = engine.analysis_db["pending_departure"]
    assert pending["required"] == pytest.approx(engine.charging_plan[0]["work_battery_level_needed"] + 30, abs=0.1)
    after = snapshot(datetime.datetime.combine(DAY, datetime.time(7, 5)), settings=settings, level=58)
    result = engine.run_job(after, engine.analysis_check_departure)
    assert result.error is None, result.error
    departure = engine.analysis_db["departures"][-1]
    assert departure["level"] == 58.0 and departure["met"] is (58.0 >= pending["required"] - 0.5)
    assert result.attributes["analysis"]["departures_count"] == 1
    assert "pending_departure" not in engine.analysis_db


def test_planned_against_actual_per_day() -> None:
    engine, snap = _engine(23)
    tomorrow = DAY + datetime.timedelta(days=1)
    engine.charge_hours = {datetime.datetime.combine(tomorrow, datetime.time(2)): {"kWh": 11.0}, datetime.datetime.combine(tomorrow, datetime.time(3)): {"kWh": 5.0}}
    assert engine.run_job(snap, engine.analysis_record_planned).error is None
    assert engine.analysis_db["planned_days"][tomorrow.isoformat()] == 16.0
    # the next evening: what was charged tomorrow, per kind
    engine.charging_history_db.update({
        datetime.datetime.combine(tomorrow, datetime.time(2)): {"kWh": 15.0, "charging_type": "planned"},
        datetime.datetime.combine(tomorrow, datetime.time(18)): {"kWh": 4.0, "charging_type": "manual"},
    })
    later = snapshot(datetime.datetime.combine(tomorrow + datetime.timedelta(days=1), datetime.time(12)))
    day = engine.run_job(later, engine.publish_analysis).attributes["analysis"]["planned_days"][-1]
    assert (day["date"], day["plan"], day["actual"]) == (tomorrow.isoformat(), 16.0, 19.0)
    assert day["planned"] == 15.0 and day["manual"] == 4.0


def test_capacity_per_month_counts_every_segment() -> None:
    from .test_charging_loss_model import _session  # noqa: PLC0415

    engine, snap = _engine()
    start = datetime.datetime.combine(DAY - datetime.timedelta(days=40), datetime.time(1), tzinfo=TZ).timestamp()
    for day in range(12):  # more than the 10 kept per group
        soc, meter, power = _session(start + day * 86400, 11.04, 3, 2)
        assert engine.run_job(snap, engine.charging_loss_update, {"soc": soc, "meter": meter, "power": power}).error is None
    result = engine.run_job(snap, engine.charging_loss_update, {})
    months = result.attributes["charging_loss_model"]["capacity_months"]
    assert sum(m["segments"] for m in months) > 10
    assert all(m["group"] == "> 7 kW" and m["kwh_per_percent"] > BATTERY / 100 for m in months)


def test_price_day_is_judged_per_days_ahead() -> None:
    rows = [{"real": hour / 10, "1": hour / 10 + 0.1, "2": (23 - hour) / 10, **({"3": 1.0} if hour < 10 else {})} for hour in range(24)]
    day = evaluate_price_day(rows)
    # 1 day ahead: 0.1 too high but the same order
    assert day["1"]["n"] == 24
    assert day["1"]["bias"] / 24 == pytest.approx(0.1)
    assert (day["1"]["cheap"], day["1"]["cheap_miss"]) == (4, 0.0)
    # 2 days ahead: the order turned around, the "cheap" hours were the 4 most expensive
    assert day["2"]["cheap"] == 0
    assert day["2"]["cheap_miss"] == pytest.approx(2.0)
    # 3 days ahead: too few hours
    assert "3" not in day


def _forecast_snapshot(now: datetime.datetime):
    """Tomorrow is only a forecast (0.1 above the real prices of snapshot())."""
    tomorrow = now.date() + datetime.timedelta(days=1)
    snap = snapshot(now)
    snap.price_data["attributes"]["sensor.price"].update(
        raw_tomorrow=[], tomorrow_valid=False,
        forecast=_price_rows(tomorrow, 0.9) + _price_rows(tomorrow + datetime.timedelta(days=1), 1.1),
    )
    return snap


def _hour_end(engine: CJPEngine, snap):
    assert engine.run_job(snap, engine.update_grid_prices).error is None
    result = engine.run_job(snap, engine.hour_end)
    assert result.error is None, result.error
    return result


def test_price_forecast_is_compared_with_the_real_prices() -> None:
    tomorrow = DAY + datetime.timedelta(days=1)
    morning = datetime.datetime.combine(DAY, datetime.time(10, 59))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(_forecast_snapshot(morning), engine.startup, {}).error is None
    _hour_end(engine, _forecast_snapshot(morning))
    hours = engine.analysis_db["price_hours"]
    forecast = {row["hour"][11:13]: row["price"] for row in _price_rows(tomorrow, 0.9)}
    # without the daily adder of the providers
    assert hours[f"{tomorrow.isoformat()}T05"]["1"] == pytest.approx(forecast["05"], abs=0.006)
    assert "real" not in hours[f"{tomorrow.isoformat()}T05"]

    # 13: the real prices of tomorrow are known
    afternoon = datetime.datetime.combine(DAY, datetime.time(13, 59))
    result = _hour_end(engine, snapshot(afternoon))
    judged = engine.analysis_db["price_days"][tomorrow.isoformat()]["1"]
    assert judged["n"] == 24
    assert judged["bias"] / 24 == pytest.approx(0.1, abs=0.006)
    assert judged["cheap"] == 4
    published = result.attributes["analysis"]["price_forecast"]
    assert published["last_30"][0]["lead"] == 1
    assert published["last_30"][0]["mae"] == pytest.approx(0.1, abs=0.006)
    assert published["last_30"][0]["correction"] is None  # 1 day measured


def test_price_forecast_is_corrected_with_its_bias() -> None:
    tomorrow = DAY + datetime.timedelta(days=1)
    now = datetime.datetime.combine(DAY, datetime.time(10, 5))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = _forecast_snapshot(now)
    assert engine.run_job(snap, engine.startup, {}).error is None
    for back in range(1, 8):
        engine.analysis_db["price_days"][(DAY - datetime.timedelta(days=back)).isoformat()] = {
            # 1 day ahead 0.2 too high; 3 days ahead 2 kr too low (capped at 0.5); 2 days ahead: too few days
            "1": {"n": 24, "abs": 4.8, "bias": 4.8},
            "3": {"n": 24, "abs": 48.0, "bias": -48.0},
            **({"2": {"n": 24, "abs": 2.4, "bias": 2.4}} if back <= 3 else {}),
        }
    assert engine.price_forecast_corrections() == {1: -0.2, 3: 0.5}

    assert engine.run_job(snap, engine.update_grid_prices).error is None
    provider = engine.price_provider.buy_prices
    prices = engine.get_grid_prices()
    hour = datetime.datetime.combine(tomorrow, datetime.time(5))
    later = hour + datetime.timedelta(days=1)
    today = datetime.datetime.combine(DAY, datetime.time(15))
    assert prices[hour] == pytest.approx(provider[hour] - 0.2)
    assert prices[later] == provider[later]
    assert prices[today] == provider[today]  # real prices are never corrected

    published = engine.run_job(snap, engine.hour_end).attributes["analysis"]["price_forecast"]
    assert {entry["lead"]: entry["correction"] for entry in published["last_30"]} == {1: -0.2, 2: None, 3: 0.5}
    row = next(row for row in published["hours"] if row["t"] == f"{tomorrow.isoformat()}T05")
    assert row["corrected"] == pytest.approx(row["d1"] - 0.2)

    # the switch off: the cached prices are recalculated without a new price update
    snap.settings["price_forecast_correction"] = False
    assert engine.run_job(snap, engine.hour_end).error is None
    assert engine.run_job(snap, engine.price_forecast_corrections).value == {}
    assert engine.run_job(snap, engine.get_grid_prices).value[hour] == provider[hour]


def test_forecast_types_are_judged_against_the_measurements() -> None:
    """Before an hour / a drive is added to its history: what each forecast type predicted for it."""
    engine, snap = _engine()
    stamps = [datetime.datetime.combine(DAY - datetime.timedelta(days=back), datetime.time(12)) for back in range(1, 5)]
    history = [[stamp, watt] for stamp, watt in zip(stamps, (1000.0, 500.0, 500.0, 500.0))]  # newest first
    engine.power_values_db[12]["power_consumption_without_all_exclusion"] = list(history)
    predicted = {kind: engine.get_forecast_value(history, kind) for kind in ("ema", "trend", "average")}
    assert engine.run_job(snap, engine.power_values_to_db, {"power_consumption_without_all_exclusion": 600.0}).error is None
    day = engine.analysis_db["consumption_days"][DAY.isoformat()]
    assert (day["hours"], day["use"]) == (1, 0.6)
    assert day["average"] == [pytest.approx(0.025), pytest.approx(0.025)]  # the average of the history: 625 W
    assert day["ema"][1] == pytest.approx((predicted["ema"] - 600.0) / 1000.0, abs=0.001)
    engine.km_kwh_efficiency_db = [[stamps[0], 6.0], [stamps[1], 5.0]]
    assert engine.run_job(snap, engine.analysis_efficiency_forecast, engine.km_kwh_efficiency_db, 5.25).error is None

    # the next day: the closed day and the drive against the measurements
    later = snapshot(datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(12)))
    forecast = engine.run_job(later, engine.publish_analysis).attributes["analysis"]["forecast"]
    assert forecast["selected"] == "ema"
    consumption = forecast["consumption"]["last_30"]
    assert (consumption["hours"], consumption["actual"]) == (1, 0.6)
    assert consumption["average"] == {"mae": 0.025, "bias": 0.025, "relative": pytest.approx(0.042, abs=0.001), "forecast": 0.625}
    drives = forecast["drives"]["all"]
    assert (drives["drives"], drives["actual"]) == (1, 5.25)
    assert drives["average"]["forecast"] == 5.5 and drives["average"]["mae"] == 0.25
    assert forecast["drives"]["best"] == min(("ema", "trend", "average"), key=lambda kind: drives[kind]["mae"])
