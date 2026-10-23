"""Load balancing between the cars of a site (shared grid connection).

Only active when a site config entry exists; the snapshot then carries ``site``:

* ``hour_capacity``: hour (naive local) -> amps available per *charger* phase for this
  car in that hour (fuse - forecast house consumption - reservations of the cars ahead)
* ``live``: amps available per charger phase right now
* ``urgent_hours``: a trip within this many hours puts the car first

The planner asks :meth:`hour_kwh_capacity` instead of using the charger maximum, so an
hour with less capacity is charged with fewer amps and the rest is planned in other
hours. :meth:`site_clamp` limits every amps decision to the live capacity (1 phase or
stop when not even the minimum is available). Without a site nothing changes.
"""
from __future__ import annotations

import datetime
import logging

from .helpers import hours_between, reset_datetime, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


class SiteMixin:
    """Mixed into CJPEngine."""

    def site_active(self) -> bool:
        return bool(self._snap is not None and self._snap.site)

    def _site(self) -> dict:
        return self._snap.site if self.site_active() else {}

    # ------------------------------------------------------------------ #
    # Capacity -> amps
    # ------------------------------------------------------------------ #

    def _site_amps(self, capacity, max_watt: float | None = None) -> tuple[int, int]:
        """(amps, phases) for the most power within capacity (amps per charger phase).

        Uses the same watt -> amps/phases table as the solar charging (charging_power_dict),
        so it switches between 1 phase and all phases with the configured min/max amps.
        """
        capacity = [max(float(value), 0.0) for value in capacity] + [0.0] * 3
        power_dict = self.charging_power_dict()
        for watt in sorted(power_dict, reverse=True):
            entry = power_dict[watt]
            if max_watt is not None and watt > max_watt + 0.5:
                continue
            amps, phases = entry["amp"], int(entry["phase"])
            if amps > 0 and all(capacity[i] >= amps for i in range(phases)):
                return int(amps), phases
        return 0, int(self.config["charger"]["charging_phases"])

    def hour_site_amps(self, hour: datetime.datetime) -> tuple[int, int] | None:
        """(amps, phases) for a planned hour, None without a site (charger maximum)."""
        if not self.site_active():
            return None
        capacity = self._site().get("hour_capacity", {}).get(reset_time_to_hour(hour))
        if capacity is None:
            return None
        return self._site_amps(capacity)

    def hour_kwh_capacity(self, hour: datetime.datetime) -> float:
        """kWh the charger can charge in a full hour (the planner's max_kwh_charging)."""
        site = self.hour_site_amps(hour)
        if site is None:
            return self.max_kwh_charging
        amps, phases = site
        return amps * self.config["charger"]["power_voltage"] * phases / 1000

    def hour_charging_amps(self, hour: datetime.datetime):
        site = self.hour_site_amps(hour)
        return self.config["charger"]["charging_max_amp"] if site is None else site[0]

    # ------------------------------------------------------------------ #
    # Live limit
    # ------------------------------------------------------------------ #

    def site_clamp(self, amps: list) -> list:
        """Limit requested amps per charger phase to the live capacity of the site."""
        live = self._site().get("live")
        if live is None or sum(amps) <= 0:
            return list(amps)
        # the most power within the live capacity, never more than requested
        requested_watt = sum(float(a) for a in amps) * self.config["charger"]["power_voltage"]
        allowed, phases = self._site_amps(live, max_watt=requested_watt)
        if allowed <= 0:
            return [0, 0, 0]
        return [allowed if i < phases else 0 for i in range(3)]

    def site_text(self, requested: list, limited: list) -> str:
        if not self.site_active() or [int(a) for a in requested] == [int(a) for a in limited]:
            return ""
        if sum(limited) <= 0:
            return self.t("ui.site.waiting")
        phases = sum(1 for value in limited if value > 0)
        return self.t("ui.site.limited", amps=int(max(limited)), phases=phases)

    # ------------------------------------------------------------------ #
    # What the site needs from this car
    # ------------------------------------------------------------------ #

    def site_urgent(self) -> bool:
        """Battery under the daily minimum or a trip soon: this car goes first."""
        try:
            if self.battery_level() < self.get_min_daily_battery_level():
                return True
            if self.is_trip_planned():
                trip = self.get_trip_date_time()
                if trip != reset_datetime() and 0 <= hours_between(self.now(), trip) <= float(self._site().get("urgent_hours", 12)):
                    return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("site_urgent: %s", err)
        return False

    def site_reservation(self) -> dict:
        """hour -> amps per charger phase of the planned charging hours."""
        reservation = {}
        for hour, data in (self.charge_hours or {}).items():
            if not isinstance(hour, datetime.datetime) or not isinstance(data, dict) or float(data.get("kWh", 0.0)) <= 0.0:
                continue
            site = self.hour_site_amps(hour)
            if site is None:
                amps, phases = int(self.config["charger"]["charging_max_amp"]), int(self.config["charger"]["charging_phases"])
            else:
                amps, phases = site
            if amps > 0:
                reservation[reset_time_to_hour(hour)] = [amps if i < phases else 0 for i in range(3)]
        return reservation

    def publish_site(self) -> None:
        if self.site_active() and self._out is not None:
            self._out.site = {
                "reservation": self.site_reservation(),
                "request": [int(a) for a in self.current_charging_amps] if self.charger_action == "charge" else [0, 0, 0],
                "urgent": self.site_urgent(),
            }

    def site_limit(self) -> None:
        """Job: the site is overloaded or capacity changed, apply the live limit again."""
        if self.charger_action == "charge":
            self.set_charger_phase_limits(self.requested_circuit_amps, self.requested_charger_amps)
        self.publish_site()
