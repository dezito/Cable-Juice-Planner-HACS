"""Helpers the future-charging rules share."""
from __future__ import annotations

import logging

from ....utils.helpers import in_between
from ..charge_plan import FUNC_NAME

_LOGGER = logging.getLogger(__name__)


class FutureSupport:
    """Battery level and charging-session helpers used by the rules."""

    def what_battery_level(self, ctx, what_day, hour, price, day, max_level=None):
        """max_level: a higher limit than the max recommended (the full charge for battery health)."""
        sub_sub_func_name = "what_battery_level"

        battery_level_id = "battery_level_at_midnight"
        max_recommended_charge_limit_battery_level = max_level if max_level is not None else self.get_max_recommended_charge_limit_battery_level()
        return_fail_list = [False, max_recommended_charge_limit_battery_level]

        try:
            if what_day < 0:
                _LOGGER.warning(f"Error in grid_prices: {hour} is before current time {self.now()} continue to next cheapest hour/price")
                return return_fail_list

            total_trip_battery_level_needed = ctx.charging_plan[day]["trip_battery_level_needed"] + ctx.charging_plan[day]["trip_battery_level_above_max"]

            if ctx.charging_plan[day]["trip"] and in_between(day - what_day, 0, 2) and max_recommended_charge_limit_battery_level < total_trip_battery_level_needed: # if charging_plan[day]["trip"] and in_between(day - what_day, 1, 0) and max_recommended_charge_limit_battery_level < total_trip_battery_level_needed:
                max_recommended_charge_limit_battery_level = total_trip_battery_level_needed

            what_day_battery_level_before_work = sum(ctx.charging_plan[what_day]['battery_level_before_work'])
            what_day_battery_level_after_work = max(sum(ctx.charging_plan[what_day]['battery_level_after_work']), sum(ctx.charging_plan[what_day]['battery_level_at_midnight']))
            after_what_day_battery_level_after_work = max(sum(ctx.charging_plan[min(what_day + 1, 7)]['battery_level_after_work']), sum(ctx.charging_plan[min(what_day + 1, 7)]['battery_level_at_midnight']))

            if price >= 0.0:
                if ctx.charging_plan[what_day]['workday'] and hour < ctx.charging_plan[what_day]['work_goto']:
                    battery_level_id = "battery_level_before_work"

                    if what_day_battery_level_before_work >= max_recommended_charge_limit_battery_level:
                        _LOGGER.debug(f"Max battery level reached for day ({what_day}) before work {hour} {price}. {what_day_battery_level_before_work}% >= {max_recommended_charge_limit_battery_level}%")
                        return return_fail_list
                else:
                    if what_day_battery_level_after_work >= max_recommended_charge_limit_battery_level:
                        _LOGGER.debug(f"Max battery level reached for day ({what_day}) at midnight {hour} {price}. {what_day_battery_level_after_work}% >= {max_recommended_charge_limit_battery_level}%")
                        return return_fail_list
                    if what_day + 1 < 7 and after_what_day_battery_level_after_work >= max_recommended_charge_limit_battery_level:
                        _LOGGER.debug(f"Max battery level reached for next day ({what_day + 1}) at midnight {hour} {price}. {after_what_day_battery_level_after_work}% >= {max_recommended_charge_limit_battery_level}%")
                        return return_fail_list
        except Exception as e:
            _LOGGER.error(f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} price:{price} day:{day}: {e} {type(e)}")
            self.persistent_notification(
                f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} price:{price} day:{day}: {e} {type(e)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_{FUNC_NAME}_{ctx.sub_func_name}_{sub_sub_func_name}_error"
            )
            raise Exception(f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} price:{price} day:{day}: {e} {type(e)}")

        return [battery_level_id, max_recommended_charge_limit_battery_level]

    def add_charging_session_to_day(self, ctx, hour, what_day, battery_level_id):
        sub_sub_func_name = "add_charging_session_to_day"

        charging_sessions_id = "battery_level_before_work" if battery_level_id == "battery_level_before_work" else "battery_level_after_work"

        try:
            if battery_level_id not in ctx.charging_plan[what_day]['charging_sessions']:
                ctx.charging_plan[what_day]['charging_sessions'][charging_sessions_id] = {}
            ctx.charging_plan[what_day]['charging_sessions'][charging_sessions_id][hour] = ctx.chargeHours[hour]
        except Exception as e:
            _LOGGER.error(f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} battery_level_id:{battery_level_id}: {e} {type(e)}")
            self.persistent_notification(
                f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} battery_level_id:{battery_level_id}: {e} {type(e)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_{FUNC_NAME}_{ctx.sub_func_name}_{sub_sub_func_name}_error"
            )
            raise Exception(f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} battery_level_id:{battery_level_id}: {e} {type(e)}")

        return charging_sessions_id

    def add_charging_to_days(self, ctx, day, what_day, charging_sessions_id, battery_level_added):
        sub_sub_func_name = "add_charging_to_days"

        try:
            if what_day > day:
                # the hour just after midnight (the +1 hour window): the next day starts from day's
                # midnight level, its own lists are filled when that day is planned
                ctx.charging_plan[day]['battery_level_at_midnight'].append(battery_level_added)
                return
            for i in range(max(what_day - 1, 0), day + 1):
                if charging_sessions_id == "battery_level_before_work":
                    if i == max(what_day - 1, 0) and day > 0:
                        ctx.charging_plan[i]['battery_level_at_midnight'].append(battery_level_added)

                    if  i >= what_day:
                        ctx.charging_plan[i]['battery_level_at_midnight'].append(battery_level_added)
                        ctx.charging_plan[i]['battery_level_after_work'].append(battery_level_added)
                        ctx.charging_plan[i]['battery_level_before_work'].append(battery_level_added)
                elif charging_sessions_id == "battery_level_after_work":
                    if i >= what_day:
                        ctx.charging_plan[i]['battery_level_at_midnight'].append(battery_level_added)
                        ctx.charging_plan[i]['battery_level_after_work'].append(battery_level_added)

                    if i > what_day:
                        ctx.charging_plan[i]['battery_level_before_work'].append(battery_level_added)
        except Exception as e:
            _LOGGER.error(f"Error in {sub_sub_func_name} what_day:{what_day} day:{day} charging_sessions_id:{charging_sessions_id} battery_level_added:{battery_level_added}: {e} {type(e)}")
            self.persistent_notification(
                f"Error in {sub_sub_func_name} what_day:{what_day} day:{day} charging_sessions_id:{charging_sessions_id} battery_level_added:{battery_level_added}: {e} {type(e)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_{FUNC_NAME}_{ctx.sub_func_name}_{sub_sub_func_name}_error"
            )
            raise Exception(f"Error in {sub_sub_func_name} what_day:{what_day} day:{day} charging_sessions_id:{charging_sessions_id} battery_level_added:{battery_level_added}: {e} {type(e)}")
