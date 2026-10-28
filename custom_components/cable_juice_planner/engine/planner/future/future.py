"""The future charging plan: all rules that add charge hours."""
from __future__ import annotations

import datetime
import logging

from ....utils.helpers import days_between, end_of_day, reset_datetime
from .alternate import FutureAlternate
from .context import FutureContext
from .departure import FutureDeparture
from .scheduled import FutureScheduled
from .solar import FutureSolar
from .support import FutureSupport
from .unscheduled import FutureUnscheduled

_LOGGER = logging.getLogger(__name__)


class FutureCharging(FutureSupport, FutureScheduled, FutureUnscheduled, FutureDeparture, FutureAlternate, FutureSolar):
    """The future charging plan: all rules that add charge hours."""

    def future_charging(self, battery_expenses, chargeHours, charge_hours_alternative, charging_plan, cheap_average_price, current_hour, energy_prediction_db, fill_up_price_limits, grid_prices, now, solar_over_production, sorted_by_cheapest_price, trip_date_time, trip_homecoming_date_time, trip_range, work_overview, totalCost, totalkWh):
        ctx = FutureContext(
            battery_expenses=battery_expenses,
            chargeHours=chargeHours,
            charge_hours_alternative=charge_hours_alternative,
            charging_plan=charging_plan,
            cheap_average_price=cheap_average_price,
            current_hour=current_hour,
            energy_prediction_db=energy_prediction_db,
            fill_up_price_limits=fill_up_price_limits,
            grid_prices=grid_prices,
            now=now,
            solar_over_production=solar_over_production,
            sorted_by_cheapest_price=sorted_by_cheapest_price,
            trip_date_time=trip_date_time,
            trip_homecoming_date_time=trip_homecoming_date_time,
            trip_range=trip_range,
            work_overview=work_overview,
            totalCost=totalCost,
            totalkWh=totalkWh,
        )

        ctx.sub_func_name = "future_charging"

        ctx.unused_solar_kwh = {}
        ctx.unused_solar_cost = {}
        ctx.solar_unit = self.get_solar_sell_price()

        ctx.ignored_reference_battery_level = 0.0
        ctx.very_cheap_max_battery_level = self.get_very_cheap_grid_charging_max_battery_level()
        ctx.ultra_cheap_max_battery_level = self.get_ultra_cheap_grid_charging_max_battery_level()

        ctx.days_need_between_recommended_full_charge = self.number_state("full_charge_recommended", error_state=0.0)
        last_full_charge = self.sensor_states.get("last_full_charge")
        ctx.days_since_last_fully_charged = days_between(last_full_charge if isinstance(last_full_charge, datetime.datetime) else reset_datetime(), self.now())
        ctx.charger_status = self.get_state(self.config['charger']['entity_ids']['status_entity_id'], error_state="unavailable")

        for day in sorted([key for key in charging_plan.keys() if isinstance(key, int)]):
            ctx.day_before = max(day - 1, 0)
            ctx.day_after = min(day + 1, 7)
            ctx.last_charging = end_of_day(self.time_plus_days(day))

            starting_battery_level = min(sum(charging_plan[ctx.day_before]['battery_level_at_midnight']), 100.0) if day > 0 else self.battery_level()

            if starting_battery_level < 0.0:
                correction_percentage = abs(starting_battery_level)
                starting_battery_level += correction_percentage
                _LOGGER.warning(f"Starting battery level under 0% ({round(correction_percentage * -1, 1)}%) adding {round(correction_percentage, 1)}%")
            charging_plan[day]['battery_level_before_work'].append(starting_battery_level)
            charging_plan[day]['battery_level_after_work'].append(starting_battery_level)

            trip_battery_needed = 0.0

            if charging_plan[day]['trip']:
                trip_battery_needed = charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max']

                if charging_plan[day]['workday'] and charging_plan[day]['trip_goto'] >= charging_plan[day]['work_goto']:
                    reference_battery_level = sum(charging_plan[day]['battery_level_after_work']) - charging_plan[day]['work_battery_level_needed']
                else:
                    reference_battery_level = sum(charging_plan[day]['battery_level_before_work'])

                charging_plan[day]['trip_kwh_needed'] = max(self.kwh_needed_for_charging(trip_battery_needed, reference_battery_level), 0.0)

            _LOGGER.debug(f"---------------------------------{day} {charging_plan[day]['day_text']} {charging_plan[day]['work_goto']} {day}---------------------------------")

            if charging_plan[day]['workday']:
                charging_plan[day]['battery_level_after_work'].append(charging_plan[day]['work_battery_level_needed'] * -1)

            charging_plan[day]['battery_level_at_midnight'].append(sum(charging_plan[day]['battery_level_after_work']))

            battery_level_at_midnight_with_solar = sum(charging_plan[day]['battery_level_at_midnight']) + sum(charging_plan[day]['solar_prediction'])
            max_allowed_battery_level = max(self.get_max_recommended_charge_limit_battery_level(), trip_battery_needed)

            if battery_level_at_midnight_with_solar > max_allowed_battery_level:
                charging_plan[day]['solar_prediction_corrected'] = True
                correction_percentage = max_allowed_battery_level - battery_level_at_midnight_with_solar
                charging_plan[day]['solar_prediction'].append(correction_percentage)
                charging_plan[day]['solar_kwh_prediction'].append(self.percentage_to_kwh(correction_percentage, include_charging_loss=True))
                _LOGGER.warning(f"Battery level on day {day} at midnight {battery_level_at_midnight_with_solar}% is above max allowed {max_allowed_battery_level}%, correcting solar prediction with {correction_percentage}%")

            charging_plan[day]['battery_level_at_midnight'].append(sum(charging_plan[day]['solar_prediction']))

            try:  # a task in ev.py: its exception was never read
                self.scheduled_planner(ctx, day)
            except Exception:  # noqa: BLE001
                pass

            if self.solar_charging_enabled():
                if day > 0:
                    ctx.unused_solar_kwh[self.time_plus_days(day).date()] = sum(charging_plan[ctx.day_before]['solar_kwh_prediction'])
                    ctx.unused_solar_cost[self.time_plus_days(day).date()] = sum(charging_plan[ctx.day_before]['solar_cost_prediction'])

            self.unscheduled_planner(ctx, day)

            self.departure_planner(ctx, day)
            self.alternate_charging_planner(ctx, day)
            self.solar_prediction_planner(ctx, day)

            if charging_plan[day]['trip']:
                total_trip_battery_level_needed = max((charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max'] - self.get_min_trip_battery_level()), 0.0) * -1
                charging_plan[day]['battery_level_at_midnight'].append(total_trip_battery_level_needed)

            if charging_plan[day]['offday']:
                charging_plan[day]['battery_level_at_midnight'].append(charging_plan[day]['offday_battery_level_needed'] * -1)
        #_LOGGER.error(f"charge_hours_alternative:\n{pformat(charge_hours_alternative, width=200, compact=True)}")
        return [ctx.totalCost, ctx.totalkWh]
