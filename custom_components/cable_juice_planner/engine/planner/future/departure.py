"""Charging needed at the departures."""
from __future__ import annotations

import datetime
import logging

from ....utils.formatting import emoji_parse
from ....utils.helpers import date_to_string, day_of_week_text

_LOGGER = logging.getLogger(__name__)


class FutureDeparture:
    """Plans the charge needed for each departure."""

    def departure_planner(self, ctx, day):

        try:
            solar_kwh = 0.0
            solar_percentage = 0.0
            ctx.solar_unit = 0.0

            if sum(ctx.unused_solar_kwh.values()) > 0.0:
                total_solar_kwh = sum(ctx.unused_solar_kwh.values())
                total_solar_price = sum(ctx.unused_solar_cost.values())

                if total_solar_kwh > 0.0:
                    ctx.solar_unit = total_solar_price / total_solar_kwh

                solar_percentage = self.kwh_to_percentage(total_solar_kwh, include_charging_loss=True)

            temp_events = []
            for event_type in ('workday', 'trip', 'offday'):
                from_battery = 0.0
                from_battery_solar = 0.0
                if ctx.charging_plan[day][event_type]:
                    if event_type in ('workday', 'trip') and ctx.charging_plan[day][event_type]:
                        goto_key = 'trip_goto' if event_type == 'trip' else 'work_goto'
                        event_time_start = ctx.charging_plan[day][goto_key]
                        event_time_end = ctx.charging_plan[day]['trip_homecoming'] if event_type == 'trip' else ctx.charging_plan[day]['work_homecoming']

                        emoji = emoji_parse({'trip': True}) if event_type == 'trip' else ctx.charging_plan[day]['emoji']
                        battery_level_needed_key = 'trip_battery_level_needed' if event_type == 'trip' else 'work_battery_level_needed'
                        battery_level_needed = ctx.charging_plan[day][battery_level_needed_key] if battery_level_needed_key =="work_battery_level_needed" else ctx.charging_plan[day][battery_level_needed_key] + ctx.charging_plan[day]["trip_battery_level_above_max"]
                        kwh_needed = ctx.charging_plan[day]['trip_kwh_needed' if event_type == 'trip' else 'work_kwh_needed']
                        range_needed = (ctx.trip_range or 0.0) if event_type == 'trip' else ctx.charging_plan[day]['work_range_needed']
                        cost = ctx.charging_plan[day]["trip_total_cost" if event_type == "trip" else "work_total_cost"]
                    elif event_type == 'offday' and ctx.charging_plan[day][event_type] and day > 0:
                        event_time_start = ctx.charging_plan[day]['start_of_day']
                        event_time_end = ctx.charging_plan[day]['end_of_day']

                        emoji = ctx.charging_plan[day]['emoji']
                        battery_level_needed = ctx.charging_plan[day]['offday_battery_level_needed']
                        kwh_needed = max(ctx.charging_plan[day]['offday_kwh_needed'], 0.0)
                        range_needed = ctx.charging_plan[day]['offday_range_needed']
                        cost = ctx.charging_plan[day]["offday_total_cost"]
                    else:
                        continue

                    ctx.unused_solar_kwh.clear()
                    ctx.unused_solar_cost.clear()

                    if solar_percentage > battery_level_needed:
                        diff = solar_percentage - battery_level_needed
                        diff_kwh = self.percentage_to_kwh(diff, include_charging_loss=True)
                        ctx.unused_solar_kwh[self.time_plus_days(day).date()] = diff_kwh
                        ctx.unused_solar_cost[self.time_plus_days(day).date()] = diff_kwh * ctx.solar_unit

                    solar_percentage = min(solar_percentage, battery_level_needed)
                    grid_battery_level_needed = battery_level_needed - solar_percentage

                    solar_kwh += self.percentage_to_kwh(solar_percentage, include_charging_loss=True)
                    solar_cost = ctx.solar_unit * solar_kwh
                    cost += solar_cost

                    if event_type in ('workday', 'trip'):
                        kwh_needed += solar_kwh
                    elif event_type == 'offday':
                        #kwh_needed = max(charging_plan[day]['offday_kwh_needed'] - kwh_needed, 0.0)
                        kwh_needed += solar_kwh

                    battery_level_expenses_solar_percentage_loop = 0.0
                    battery_level_expenses_solar_kwh_loop = 0.0
                    reference_battery_level = self.get_min_daily_battery_level() if event_type in ("workday", "offday") else self.get_min_trip_battery_level()

                    battery_level_expenses_percentage = ctx.battery_expenses.get("battery_level_expenses_percentage", 0.0)
                    battery_level_expenses_solar_percentage = ctx.battery_expenses.get("battery_level_expenses_solar_percentage", 0.0)

                    for key in sorted([k for k in ctx.battery_expenses.keys() if type(k) is datetime.datetime]):

                        if ctx.ignored_reference_battery_level < reference_battery_level:
                            ctx.ignored_reference_battery_level += ctx.battery_expenses[key]["percentage"]

                            if ctx.ignored_reference_battery_level > reference_battery_level:
                                diff = ctx.ignored_reference_battery_level - reference_battery_level

                                amount = diff / ctx.battery_expenses[key]["percentage"] if ctx.battery_expenses[key]["percentage"] > 0.0 else 0.0
                                ctx.battery_expenses[key]["percentage"] *= amount
                                ctx.battery_expenses[key]["solar_percentage"] *= amount

                            battery_level_expenses_percentage -= ctx.battery_expenses[key]["percentage"]

                        if ctx.ignored_reference_battery_level >= reference_battery_level and grid_battery_level_needed > 0.0 and battery_level_expenses_percentage > 0.0:
                            battery_level_expenses_grid_amount = min(grid_battery_level_needed, ctx.battery_expenses[key]["percentage"])
                            grid_battery_level_needed -= battery_level_expenses_grid_amount
                            battery_level_expenses_percentage -= battery_level_expenses_grid_amount

                            battery_level_expenses_grid_percentage_loop = battery_level_expenses_grid_amount
                            battery_level_expenses_grid_kwh_loop = self.percentage_to_kwh(battery_level_expenses_grid_percentage_loop, include_charging_loss=True)
                            battery_level_expenses_grid_cost_loop = ctx.battery_expenses['battery_level_expenses_unit'] * battery_level_expenses_grid_kwh_loop

                            kwh_needed += battery_level_expenses_grid_kwh_loop
                            cost += battery_level_expenses_grid_cost_loop

                            if round(battery_level_expenses_grid_kwh_loop, 1) > 0.0:
                                from_battery += battery_level_expenses_grid_kwh_loop

                            if battery_level_expenses_solar_percentage > 0.0:
                                battery_level_expenses_solar_amount = min(battery_level_expenses_grid_percentage_loop, ctx.battery_expenses[key]["solar_percentage"])
                                battery_level_expenses_solar_percentage -= battery_level_expenses_solar_amount

                                battery_level_expenses_solar_percentage_loop += min(battery_level_expenses_solar_amount, grid_battery_level_needed)
                                battery_level_expenses_solar_kwh_loop += self.percentage_to_kwh(battery_level_expenses_solar_amount, include_charging_loss=True)

                                if round(battery_level_expenses_solar_kwh_loop, 1) > 0.0:
                                    from_battery_solar = battery_level_expenses_solar_kwh_loop

                        ctx.battery_expenses[key]["percentage"] = round(ctx.battery_expenses[key]["percentage"], 1)
                        ctx.battery_expenses[key]["solar_percentage"] = round(ctx.battery_expenses[key]["solar_percentage"], 1)
                        ctx.battery_expenses["battery_level_expenses_percentage"] = round(ctx.battery_expenses["battery_level_expenses_percentage"], 1)
                        ctx.battery_expenses["battery_level_expenses_solar_percentage"] = round(ctx.battery_expenses["battery_level_expenses_solar_percentage"], 1)

                    if kwh_needed > self.percentage_to_kwh(battery_level_needed, include_charging_loss=True):
                        cost_unit = cost / kwh_needed
                        kwh_needed = self.percentage_to_kwh(battery_level_needed, include_charging_loss=True)
                        cost = kwh_needed * cost_unit

                    from_grid = max(kwh_needed - solar_kwh - from_battery - from_battery_solar, 0.0)

                    solar_percentage_label = solar_percentage + battery_level_expenses_solar_percentage_loop
                    solar_kwh_label = solar_kwh + battery_level_expenses_solar_kwh_loop

                    if solar_kwh_label > self.percentage_to_kwh(battery_level_needed, include_charging_loss=True):
                        solar_percentage_label = battery_level_needed
                        solar_kwh_label = self.percentage_to_kwh(battery_level_needed, include_charging_loss=True)

                    solar_percentage_label = round(solar_percentage_label, 0)
                    solar_kwh_label = round(solar_kwh_label, 1)
                    solar_label = f"{solar_percentage_label:.0f}% {solar_kwh_label:.1f}kWh" if solar_kwh_label > 0.0 else ""

                    solar_kwh = 0.0
                    solar_percentage = 0.0

                    temp_events.append({
                        "time": event_time_start,
                        "data": {
                            "emoji": emoji,
                            "day": day,
                            "day_string": f"*{self.t('ui.calendar.weekday_names.' + day_of_week_text(event_time_start))}*",
                            "date": f"*{date_to_string(date = event_time_start, format = '%d/%m')}*",
                            "goto": f"*{date_to_string(date = event_time_start, format = '%H:%M')}*",
                            "homecoming": f"*{date_to_string(date = event_time_end, format = '%H:%M')}*",
                            "solar": solar_label,
                            "solar_kwh": solar_kwh_label,
                            "battery_needed": battery_level_needed,
                            "kwh_needed": self.percentage_to_kwh(battery_level_needed, include_charging_loss=True),
                            "range_needed": range_needed,
                            "cost": cost,
                            "from_grid": from_grid,
                            "from_battery": from_battery,
                            "from_battery_solar": from_battery_solar,
                            "event_type": event_type
                        }
                    })

            if temp_events and len(temp_events) > 1:
                temp_events.sort(key=lambda x: x["time"])

                for index, event in enumerate(temp_events[:-1]): # Exclude the last item as it has no next item
                    current_event_date = event["time"].date()
                    next_event_date = temp_events[index + 1]["time"].date()

                    if current_event_date == next_event_date:
                        temp_events[index + 1]["data"]["solar"] = ""
                        break

            for index, event in enumerate(temp_events):
                work_overview_key = float(day) + (index / 10.0)
                ctx.work_overview[work_overview_key] = event["data"]
        except Exception as e:
            _LOGGER.warning(f"Cant create work overview for day {day}: {e} {type(e)}")
