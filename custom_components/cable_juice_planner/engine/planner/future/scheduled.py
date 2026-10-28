"""Charging for the scheduled work and trip departures."""
from __future__ import annotations

import datetime
import logging

from ....utils.helpers import days_between, hours_between, in_between
from ..charge_plan import FUNC_NAME

_LOGGER = logging.getLogger(__name__)


class FutureScheduled:
    """Scheduled charging before work and trips."""

    def battery_level_full_on_next_departure(self, ctx, what_day):
        sub_sub_func_name = "battery_level_full_on_next_departure"

        try:
            if what_day == 7:
                return False

            for day in range(what_day + 1,8):
                if ctx.charging_plan[day]['trip']:
                    return False

                if ctx.charging_plan[day]['workday']:
                    if round(sum(ctx.charging_plan[day]["battery_level_before_work"]),0) >= self.get_max_recommended_charge_limit_battery_level():
                        return True
                    break
        except Exception as e:
            _LOGGER.error(f"Error in battery_level_full_on_next_departure what_day:{what_day}: {e} {type(e)}")
            self.persistent_notification(
                f"Error in battery_level_full_on_next_departure what_day:{what_day}: {e} {type(e)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_{FUNC_NAME}_{ctx.sub_func_name}_{sub_sub_func_name}_error"
            )

        return False

    def scheduled_charging_added(self, ctx, day, what_day, timestamp, battery_level_id, battery_level_added, cost_added):
        """Book an hour added by scheduled_planner: solar, the cost of trip/work and the battery levels."""
        self.remove_solar_prediction_from_charge_hours(ctx.charging_plan, ctx.energy_prediction_db, timestamp, day, battery_level_added)

        total_trip_battery_level_needed = ctx.charging_plan[day]['trip_battery_level_needed'] + ctx.charging_plan[day]['trip_battery_level_above_max']
        battery_level_sum = total_trip_battery_level_needed + ctx.charging_plan[day]['work_battery_level_needed']

        if battery_level_sum > 0.0:
            if "trip" in ctx.charging_plan[day]['rules']:
                cost_trip = (total_trip_battery_level_needed / battery_level_sum) * cost_added
                ctx.charging_plan[day]['trip_total_cost'] += cost_trip

            for rule in ctx.charging_plan[day].get('rules', []):
                if 'workday_preparation' in rule:
                    cost_work = (ctx.charging_plan[day]['work_battery_level_needed'] / battery_level_sum) * cost_added
                    ctx.charging_plan[day]['work_total_cost'] += cost_work
                    break

        charging_sessions_id = self.add_charging_session_to_day(ctx, timestamp, what_day, battery_level_id)
        self.add_charging_to_days(ctx, day, what_day, charging_sessions_id, battery_level_added)

    def scheduled_planner(self, ctx, day):
        sub_sub_func_name = "scheduled_planner"

        try:
            if ctx.charging_plan[day]['workday'] or ctx.charging_plan[day]['trip']:
                if ctx.charging_plan[day]['workday']:
                    ctx.last_charging = ctx.charging_plan[day]['work_last_charging']
                    next_departure = ctx.charging_plan[day]['work_goto']

                if ctx.charging_plan[day]['trip']:
                    ctx.last_charging = min(ctx.charging_plan[day]['work_last_charging'], ctx.charging_plan[day]['trip_last_charging']) if ctx.charging_plan[day]['workday'] else ctx.charging_plan[day]['trip_last_charging']
                    next_departure = min(ctx.charging_plan[day]['work_goto'], ctx.charging_plan[day]['trip_goto']) if ctx.charging_plan[day]['workday'] else ctx.charging_plan[day]['trip_goto']

                kwh_needed_today = max(self.kwh_needed_for_charging(ctx.charging_plan[day]['work_battery_level_needed'] + self.get_min_daily_battery_level(), sum(ctx.charging_plan[day]['battery_level_before_work'])), 0.0)
                ctx.charging_plan[day]['work_kwh_needed'] = kwh_needed_today
                kwh_needed_today += ctx.charging_plan[day]['trip_kwh_needed']

                #Workaround for cold weather
                if kwh_needed_today <= max((self.config['ev_car']['battery_size'] / 100), 0.3):
                    kwh_needed_today = 0.0

                while_count = 0
                while_loop = True

                while while_loop and while_count < 2:
                    for timestamp, price in ctx.sorted_by_cheapest_price:
                        if ctx.charging_plan[day]['workday'] and ctx.charging_plan[day]['trip'] and while_count == 0:
                            #_LOGGER.info(f"DEBUG {timestamp} {price} {charging_plan[day]['work_homecoming']} < {charging_plan[day]['trip_last_charging']} {charging_plan[day]['work_homecoming'] < charging_plan[day]['trip_last_charging']} {sum(charging_plan[day]['battery_level_before_work'])} >= ({charging_plan[day]['work_battery_level_needed']} + {self.get_min_daily_battery_level()}) {sum(charging_plan[day]['battery_level_before_work']) >= (charging_plan[day]['work_battery_level_needed'] + self.get_min_daily_battery_level())}")

                            if (ctx.charging_plan[day]['work_homecoming'] < ctx.charging_plan[day]['trip_last_charging']
                                and sum(ctx.charging_plan[day]['battery_level_before_work']) >= (ctx.charging_plan[day]['work_battery_level_needed'] + self.get_min_trip_battery_level())):
                                ctx.last_charging = ctx.charging_plan[day]['trip_last_charging']
                                _LOGGER.info(f"Enought battery level for work, planning for trip {sum(ctx.charging_plan[day]['battery_level_before_work'])} >= ({ctx.charging_plan[day]['work_battery_level_needed']} + {self.get_min_trip_battery_level()})")
                                break

                        timestamp = self.change_timestamp_with_minutes(ctx.charging_plan, timestamp)

                        if not in_between(timestamp, ctx.current_hour, ctx.last_charging + datetime.timedelta(hours=1)):
                            continue

                        hour_in_chargeHours, kwh_available = self.kwh_available_in_hour(ctx.chargeHours, timestamp)
                        if hour_in_chargeHours and not kwh_available:
                            continue

                        what_day = days_between(self.now(), timestamp)
                        if what_day < 0:
                            continue

                        battery_level_id, max_recommended_charge_limit_battery_level = self.what_battery_level(ctx, what_day, timestamp, price, day)
                        if not battery_level_id:
                            _LOGGER.debug(f"battery_level_id not found for day ({what_day}) {timestamp} {price}. continue to next cheapest timestamp/price")
                            continue

                        if self.battery_level_full_on_next_departure(ctx, what_day):
                            _LOGGER.debug(f"Max battery level reached for day ({what_day}) before work {timestamp} {price}. continue to next cheapest timestamp/price")
                            continue

                        working, on_trip = self.available_for_charging_prediction(ctx.charging_plan, timestamp, ctx.trip_date_time, ctx.trip_homecoming_date_time)
                        if working or on_trip:
                            continue

                        #Code below temporary disabled, maybe not needed anymore
                        '''if len(chargeHours) > 1:
                            filteredDict = {k: v for k, v in chargeHours.items() if type(k) is datetime.datetime}
                            if len(filteredDict) > 1:
                                first_charging_session = sorted(filteredDict.keys())[0]
                                trip_battery_level_needed = charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max']

                                try:
                                    if (chargeHours[first_charging_session]['trip'] and
                                    timestamp < first_charging_session and
                                    trip_battery_level_needed != 100):
                                        if what_day != 0 and not charging_plan[day]['workday']:
                                            continue
                                except:
                                    pass'''

                        if round(kwh_needed_today, 1) > 0.0 and self.kwh_to_percentage(kwh_needed_today, include_charging_loss = True) > 0.0:
                            '''if round(sum(charging_plan[what_day][battery_level_id]), 0) >= self.get_max_recommended_charge_limit_battery_level() - 1.0:
                                #Workaround for cold battery percentage: ex. 90% normal temp = 89% cold temp
                                continue'''

                            kwh_needed_today, ctx.totalCost, ctx.totalkWh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.chargeHours, ctx.charging_plan, kwh_needed_today, ctx.totalCost, ctx.totalkWh, timestamp, price, None, None, kwh_available, sum(ctx.charging_plan[what_day][battery_level_id]), check_max_charging_plan={"day": day, "what_day": what_day, "battery_level_id": battery_level_id}, max_recommended_battery_level=max_recommended_charge_limit_battery_level, rules=ctx.charging_plan[day]['rules'])

                            if timestamp in ctx.chargeHours and battery_level_added:
                                self.scheduled_charging_added(ctx, day, what_day, timestamp, battery_level_id, battery_level_added, cost_added)
                        else:
                            while_loop = False
                            break
                    while_count += 1

                if kwh_needed_today > 0.0 and self.kwh_to_percentage(kwh_needed_today, include_charging_loss = True) > 0.0:
                    _LOGGER.warning(f"Not enought charging planned for day {day} kwh_needed_today {kwh_needed_today}kWh, using all available hours until {next_departure} to fullfill needed kWh")
                    for i in range(hours_between(ctx.current_hour, next_departure), 0, -1):
                        timestamp = next_departure - datetime.timedelta(hours=i)
                        hour_in_chargeHours, kwh_available = self.kwh_available_in_hour(ctx.chargeHours, timestamp)
                        if hour_in_chargeHours and not kwh_available:
                            continue

                        what_day = days_between(self.now(), timestamp)
                        if what_day < 0:
                            continue

                        battery_level_id, max_recommended_charge_limit_battery_level = self.what_battery_level(ctx, what_day, timestamp, price, day)
                        if not battery_level_id:
                            continue

                        if round(kwh_needed_today, 1) > 0.0 and self.kwh_to_percentage(kwh_needed_today, include_charging_loss = True) > 0.0:
                            '''if round(sum(charging_plan[what_day][battery_level_id]), 0) >= self.get_max_recommended_charge_limit_battery_level() - 1.0:
                                #Workaround for cold battery percentage: ex. 90% normal temp = 89% cold temp
                                continue'''
                            kwh_needed_today, ctx.totalCost, ctx.totalkWh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.chargeHours, ctx.charging_plan, kwh_needed_today, ctx.totalCost, ctx.totalkWh, timestamp, price, None, None, kwh_available, sum(ctx.charging_plan[what_day][battery_level_id]), check_max_charging_plan={"day": day, "what_day": what_day, "battery_level_id": battery_level_id}, max_recommended_battery_level=max_recommended_charge_limit_battery_level, rules=ctx.charging_plan[day]['rules'])

                            if timestamp in ctx.chargeHours and battery_level_added:
                                self.scheduled_charging_added(ctx, day, what_day, timestamp, battery_level_id, battery_level_added, cost_added)
                    if round(kwh_needed_today, 1) > 0.0 and self.kwh_to_percentage(kwh_needed_today, include_charging_loss = True) > 0.0:
                        _LOGGER.warning(f"Battery level above max recommended charge limit {self.get_max_recommended_charge_limit_battery_level()}%, could not fullfill needed kWh for day {day}, left kwh_needed_today {kwh_needed_today}kWh")
        except Exception as e:
            _LOGGER.error(f"Error in scheduled_planner day:{day}: {e} {type(e)}")
            self.persistent_notification(
                f"Error in scheduled_planner day:{day}: {e} {type(e)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_{FUNC_NAME}_{ctx.sub_func_name}_{sub_sub_func_name}_error"
            )
            raise Exception(f"Error in scheduled_planner day:{day}: {e} {type(e)}")
