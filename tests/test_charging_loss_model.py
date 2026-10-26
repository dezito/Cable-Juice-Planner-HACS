"""Charging loss per power: segments between changes of the battery level, the model, the sensor."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine_loss import (
    LOSS_GROUP_SIZE,
    _outlier_segments,
    find_segments,
    find_statistics_segments,
    fit_model,
    merge_segments,
    resample_steps,
)

from .test_engine import DAY, OPTIONS, TEXTS, snapshot

BATTERY = 75.0
P0, C1, C3 = 0.2, 0.07, 0.06  # the "true" car the sessions are simulated with


def _session(start: float, kw: float, phases: int, hours: float, soc: float = 20.0, poll: float = 40.0):
    """A charging session of the simulated car: battery level polled with a delay, meter every minute."""
    share = P0 / kw + (C3 if phases == 3 else C1)
    battery_kw = kw * (1 - share)
    soc_rows, meter_rows, power_rows = [(start - 600, soc)], [], []
    level, t, kwh = soc, start, 1000.0
    next_soc = int(level) + 1
    while t <= start + hours * 3600:
        meter_rows.append((t, round(kwh, 2)))
        level_now = soc + battery_kw * (t - start) / 3600 / BATTERY * 100
        while level_now >= next_soc:
            crossed = start + (next_soc - soc) / 100 * BATTERY / battery_kw * 3600
            soc_rows.append((crossed + poll, float(next_soc)))
            next_soc += 1
        t += 60
        kwh += kw / 60
    end = t
    power_rows = [(s, kw * 1000, kw * 990, kw * 1010) for s in range(int(start - start % 300), int(end), 300)]
    # after the session: both report once more
    soc_rows.append((end + 900, float(next_soc - 1)))
    meter_rows.append((end + 900, round(kwh, 2)))
    return sorted(set(soc_rows)), meter_rows, power_rows


def _segments(kw: float, phases: int, hours: float, start: float = 1_760_000_000.0):
    soc, meter, power = _session(start, kw, phases, hours)
    return find_segments(soc, meter, power, [], BATTERY, 3)


def test_segments_match_the_simulated_loss() -> None:
    segments = _segments(11.04, 3, 3)
    assert segments
    expected = P0 / 11.04 + C3
    for segment in segments:
        assert segment["phases"] == 3
        assert segment["soc_delta"] >= 3
        assert segment["loss"] == pytest.approx(expected, abs=0.01)
        assert segment["kw"] == pytest.approx(11.04, rel=0.01)


def test_variable_power_is_still_a_segment() -> None:
    soc, meter, power = _session(1_760_000_000.0, 11.04, 3, 2)
    # Solar surplus can vary the power substantially during one charging session.
    middle = power[len(power) // 2][0]
    power = [(s, m / 2, lo / 2, hi / 2) if s >= middle else (s, m, lo, hi) for s, m, lo, hi in power]
    segments = find_segments(soc, meter, power, [], BATTERY, 3)
    assert any(segment["start"] < middle < segment["end"] for segment in segments)


def test_isolated_low_power_outlier_does_not_break_a_segment() -> None:
    soc, meter, power = _session(1_760_000_000.0, 11.04, 3, 2)
    middle = power[len(power) // 2][0]
    power = [(start, 900.0, 500.0, 1300.0) if start == middle else (start, mean, low, high) for start, mean, low, high in power]
    assert any(segment["start"] < middle < segment["end"] for segment in find_segments(soc, meter, power, [], BATTERY, 3))


def test_sustained_low_power_breaks_a_segment() -> None:
    soc, meter, power = _session(1_760_000_000.0, 11.04, 3, 2)
    middle = power[len(power) // 2][0]
    power = [
        (start, 900.0, 500.0, 1300.0) if middle <= start < middle + 600 else (start, mean, low, high)
        for start, mean, low, high in power
    ]
    assert not any(segment["start"] < middle < segment["end"] for segment in find_segments(soc, meter, power, [], BATTERY, 3))


def test_no_segment_before_both_have_reported_after_it() -> None:
    soc, meter, power = _session(1_760_000_000.0, 11.04, 3, 2)
    last_change = soc[-2][0]
    # the meter has not reported since: the last segment must wait
    meter = [row for row in meter if row[0] < last_change - 120]
    assert all(segment["end"] < meter[-1][0] for segment in find_segments(soc, meter, power, [], BATTERY, 3))


def test_model_finds_the_fixed_share_and_the_chargers_loss() -> None:
    start = 1_760_000_000.0
    segments = []
    for offset, (kw, phases, hours) in enumerate([(11.04, 3, 3), (4.14, 3, 4), (1.38, 1, 9), (2.3, 1, 6)]):
        segments += _segments(kw, phases, hours, start + offset * 86400)
    model = fit_model(segments, 0.05)
    assert model["status"] == "model"
    assert model["p0"] == pytest.approx(P0, abs=0.05)
    assert model["c"][3] == pytest.approx(C3, abs=0.015)
    assert model["c"][1] == pytest.approx(C1, abs=0.02)


@pytest.mark.parametrize(("outlier_index", "share_deviation"), [
    (0, -0.35), (0, 0.35), (3, -0.35), (3, 0.35),
    (6, -0.35), (6, 0.35), (9, -0.35), (9, 0.35),
])
def test_isolated_high_or_low_loss_outlier_is_ignored(outlier_index: int, share_deviation: float) -> None:
    segments = []
    for kw, phases in ((1.38, 1), (2.3, 1), (4.14, 3), (11.04, 3)):
        for _ in range(3):
            hours = 2.0
            kwh = kw * hours
            share = P0 / kw + (C3 if phases == 3 else C1)
            kwh_battery = kwh * (1 - share)
            segments.append({
                "start": 1_760_000_000.0 + len(segments) * 86400,
                "end": 1_760_000_000.0 + len(segments) * 86400 + hours * 3600,
                "hours": hours, "kwh": kwh, "kwh_battery": kwh_battery,
                "soc_delta": kwh_battery / BATTERY * 100, "kw": kw, "phases": phases,
                "loss": share, "source": "history",
                "group": "< 2,5 kW" if kw < 2.5 else "2,5-4 kW" if kw < 4 else "4-7 kW" if kw < 7 else "> 7 kW",
            })

    outlier = segments[outlier_index]
    outlier["kwh_battery"] = outlier["kwh"] * (1 - outlier["loss"] - share_deviation)
    outlier["loss"] = 1 - outlier["kwh_battery"] / outlier["kwh"]

    accepted, rejected = _outlier_segments(segments)
    model = fit_model(segments, 0.05)

    assert rejected == [outlier]
    assert len(accepted) == len(segments) - 1
    assert model["p0"] == pytest.approx(P0, abs=0.05)
    assert model["c"][1] == pytest.approx(C1, abs=0.02)
    assert model["c"][3] == pytest.approx(C3, abs=0.02)


def test_model_fallbacks() -> None:
    assert fit_model([], 0.05) == {"status": "no_data", "p0": 0.0, "c": {1: 0.05, 3: 0.05}}
    one = fit_model(_segments(11.04, 3, 3), 0.05)
    assert one["status"] == "one_group"
    assert one["c"][1] == one["c"][3] == pytest.approx(P0 / 11.04 + C3, abs=0.01)


def test_merge_skips_the_same_charging_and_keeps_the_newest_per_group() -> None:
    first = _segments(11.04, 3, 3)
    assert merge_segments(first, first) == first
    many = []
    for day in range(LOSS_GROUP_SIZE + 5):
        many += _segments(11.04, 3, 1, 1_760_000_000.0 + day * 86400)
    merged = merge_segments([], many)
    assert len(merged) == LOSS_GROUP_SIZE
    assert merged[-1]["start"] == max(s["start"] for s in many)


def test_statistics_segments_from_variable_power_hours() -> None:
    kw, share = 11.04, P0 / 11.04 + C3
    powers = [8.0, 10.0, 12.0, 14.0]
    hours = []
    soc = 20.0
    for h, power_kw in enumerate(powers):
        soc_mid = soc + power_kw * (1 - share) / BATTERY * 50
        hours.append({
            "start": 1_760_000_000.0 + h * 3600, "power_mean": power_kw * 1000,
            "power_min": 0.0, "power_max": (power_kw + 2) * 1000,
            "kwh": power_kw, "soc": soc_mid,
        })
        soc += power_kw * (1 - share) / BATTERY * 100
    # an hour where the charging stopped ends the run
    hours.append({"start": hours[-1]["start"] + 3600, "power_mean": 0.0, "power_min": 0.0, "power_max": kw * 1000, "kwh": 0.0, "soc": soc})
    segments = find_statistics_segments(hours, BATTERY, 3)
    assert len(segments) == 1
    assert segments[0]["source"] == "statistics"
    assert segments[0]["loss"] == pytest.approx(share, abs=0.005)
    assert segments[0]["hours"] == pytest.approx(3)


def test_old_segments_are_invalidated_for_backfill() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)))
    old_database = {"segments": [{"start": 1.0}], "backfilled": True}
    result = engine.run_job(snap, engine.startup, {"charging_loss": old_database})
    assert result.error is None, result.error
    assert engine.charging_loss_db["segments"] == []
    assert not engine.charging_loss_backfilled()
    assert engine.charging_loss_db["segment_version"] == 2


def test_resample_holds_the_value_until_the_next_reading() -> None:
    rows = resample_steps([(0.0, 1000.0), (700.0, 2000.0)], 0.0, 1200.0)
    assert [r[0] for r in rows] == [0.0, 300.0, 600.0, 900.0]
    assert rows[1][1:] == (1000.0, 1000.0, 1000.0)
    assert rows[2][2:] == (1000.0, 2000.0)
    assert rows[3][1] == 2000.0


def test_engine_job_publishes_the_model() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)))
    assert engine.run_job(snap, engine.startup, {}).error is None
    start = 1_760_000_000.0
    data = {"soc": [], "meter": [], "power": [], "phases": [], "backfill": True}
    for offset, (kw, phases, hours) in enumerate([(11.04, 3, 3), (4.14, 3, 4), (1.38, 1, 9)]):
        soc, meter, power = _session(start + offset * 86400, kw, phases, hours)
        data["soc"] += soc
        data["meter"] += [(t, v + offset * 1000) for t, v in meter]
        data["power"] += power
    result = engine.run_job(snap, engine.charging_loss_update, data)
    assert result.error is None, result.error
    attrs = result.attributes["charging_loss_model"]
    assert attrs["status"] == "model" and attrs["backfilled"]
    assert attrs["fixed_kw"] == pytest.approx(P0, abs=0.05)
    assert [p["amps"] for p in attrs["curve"]["3"]] == list(range(6, 17))
    assert attrs["loss_low"]["loss"] > attrs["loss_high"]["loss"]
    assert result.states["charging_loss_model"] == pytest.approx((P0 / 11.04 + C3) * 100, abs=1.5)
    assert "charging_loss" in result.db_saves
    # restored after a restart
    saved = engine.charging_loss_db
    engine2 = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine2.run_job(snap, engine2.startup, {"charging_loss": saved}).error is None
    assert engine2.charging_loss_backfilled()


def test_reset_forgets_the_measurements() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)))
    assert engine.run_job(snap, engine.startup, {}).error is None
    soc, meter, power = _session(1_760_000_000.0, 11.04, 3, 3)
    groups = engine.run_job(snap, engine.charging_loss_update, {"soc": soc, "meter": meter, "power": power}).value
    assert groups["> 7 kW"] > 0 and groups["< 2,5 kW"] == 0
    assert engine.run_job(snap, engine.charging_loss_update, {"backfill": True}).error is None
    assert engine.charging_loss_backfilled()
    result = engine.run_job(snap, engine.charging_loss_reset)
    assert result.attributes["charging_loss_model"]["status"] == "no_data"
    assert not engine.charging_loss_backfilled() and "charging_loss" in result.db_saves


def test_one_group_only_draws_where_it_was_measured() -> None:
    """Like the Tesla: only night charging at 3x16 A. kWh per % is the headline, no 1 phase curve."""
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)))
    assert engine.run_job(snap, engine.startup, {}).error is None
    soc, meter, power = _session(1_760_000_000.0, 11.04, 3, 3)
    result = engine.run_job(snap, engine.charging_loss_update, {"soc": soc, "meter": meter, "power": power})
    attrs = result.attributes["charging_loss_model"]
    assert attrs["status"] == "one_group"
    assert list(attrs["curve"]) == ["3"]
    assert all(point["kw"] >= 7 for point in attrs["curve"]["3"])
    assert attrs["charger_loss_1_phase"] is None
    # kWh from the charger per 1 %: battery size / 100 / (1 - loss), independent of the model
    share = P0 / 11.04 + C3
    assert attrs["kwh_per_percent"] == pytest.approx(BATTERY / 100 / (1 - share), rel=0.02)
    assert attrs["groups_kwh_per_percent"]["> 7 kW"] == attrs["kwh_per_percent"]
    assert attrs["loss_low"]["phases"] == attrs["loss_high"]["phases"] == 3
    assert all(segment["soc_delta"] >= 5 for segment in attrs["segments"])


@pytest.mark.parametrize(("switch", "with_data", "measured_used"), [(True, True, True), (False, True, False), (True, False, False)])
def test_plan_uses_the_measured_loss_when_enabled(switch: bool, with_data: bool, measured_used: bool) -> None:
    """With use_measured_charging_loss the plan's % <-> kWh uses the measured loss at the max power."""
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings={"use_measured_charging_loss": switch})
    assert engine.run_job(snap, engine.startup, {}).error is None
    if with_data:
        soc, meter, power = _session(1_760_000_000.0, 11.04, 3, 3)
        assert engine.run_job(snap, engine.charging_loss_update, {"soc": soc, "meter": meter, "power": power}).error is None
    result = engine.run_job(snap, engine.recalculate)
    assert result.error is None, result.error
    engine._snap, engine._job_cache = snap, {}
    kwh = engine.percentage_to_kwh(100, include_charging_loss=True)
    engine._snap = None
    share = P0 / 11.04 + C3  # the simulated session is 3x16 A, the configured max power
    configured = abs(const.build_config(OPTIONS)["charger"]["charging_loss"])
    expected = BATTERY / (1 - share) if measured_used else BATTERY * (1 + configured)
    assert kwh == pytest.approx(expected, rel=0.02)
    attrs = result.attributes["charging_loss_model"]
    assert attrs["plan_uses_measured"] is measured_used
    assert attrs["kwh_per_percent_plan"] == pytest.approx(expected / 100, rel=0.02)


def test_plan_uses_the_loss_at_the_hours_power() -> None:
    """With a model (two power groups) a low power loses more: solar and a limited hour take more kWh per %."""
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings={"use_measured_charging_loss": True})
    assert engine.run_job(snap, engine.startup, {}).error is None
    start = 1_760_000_000.0
    for offset, (kw, phases, hours) in enumerate([(11.04, 3, 3), (4.14, 3, 4), (1.38, 1, 9), (2.3, 1, 6)]):
        soc, meter, power = _session(start + offset * 86400, kw, phases, hours)
        assert engine.run_job(snap, engine.charging_loss_update, {"soc": soc, "meter": meter, "power": power}).error is None
    engine._snap, engine._job_cache = snap, {}
    try:
        high, low = engine.charging_loss_factor(11.04), engine.charging_loss_factor(1.38)
        assert engine.charging_loss_factor() == pytest.approx(high)  # the default: the max power
        share = P0 / 1.38 + C1
        assert low == pytest.approx(share / (1 - share), abs=0.03)
        assert low > high
        # 1 % of the battery takes more kWh from the charger at 6 A on 1 phase than at 3x16 A
        assert engine.percentage_to_kwh(1, True, kw=1.38) > engine.percentage_to_kwh(1, True, kw=11.04)
    finally:
        engine._snap = None
