"""Charging with the alternative charger."""
from __future__ import annotations

import datetime
import logging

from ....const import CHARGER_CHARGING_STATUS, CHARGER_COMPLETED_STATUS, CHARGER_READY_STATUS
from ....utils.formatting import emoji_parse
from ....utils.helpers import average, in_between

_LOGGER = logging.getLogger(__name__)


class FutureAlternate:
    """Alternative charging."""

    def alternate_charging_planner(self, ctx, day):

        try: #Alternative charging estimate

            dates = [date for date in [ctx.charging_plan[day]['work_homecoming'], ctx.charging_plan[day]['trip_homecoming'], self.now() if day == 0 and ctx.charger_status in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS else None] if date is not None]
            homecoming_alternative = min(dates) if dates else ctx.charging_plan[ctx.day_before]['start_of_day']

            dates = [date for date in [ctx.charging_plan[ctx.day_after]['work_last_charging'], ctx.charging_plan[ctx.day_after]['trip_last_charging'], ctx.charging_plan[day]['trip_last_charging']] if date is not None]
            last_charging_alternative = min(dates) if dates else ctx.charging_plan[ctx.day_after]['end_of_day']

            if ctx.now <= last_charging_alternative and day < 7:
                if day == 0:
                    used_battery_level_alternative = max(self.get_max_recommended_charge_limit_battery_level() - self.battery_level(), 0.0)
                else:
                    work_battery_level_needed_alternative = ctx.charging_plan[day]['work_battery_level_needed']
                    trip_battery_level_needed_alternative = ctx.charging_plan[day]['trip_battery_level_needed'] + ctx.charging_plan[day]['trip_battery_level_above_max'] + ctx.charging_plan[ctx.day_after]['trip_battery_level_needed'] + ctx.charging_plan[ctx.day_after]['trip_battery_level_above_max']
                    typical_daily_battery_level_needed_alternative = self.calc_distance_to_battery_level(self.get_entity_daily_distance()) if self.fill_up_charging_enabled() else 0.0
                    total_battery_level_needed_alternative = work_battery_level_needed_alternative + trip_battery_level_needed_alternative + typical_daily_battery_level_needed_alternative
                    used_battery_level_alternative = max(total_battery_level_needed_alternative, 0.0)

                    if ctx.charging_plan[day]["trip"]:
                        diff_min_alternative = max(self.get_min_daily_battery_level() - self.get_min_trip_battery_level(), 0.0) if used_battery_level_alternative > 0.0 else 0.0
                        used_battery_level_alternative += ctx.charging_plan[day]['trip_battery_level_needed'] + ctx.charging_plan[day]['trip_battery_level_above_max'] + diff_min_alternative

                kwh_needed_today_alternative = self.kwh_needed_for_charging(used_battery_level_alternative, 0.0)
                kwh_solar_alternative = min(sum(ctx.charging_plan[ctx.day_before]['solar_kwh_prediction']), kwh_needed_today_alternative) if day > 0 else 0.0
                kwh_needed_today_alternative -= kwh_solar_alternative

                if kwh_solar_alternative > 0.0:
                    solar_price = average(ctx.charging_plan[ctx.day_before]['solar_cost_prediction'])

                    sunrise = self.get_sun_events(ctx.charging_plan[day]['start_of_day'])['sunrise']
                    ctx.charge_hours_alternative[sunrise] = {
                        "emoji": emoji_parse({'solar': True}),
                        "day": f"{day} {ctx.charging_plan[day]['day_text']}",
                        "used_battery_level_alternative": used_battery_level_alternative,
                        "Price": solar_price,
                        "kWh": kwh_solar_alternative,
                        "Cost": kwh_solar_alternative * solar_price,
                        "solar": True
                    }

                for timestamp, price in ctx.sorted_by_cheapest_price:
                    if timestamp not in ctx.charge_hours_alternative and in_between(timestamp, homecoming_alternative - datetime.timedelta(hours=1), last_charging_alternative + datetime.timedelta(hours=1)):
                        working, on_trip = self.available_for_charging_prediction(ctx.charging_plan, timestamp, ctx.trip_date_time, ctx.trip_homecoming_date_time)
                        if working or on_trip:
                            continue

                        if round(kwh_needed_today_alternative, 1) > 0.0:
                            if (kwh_needed_today_alternative - self.max_kwh_charging) < 0.0:
                                kwh = kwh_needed_today_alternative
                                kwh_needed_today_alternative = 0.0
                            else:
                                kwh = self.max_kwh_charging
                                kwh_needed_today_alternative -= kwh
                            cost = kwh * price

                            emoji = ctx.charging_plan[ctx.day_after]['emoji'] if ctx.charging_plan[ctx.day_after]['emoji'] else "⛽"
                            ctx.charge_hours_alternative[timestamp] = {
                                "emoji": f"{emoji}",
                                "day": f"{ctx.day_after} {ctx.charging_plan[ctx.day_after]['day_text']}",
                                "used_battery_level_alternative": used_battery_level_alternative,
                                "Price": price,
                                "kWh": kwh,
                                "Cost": cost
                            }
                        else:
                            break
        except Exception as e:
            _LOGGER.warning(f"Cant create alternative charging estimate for day {day}: {e} {type(e)}")
