"""Charging that is not tied to a departure."""
from __future__ import annotations

import datetime
import logging

from ....utils.helpers import days_between, in_between
from ..charge_plan import FUNC_NAME

_LOGGER = logging.getLogger(__name__)


class FutureUnscheduled:
    """Cheap hours, fill up and the recommended full charge."""

    def unscheduled_planner(self, ctx, day):

        try:
            need_recommended_full_charge = ctx.days_need_between_recommended_full_charge != 0 and ctx.days_since_last_fully_charged > ctx.days_need_between_recommended_full_charge and day == 1

            if need_recommended_full_charge:
                _LOGGER.info(f"Days since last fully charged: {ctx.days_since_last_fully_charged}. Need full charge: {need_recommended_full_charge}")
            fill_up = self.fill_up_charging_enabled()
            cheap_grid_charging = self.cheap_grid_charging_enabled()

            if fill_up or need_recommended_full_charge or cheap_grid_charging:
                midnight_battery_level = sum(ctx.charging_plan[day]['battery_level_at_midnight'])

                # fill up: the floor (the day's driving + min daily level) in the cheapest hours,
                # the rest up to the max recommended level only in the cheap hours of the week
                fill_up_floor_kwh = 0.0
                fill_up_top_kwh = 0.0
                if fill_up:
                    fill_up_floor_kwh = self.kwh_needed_for_charging(ctx.charging_plan[day]['offday_battery_level_needed'] + self.get_min_daily_battery_level(), midnight_battery_level)
                    fill_up_top_kwh = self.kwh_needed_for_charging(self.get_max_recommended_charge_limit_battery_level(), midnight_battery_level)

                full_charge_kwh = 0.0
                waiting_for_full_charge_at_99 = (
                    self.battery_level() >= 99.0
                    and isinstance(self.full_charge_99_since, datetime.datetime)
                )
                if need_recommended_full_charge and not waiting_for_full_charge_at_99:
                    # from the day's own level (after the earlier days' charging and driving)
                    full_charge_kwh = self.kwh_needed_for_charging(100.0, midnight_battery_level)
                    _LOGGER.info(f"Needed for full charge: {round(full_charge_kwh,1)}kWh")

                if cheap_grid_charging:
                    # after the scheduled planner, so its charging counts
                    very_cheap_kwh_needed_today = self.kwh_needed_for_charging(ctx.very_cheap_max_battery_level, midnight_battery_level)
                    ultra_cheap_kwh_needed_today = self.kwh_needed_for_charging(ctx.ultra_cheap_max_battery_level, midnight_battery_level)
                else:
                    very_cheap_kwh_needed_today = ultra_cheap_kwh_needed_today = 0.0

                def kwh_still_needed():
                    return max(fill_up_floor_kwh, fill_up_top_kwh, full_charge_kwh, very_cheap_kwh_needed_today, ultra_cheap_kwh_needed_today)

                if kwh_still_needed() > 0.0:
                    for timestamp, price in ctx.sorted_by_cheapest_price:

                        if not in_between(timestamp, ctx.current_hour, ctx.last_charging + datetime.timedelta(hours=1)):
                            continue

                        timestamp = self.change_timestamp_with_minutes(ctx.charging_plan, timestamp)

                        hour_in_chargeHours, kwh_available = self.kwh_available_in_hour(ctx.chargeHours, timestamp)
                        if hour_in_chargeHours and not kwh_available:
                            continue

                        what_day = days_between(self.now(), timestamp)
                        if what_day < 0:
                            continue

                        # the full charge may go above the max recommended level (else the hour is refused at it)
                        full_charge_hour = full_charge_kwh > 0.01
                        battery_level_id, max_recommended_charge_limit_battery_level = self.what_battery_level(ctx, what_day, timestamp, price, day, max_level=100.0 if full_charge_hour else None)

                        if not battery_level_id:
                            continue

                        if full_charge_hour:
                            if not self.is_ev_configured(): #Sets battery level to 0.0 at midnight
                                battery_level_id = "battery_level_at_midnight"

                                '''if battery_level_id not in charging_plan[what_day]:
                                    battery_level_id = "battery_level_after_work"

                                if battery_level_id not in charging_plan[what_day]:
                                    battery_level_id = "battery_level_before_work"'''

                        very_cheap_price, ultra_cheap_price = self.cheap_price_check(ctx.cheap_average_price, ctx.grid_prices, price)

                        # 0.01: a cold battery may stop at 99%, don't keep asking the car for a tiny rest
                        if kwh_still_needed() <= 0.01:
                            break

                        full_charge_allowed = full_charge_hour and (very_cheap_price or ultra_cheap_price or not ctx.charging_plan['workday_in_week'])
                        if full_charge_allowed:
                            max_recommended_charge_limit_battery_level = 100.0 #Ignore solar over production
                        else:
                            # the other rules stay below the max recommended level
                            max_recommended_charge_limit_battery_level = min(max_recommended_charge_limit_battery_level, self.get_max_recommended_charge_limit_battery_level())

                        if full_charge_allowed:
                            kwh_needed = full_charge_kwh
                            hour_rules = ["battery_health"]
                        elif fill_up_floor_kwh > 0.01:
                            kwh_needed = fill_up_floor_kwh
                            hour_rules = ["fill_up"]
                        elif fill_up_top_kwh > 0.01 and self.fill_up_cheap_enough(ctx.fill_up_price_limits, ctx.grid_prices, timestamp, price):
                            kwh_needed = fill_up_top_kwh
                            hour_rules = ["fill_up"]
                        elif ultra_cheap_price and ultra_cheap_kwh_needed_today > 0.01:
                            kwh_needed = ultra_cheap_kwh_needed_today
                            hour_rules = []
                            max_recommended_charge_limit_battery_level = min(max_recommended_charge_limit_battery_level, ctx.ultra_cheap_max_battery_level)
                        elif very_cheap_price and very_cheap_kwh_needed_today > 0.01:
                            kwh_needed = very_cheap_kwh_needed_today
                            hour_rules = []
                            max_recommended_charge_limit_battery_level = min(max_recommended_charge_limit_battery_level, ctx.very_cheap_max_battery_level)
                        else:
                            continue

                        kwh_left, ctx.totalCost, ctx.totalkWh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.chargeHours, ctx.charging_plan, kwh_needed, ctx.totalCost, ctx.totalkWh, timestamp, price, very_cheap_price, ultra_cheap_price, kwh_available, sum(ctx.charging_plan[what_day][battery_level_id]), check_max_charging_plan={"day": day, "what_day": what_day, "battery_level_id": battery_level_id}, max_recommended_battery_level=max_recommended_charge_limit_battery_level, rules=hour_rules)
                        # the charged kWh count for every target level
                        kwh_added = self.percentage_to_kwh(battery_level_added, include_charging_loss=True)
                        fill_up_floor_kwh -= kwh_added
                        fill_up_top_kwh -= kwh_added
                        full_charge_kwh -= kwh_added
                        very_cheap_kwh_needed_today -= kwh_added
                        ultra_cheap_kwh_needed_today -= kwh_added
                        self.remove_solar_prediction_from_charge_hours(ctx.charging_plan, ctx.energy_prediction_db, timestamp, day, battery_level_added)

                        if timestamp in ctx.chargeHours and battery_level_added:
                            charging_sessions_id = self.add_charging_session_to_day(ctx, timestamp, what_day, battery_level_id)
                            self.add_charging_to_days(ctx, day, what_day, charging_sessions_id, battery_level_added)
                            if ctx.charging_plan[day]['offday']:
                                ctx.charging_plan[day]['offday_total_cost'] += cost_added
        except Exception as e:
            error_message = f"Error in fill up or need recommended full charge for day {day}: {e} {type(e)}"
            _LOGGER.warning(error_message)

            self.persistent_notification(error_message, f"{self.title} error", notification_id=f"{self.name}_{FUNC_NAME}_{ctx.sub_func_name}_fill_up_or_need_recommended_full_charge_error")
