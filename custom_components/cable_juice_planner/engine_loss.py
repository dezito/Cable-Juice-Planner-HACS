"""Charging loss per charging power, measured continuously (engine mixin).

A segment runs from one change of the car's battery level to a later one (e.g. 55 % ->
60 %), so the battery side is exact: the level changes by whole percents. The energy
from the charger is the kWh meter, interpolated to the times of those changes. A
segment is only used while charging power stays above the minimum and phases stay the
same, and only once both the meter and the battery level have reported after it.

Older charging (beyond the recorder's raw history) comes from the hourly long-term
statistics: runs of hours with charging power throughout. Variable charging power is
expected when solar surplus controls the charger. These measurements count half.

The model, fitted over all segments:

    lost kWh = P0 * hours + c(phases) * kWh from the charger

P0: what the car uses while it charges (electronics, battery management), the same for
1 and 3 phases. c: the loss of the car's charger. The share lost at a power P is
P0 / P + c, so a low power (solar on 1 phase) loses the most. kWh per % uses the
configured battery size; a wrong size only moves the loss, the plan stays consistent.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

LOSS_GROUPS = ((0.0, 2.5, "< 2,5 kW"), (2.5, 4.0, "2,5-4 kW"), (4.0, 7.0, "4-7 kW"), (7.0, 100.0, "> 7 kW"))
LOSS_GROUP_SIZE = 10  # newest segments kept per power group
LOSS_MIN_SOC_DELTA = 5.0  # % per segment: the car reports its level with a delay, short segments are noisy
LOSS_MAX_SEGMENT_HOURS = 8.0
LOSS_MIN_POWER = 1000.0  # W: below this it is not charging
LOSS_METER_MAX_GAP = 20 * 60  # s between the meter readings around a change of the battery level
LOSS_POWER_MAX_GAP = 15 * 60  # s without a power value inside a segment
LOSS_STATISTICS_WEIGHT = 0.5
LOSS_SEGMENT_VERSION = 2
LOSS_SHARE_LIMITS = (-0.3, 0.6)  # a wrong battery size moves the loss, so the limits are wide
CAPACITY_MONTHS = 36
LOSS_THREE_PHASE_MIN_KW = 3.8  # above the most 1 phase can give (16 A * 230 V = 3.7 kW)

Series = list[tuple[float, float]]  # (timestamp, value)


def _group(kw: float) -> str:
    return next(name for low, high, name in LOSS_GROUPS if low <= kw < high)


def _value_at(series: Series, ts: float, max_gap: float) -> float | None:
    """Linear interpolation between the readings around ts (None when they are too far apart)."""
    for (t1, v1), (t2, v2) in zip(series, series[1:]):
        if t1 <= ts <= t2:
            if t2 - t1 > max_gap and ts not in (t1, t2):
                return None
            return v1 if t2 == t1 else v1 + (v2 - v1) * (ts - t1) / (t2 - t1)
    return None


def _state_at(series: Series, ts: float) -> float | None:
    """The value in force at ts (a step series)."""
    value = None
    for t, v in series:
        if t > ts:
            break
        value = v
    return value


def _phase_count(phase_series: list[Series], ts_from: float, ts_to: float) -> int | None:
    """Phases with current during [ts_from, ts_to], None when unknown or changing."""
    if not phase_series or not all(phase_series):
        return None
    counts = set()
    times = sorted({ts_from, *(t for series in phase_series for t, _ in series if ts_from < t < ts_to)})
    for ts in times:
        values = [_state_at(series, ts) for series in phase_series]
        if any(v is None for v in values):
            return None
        counts.add(sum(1 for v in values if v > 0))
    return counts.pop() if len(counts) == 1 else None


def _infer_phases(kw: float, configured: int) -> int:
    return 3 if configured >= 3 and kw > LOSS_THREE_PHASE_MIN_KW else 1


def _power_continuous(power: list[tuple[float, float, float, float]], ts_from: float, ts_to: float) -> bool:
    """power: (start, mean, min, max) of 5 minute statistics or of single readings."""
    inside = [row for row in power if ts_from - 300 < row[0] < ts_to]
    if not inside:
        return False
    starts = [ts_from, *(row[0] for row in inside if row[0] > ts_from), ts_to]
    if max(b - a for a, b in zip(starts, starts[1:])) > LOSS_POWER_MAX_GAP:
        return False
    return all(row[1] >= LOSS_MIN_POWER for row in inside)


def _segment(start: float, end: float, kwh_in: float, soc_delta: float, battery_size: float, phases: int, source: str) -> dict | None:
    hours = (end - start) / 3600
    if kwh_in <= 0.0 or hours <= 0.0:
        return None
    kwh_battery = battery_size * soc_delta / 100
    share = 1 - kwh_battery / kwh_in
    if not LOSS_SHARE_LIMITS[0] <= share <= LOSS_SHARE_LIMITS[1]:
        return None
    kw = kwh_in / hours
    return {
        "start": start,
        "end": end,
        "hours": round(hours, 4),
        "kwh": round(kwh_in, 3),
        "kwh_battery": round(kwh_battery, 3),
        "soc_delta": round(soc_delta, 1),
        "kw": round(kw, 3),
        "phases": phases,
        "loss": round(share, 4),
        "source": source,
        "group": _group(kw),
    }


def find_segments(soc: Series, meter: Series, power, phase_series: list[Series], battery_size: float, configured_phases: int, source: str = "history") -> list[dict]:
    """Segments from changes of the battery level in raw history (sorted series)."""
    # only real changes are anchors: the first reading is the level from before
    ticks: Series = [(ts, value) for (_, previous), (ts, value) in zip(soc, soc[1:]) if value != previous]
    last_meter = meter[-1][0] if meter else 0.0
    last_soc = soc[-1][0] if soc else 0.0
    segments = []
    i = 0
    while i < len(ticks) - 1:
        t_a, v_a = ticks[i]
        kwh_a = _value_at(meter, t_a, LOSS_METER_MAX_GAP)
        best = None
        for j in range(i + 1, len(ticks)):
            t_b, v_b = ticks[j]
            if v_b <= ticks[j - 1][1] or t_b - t_a > LOSS_MAX_SEGMENT_HOURS * 3600:
                break
            # both entities must have reported after the end of the segment
            if t_b >= last_meter or t_b >= last_soc:
                break
            if kwh_a is None or not _power_continuous(power, t_a, t_b):
                break
            kwh_b = _value_at(meter, t_b, LOSS_METER_MAX_GAP)
            if kwh_b is not None and kwh_b > kwh_a:
                best = (j, kwh_b)
        if best is None:
            i += 1
            continue
        j, kwh_b = best
        t_b, v_b = ticks[j]
        if v_b - v_a >= LOSS_MIN_SOC_DELTA:
            phases = _phase_count(phase_series, t_a, t_b)
            if phases is None or phases == 0:
                phases = _infer_phases((kwh_b - kwh_a) / ((t_b - t_a) / 3600), configured_phases)
            if (segment := _segment(t_a, t_b, kwh_b - kwh_a, v_b - v_a, battery_size, phases, source)) is not None:
                segments.append(segment)
            i = j
        else:
            i += 1
    return segments


def find_statistics_segments(hours: list[dict], battery_size: float, configured_phases: int) -> list[dict]:
    """Segments from runs of hours with charging power throughout (hourly statistics).

    hours: {"start", "power_mean", "power_min", "power_max", "kwh" (meter change), "soc" (mean)}
    The mean battery level is the level at the middle of the hour, so from the middle of the
    first to the middle of the last hour: half of the first and last hour's kWh plus the rest.
    """
    def charging(hour):
        mean = hour.get("power_mean") or 0.0
        return (
            mean >= LOSS_MIN_POWER
            and (hour.get("kwh") or 0.0) > 0.0 and hour.get("soc") is not None
        )

    segments = []
    run: list[dict] = []
    for hour in [*sorted(hours, key=lambda h: h["start"]), None]:
        same = (
            hour is not None and charging(hour) and run and hour["start"] - run[-1]["start"] == 3600
        )
        if same:
            run.append(hour)
            continue
        if len(run) >= 3:
            soc_delta = run[-1]["soc"] - run[0]["soc"]
            kwh = run[0]["kwh"] / 2 + sum(h["kwh"] for h in run[1:-1]) + run[-1]["kwh"] / 2
            start, end = run[0]["start"] + 1800, run[-1]["start"] + 1800
            if soc_delta >= LOSS_MIN_SOC_DELTA:
                phases = _infer_phases(kwh / ((end - start) / 3600), configured_phases)
                if (segment := _segment(start, end, kwh, soc_delta, battery_size, phases, "statistics")) is not None:
                    segments.append(segment)
        run = [hour] if hour is not None and charging(hour) else []
    return segments


def merge_segments(old: list[dict], new: list[dict]) -> list[dict]:
    """Add new segments (the same charging found again is skipped), newest LOSS_GROUP_SIZE per group."""
    merged = list(old)
    for segment in new:
        if not any(seg["start"] < segment["end"] and segment["start"] < seg["end"] for seg in merged):
            merged.append(segment)
    kept = []
    for _, _, name in LOSS_GROUPS:
        group = sorted((s for s in merged if s["group"] == name), key=lambda s: s["start"], reverse=True)
        kept += group[:LOSS_GROUP_SIZE]
    return sorted(kept, key=lambda s: s["start"])


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination for the small normal equations (None when singular)."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-9:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col:
                factor = a[r][col] / a[col][col]
                a[r] = [x - factor * y for x, y in zip(a[r], a[col])]
    return [a[i][n] / a[i][i] for i in range(n)]


def fit_model(segments: list[dict], fallback_share: float) -> dict:
    """P0 (kW) and c per phase count by weighted least squares over all segments.

    status: no_data (the configured loss), one_group (one measured loss for every power)
    or model (P0 and c fitted, needs segments in at least two power groups).
    """
    if not segments:
        return {"status": "no_data", "p0": 0.0, "c": {1: fallback_share, 3: fallback_share}}
    weight = lambda s: LOSS_STATISTICS_WEIGHT if s["source"] == "statistics" else 1.0  # noqa: E731
    lost = lambda s: s["kwh"] - s["kwh_battery"]  # noqa: E731
    if len({s["group"] for s in segments}) < 2:
        share = sum(weight(s) * lost(s) for s in segments) / sum(weight(s) * s["kwh"] for s in segments)
        return {"status": "one_group", "p0": 0.0, "c": {1: share, 3: share}}

    phases = sorted({s["phases"] for s in segments})

    def solve(with_p0: bool):
        columns = (["p0"] if with_p0 else []) + phases
        rows = []
        for s in segments:
            features = ([s["hours"]] if with_p0 else []) + [s["kwh"] if s["phases"] == ph else 0.0 for ph in phases]
            rows.append((features, lost(s), weight(s)))
        size = len(columns)
        matrix = [[sum(w * f[i] * f[j] for f, _, w in rows) for j in range(size)] for i in range(size)]
        vector = [sum(w * f[i] * y for f, y, w in rows) for i in range(size)]
        result = _solve(matrix, vector)
        return None if result is None else dict(zip(columns, result))

    result = solve(True)
    if result is None or result["p0"] < 0.0:
        # no fixed share in the data (or not separable): the loss is only the charger's
        result = solve(False) or {}
        result["p0"] = 0.0
    c = {ph: max(result.get(ph, 0.0), 0.0) for ph in phases}
    for ph in (1, 3):
        c.setdefault(ph, c[phases[0]])
    return {"status": "model", "p0": max(result["p0"], 0.0), "c": c}


def loss_share(model: dict, kw: float, phases: int) -> float:
    """The share of the energy from the charger that is lost at kw."""
    return (model["p0"] / kw if kw > 0 else 0.0) + model["c"][3 if phases >= 3 else 1]


class LossMixin:
    """The measured charging loss per power (database charging_loss)."""

    def load_charging_loss_db(self, database) -> None:
        self.charging_loss_db = database if isinstance(database, dict) and isinstance(database.get("segments"), list) else {"segments": [], "backfilled": False}
        if self.charging_loss_db.get("segment_version") != LOSS_SEGMENT_VERSION:
            self.charging_loss_db.update(segments=[], backfilled=False, segment_version=LOSS_SEGMENT_VERSION)
            self.mark_dirty("charging_loss")

    def _fallback_loss_share(self) -> float:
        loss = abs(self.config["charger"]["charging_loss"])  # kWh from the charger = kWh in the car * (1 + loss)
        return loss / (1 + loss)

    def charging_loss_update(self, data: dict) -> dict | None:
        """data from the coordinator: raw history (soc, meter, power, phases) and/or hourly statistics.

        Returns the segments per power group, so the coordinator knows which groups still
        need older statistics. backfill=True marks the history as read.
        """
        if not self.is_ev_configured():
            return None
        battery_size = float(self.config["ev_car"]["battery_size"])
        configured = int(self.config["charger"]["charging_phases"])
        found = []
        if data.get("soc") and data.get("meter"):
            found += find_segments(data["soc"], data["meter"], data.get("power") or [], data.get("phases") or [], battery_size, configured, data.get("source", "history"))
        if data.get("hours"):
            found += find_statistics_segments(data["hours"], battery_size, configured)
        segments = merge_segments(self.charging_loss_db.get("segments", []), found)
        # every segment counts per month, also those not kept: kWh per % over time (battery capacity)
        months = self.charging_loss_db.setdefault("months", {})
        for segment in found:
            month = datetime.datetime.fromtimestamp(segment["start"], self._snap.tz).strftime("%Y-%m")
            entry = months.setdefault(month, {}).setdefault(segment["group"], {"kwh": 0.0, "soc": 0.0, "starts": []})
            if segment["start"] not in entry["starts"]:
                entry["starts"].append(segment["start"])
                entry["kwh"] = round(entry["kwh"] + segment["kwh"], 3)
                entry["soc"] = round(entry["soc"] + segment["soc_delta"], 1)
                self.mark_dirty("charging_loss")
        for month in sorted(months)[:-CAPACITY_MONTHS]:
            del months[month]
        changed = segments != self.charging_loss_db.get("segments")
        if changed or data.get("backfill"):
            self.charging_loss_db["segments"] = segments
            if data.get("backfill"):
                self.charging_loss_db["backfilled"] = True
            self.mark_dirty("charging_loss")
        _LOGGER.debug("Charging loss: %s new segments, %s kept", len(found), len(segments))
        self.publish_charging_loss_model()
        return {name: sum(1 for s in segments if s["group"] == name) for _, _, name in LOSS_GROUPS}

    def charging_loss_reset(self) -> None:
        """button recalculate_charging_loss: forget the segments, the coordinator reads the history again."""
        self.charging_loss_db = {"segments": [], "backfilled": False, "segment_version": LOSS_SEGMENT_VERSION}
        self.mark_dirty("charging_loss")
        self.publish_charging_loss_model()

    def publish_charging_loss_model(self) -> None:
        """The sensor: the loss at the max charging power, kWh from the charger per 1 % (what the
        plan needs, independent of the battery size), the curves and the measurements."""
        if not self.is_ev_configured():
            return
        segments = self.charging_loss_db.get("segments", [])
        model = fit_model(segments, self._fallback_loss_share())
        voltage = float(self.config["charger"]["power_voltage"])
        max_amps = int(self.config["charger"]["charging_max_amp"])
        configured = int(self.config["charger"]["charging_phases"])
        battery_size = float(self.config["ev_car"]["battery_size"])
        measured_groups = {s["group"] for s in segments}
        measured_phases = {3 if s["phases"] >= 3 else 1 for s in segments}

        def per_percent(share: float) -> float:
            return battery_size / 100 / (1 - share) if share < 1 else 0.0

        curve: dict[str, list] = {}
        # without data the configured loss (one flat line), with one group only where it was measured,
        # with a model for every phase count that has measurements
        phase_counts = sorted({configured}) if model["status"] == "no_data" else sorted(measured_phases)
        for phases in phase_counts:
            points = []
            for amps in range(6, max(max_amps, 6) + 1):
                kw = amps * voltage * phases / 1000
                if model["status"] == "one_group" and _group(kw) not in measured_groups:
                    continue
                fixed = model["p0"] / kw
                loss = fixed + model["c"][phases]
                points.append({
                    "amps": amps, "phases": phases, "kw": round(kw, 3), "loss": round(loss, 4), "fixed": round(fixed, 4),
                    "charger": round(model["c"][phases], 4), "kwh_per_percent": round(per_percent(loss), 4),
                })
            if points:
                curve[str(phases)] = points
        all_points = sorted((p for points in curve.values() for p in points), key=lambda p: p["kw"])
        low, top = (all_points[0], all_points[-1]) if all_points else (None, None)

        # measured: kWh from the charger per 1 % of the battery level
        def measured(group_segments):
            soc = sum(s["soc_delta"] for s in group_segments)
            return round(sum(s["kwh"] for s in group_segments) / soc, 4) if soc else None

        local = lambda ts: datetime.datetime.fromtimestamp(ts, self._snap.tz).isoformat(timespec="minutes")  # noqa: E731
        self.set_state("charging_loss_model", round(top["loss"] * 100, 1) if top else None)
        attrs = {
            "status": model["status"],
            "kwh_per_percent": measured(segments),
            "kwh_per_percent_configured": round(per_percent(self._fallback_loss_share()), 4),
            # what the plan uses now: the configured or (use_measured_charging_loss) the measured loss
            "plan_uses_measured": self.switch_state("use_measured_charging_loss", error_state="off") == "on" and model["status"] != "no_data",
            "kwh_per_percent_plan": round(battery_size / 100 * (1 + self.charging_loss_factor()), 4),
            "fixed_kw": round(model["p0"], 3),
            "charger_loss_1_phase": round(model["c"][1], 4) if 1 in measured_phases else None,
            "charger_loss_3_phases": round(model["c"][3], 4) if 3 in measured_phases else None,
            "configured_loss": round(self._fallback_loss_share(), 4),
            "battery_size": battery_size,
            "loss_low": low,
            "loss_high": top,
            "groups": {name: sum(1 for s in segments if s["group"] == name) for _, _, name in LOSS_GROUPS},
            "groups_kwh_per_percent": {name: measured([s for s in segments if s["group"] == name]) for _, _, name in LOSS_GROUPS},
            "segments": [
                {
                    "time": local(s["start"]), "kw": s["kw"], "loss": s["loss"], "phases": s["phases"], "soc_delta": s["soc_delta"],
                    "kwh": s["kwh"], "kwh_per_percent": round(s["kwh"] / s["soc_delta"], 4), "source": s["source"],
                }
                for s in segments
            ],
            "curve": curve,
            # kWh per % per month and group: rising over the years = less battery capacity
            "capacity_months": [
                {"month": month, "group": group, "kwh_per_percent": round(v["kwh"] / v["soc"], 4), "segments": len(v["starts"])}
                for month, groups in sorted(self.charging_loss_db.get("months", {}).items())
                for group, v in groups.items() if v["soc"]
            ],
            "backfilled": bool(self.charging_loss_db.get("backfilled")),
            # how far back each group had to go for its measurements
            "group_oldest": {
                name: local(min(s["start"] for s in group)) if (group := [s for s in segments if s["group"] == name]) else None
                for _, _, name in LOSS_GROUPS
            },
        }
        for key, value in attrs.items():
            self.set_attr("charging_loss_model", key, value)

    def measured_charging_loss_factor(self, kw: float | None = None) -> float | None:
        """The measured loss as kWh from the charger / kWh in the car - 1 (None without measurements).

        At kw when the model knows more than one power (one group: the measured loss at any
        power), else at the max charging power, which the planned grid charging uses.
        """
        segments = self.charging_loss_db.get("segments", [])
        model = fit_model(segments, self._fallback_loss_share())
        if model["status"] == "no_data":
            return None
        configured = int(self.config["charger"]["charging_phases"])
        if kw is None or kw <= 0 or model["status"] != "model":
            kw = int(self.config["charger"]["charging_max_amp"]) * float(self.config["charger"]["power_voltage"]) * configured / 1000
        share = loss_share(model, kw, 3 if configured >= 3 and kw > LOSS_THREE_PHASE_MIN_KW else 1)
        # may be a little negative when the battery size is set too high: the plan still fits
        return share / (1 - share) if share < 1 else None

    def charging_loss_backfilled(self) -> bool:
        return bool(self.charging_loss_db.get("backfilled"))


def resample_steps(series: Series, start: float, end: float, step: float = 300.0) -> list[tuple[float, float, float, float]]:
    """(start, mean, min, max) per step from readings that only come on a change (the value holds until the next)."""
    rows = []
    t = start - start % step
    index = 0
    current = None
    while t < end:
        values = []
        while index < len(series) and series[index][0] <= t:
            current = series[index][1]
            index += 1
        if current is not None:
            values.append(current)
        look = index
        while look < len(series) and series[look][0] < t + step:
            values.append(series[look][1])
            look += 1
        if values:
            rows.append((t, sum(values) / len(values), min(values), max(values)))
        t += step
    return rows


def as_series(rows: Any) -> Series:
    """(timestamp, float) pairs from recorder rows, unknown values skipped."""
    result = []
    for ts, value in rows:
        try:
            result.append((float(ts), float(value)))
        except (TypeError, ValueError):
            continue
    return sorted(result)
