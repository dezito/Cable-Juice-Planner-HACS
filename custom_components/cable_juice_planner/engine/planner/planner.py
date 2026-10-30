"""The charging plan (engine part): port of ev.py cheap_grid_charge_hours.

The function is kept as one method with the nested planners of the original, so the
order of the calculations (and the floating point results) stay the same. The
PyScript tasks inside are called sequentially; where the original never read a task
result, an exception is swallowed the same way.
"""
from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from copy import deepcopy
from pprint import pformat

from ...const import CHARGING_ALLOWED_AFTER_GOTO_TIME
from ...utils.formatting import emoji_parse, emoji_text_format, join_unique_emojis
from ...utils.helpers import (
    average,
    check_next_24_hours_diff,
    date_to_string,
    day_of_week_text,
    days_between,
    hours_between,
    in_between,
    minutes_between,
    reset_datetime,
    reset_time_to_hour,
    round_up,
    start_of_day,
)
from .hours_plan import HoursPlan
from .price_levels import PriceLevels
from .charge_plan import ChargePlan
from .future import FutureCharging

_LOGGER = logging.getLogger(__name__)
FUNC_NAME = "cheap_grid_charge_hours"


class Planner(PriceLevels, HoursPlan, ChargePlan, FutureCharging):
    """The charging plan, the departure plan and the overview markdown."""

    def cheap_grid_charge_hours(self, force_recalculate=False):  # noqa: C901 - ported as one function
        func_name = "cheap_grid_charge_hours"

        if not self.price_provider.buy_prices_available():
            entity_id = self.price_provider.get_buy_price_entity_id()
            _LOGGER.error(f"{entity_id} not in entities")
            self.persistent_notification(
                self.t("ui.cheap_grid_charge_hours.entity_not_in_domain", entity=entity_id),
                title=f"{self.title} warning",
                notification_id=f"{self.name}_{func_name}_real_prices_not_found"
            )
            return

        try:
            self.get_forecast_type()
            self.current_battery_level_expenses()
            energy_prediction = self.local_energy_prediction()
        except KeyError as e:
            _LOGGER.warning(f"Cancelled/Timeout/KeyError in cheap_grid_charge_hours: {e} {type(e)}")
            return
        except Exception as e:
            _LOGGER.error(f"Error in cheap_grid_charge_hours: {e} {type(e)}")
            raise Exception(f"Error in cheap_grid_charge_hours: {e} {type(e)}")

        if self.should_skip_calculation() and not force_recalculate:
            _LOGGER.debug("Hash is the same, skipping calculation")
            return

        grid_prices = deepcopy(self.get_grid_prices())
        sorted_by_cheapest_price = sorted(grid_prices.items(), key=lambda kv: (kv[1], kv[0]))
        energy_prediction_db = deepcopy(self.local_energy_prediction_db)
        battery_expenses = deepcopy(self.battery_level_expenses)
        last_drive_efficiency = deepcopy(self.last_drive_efficiency_data)

        today = self.start_of_day()
        current_hour = self.current_hour()
        now = self.now()

        chargeHours = {}
        charge_hours_alternative = {}

        totalCost = 0.0
        totalkWh = 0.0

        solar_over_production = {}
        work_overview = {}
        charging_plan = {
            "workday_in_week": False
        }

        cheap_average_price = []  # the average of the lowest prices, calculated once
        fill_up_price_limits = []

        trip_planned = self.is_trip_planned()
        trip_date_time = self.get_trip_date_time() if self.get_trip_date_time() != reset_datetime() and trip_planned else None
        trip_homecoming_date_time = self.get_trip_homecoming_date_time() if self.get_trip_homecoming_date_time() != reset_datetime() and trip_planned else None
        trip_range = self.get_trip_range() if self.get_trip_range() != 0.0 and trip_planned else None
        trip_target_level = self.get_trip_target_level() if self.get_trip_target_level() != 0.0 and trip_planned else None

        solar_kwh_prediction = energy_prediction[0]
        solar_price_prediction = energy_prediction[1]

        total_kwh_from_solar_available = solar_kwh_prediction['today']
        total_solar_price = [solar_price_prediction['today']]

        if self.solar_charging_enabled():
            attr_dict = {
                today.date(): f"{round(total_kwh_from_solar_available, 2)}% {round((total_kwh_from_solar_available * average(total_solar_price)), 2)}{self.t('ui.common.valuta')}"
                }

            for date in [today + datetime.timedelta(days=1), today + datetime.timedelta(days=2), today + datetime.timedelta(days=3), today + datetime.timedelta(days=4)]:
                if date.date() in solar_kwh_prediction:
                    attr_dict[date.date()] = f"{round(self.kwh_to_percentage(solar_kwh_prediction[date], include_charging_loss = True), 2)}% {round((solar_kwh_prediction[date] * solar_price_prediction[date]), 2)}{self.t('ui.common.valuta')}"
                    total_kwh_from_solar_available += solar_kwh_prediction[date]
                    total_solar_price.append(solar_price_prediction[date])

            attr_dict['Total'] = f"{round(self.kwh_to_percentage(total_kwh_from_solar_available, include_charging_loss = True), 2)}% {round(total_kwh_from_solar_available * average(total_solar_price), 2)}{self.t('ui.common.valuta')}"
            self.set_attr("charge_very_cheap_battery_level", "unused_solar_production_availability_prediction", attr_dict)
            self.set_attr("charge_ultra_cheap_battery_level", "unused_solar_production_availability_prediction", attr_dict)

        ev_charge_very_cheap_battery_level = max(0, int(round(self.get_very_cheap_grid_charging_max_battery_level() - self.kwh_to_percentage(total_kwh_from_solar_available, include_charging_loss = True), 0)))
        ev_charge_ultra_cheap_battery_level = max(0, int(round(self.get_ultra_cheap_grid_charging_max_battery_level() - self.kwh_to_percentage(total_kwh_from_solar_available, include_charging_loss = True), 0)))

        self.set_state("charge_very_cheap_battery_level", ev_charge_very_cheap_battery_level)
        self.set_state("charge_ultra_cheap_battery_level", ev_charge_ultra_cheap_battery_level)

        workday_rules =["first_workday_preparation", "second_workday_preparation", "third_workday_preparation", "fourth_workday_preparation", "fifth_workday_preparation", "sixth_workday_preparation", "seventh_workday_preparation", "eighth_workday_preparation"]
        workday_labels = [self.t(f"charging_type.{rule}.description") for rule in workday_rules]
        workday_emoji = []

        for i, rule in enumerate(workday_rules):
            workday_labels[i] =  f"{emoji_parse({rule: True})} {workday_labels[i]}"
            workday_emoji.append(f"{emoji_parse({rule: True})}")

        charging_plan_warnings = {}

        for day in range(8):
            start_of_day_datetime = self.start_of_day() + datetime.timedelta(days=day)
            end_of_day_datetime = self.end_of_day() + datetime.timedelta(days=day)
            day_text = day_of_week_text(end_of_day_datetime)

            charging_plan[day] = {
                "start_of_day": start_of_day_datetime,
                "end_of_day": end_of_day_datetime,
                "trip": False,
                "trip_goto": None,
                "trip_homecoming": None,
                "trip_last_charging": None,
                "trip_kwh_needed": 0.0,
                "trip_battery_level_needed": 0.0,
                "trip_battery_level_above_max": 0.0,
                "trip_total_cost": 0.0,
                "offday": False,
                "offday_range_needed": 0.0,
                "offday_battery_level_needed": 0.0,
                "offday_kwh_needed": 0.0,
                "offday_total_cost": 0.0,
                "workday": False,
                "work_range_needed": 0.0,
                "work_battery_level_needed": 0.0,
                "work_kwh_needed": 0.0,
                "work_goto": None,
                "work_homecoming": None,
                "work_away": False,
                "work_last_charging": None,
                "work_total_cost": 0.0,
                "total_needed_battery_level": 0.0,
                "day_text": day_text,
                "label": None,
                "emoji": "",
                "rules": [],
                "battery_level_before_work": [],
                "battery_level_after_work": [],
                "battery_level_at_midnight": [],
                "charging_sessions": {},
                "solar_prediction": [],
                "solar_kwh_prediction": [],
                "solar_cost_prediction": [],
                "solar_prediction_corrected": False,
            }

            if self.workplan_charging_enabled() and self.switch_state(f"workday_{day_text}") == "on":
                work_goto = end_of_day_datetime.replace(hour=self.setting(f'workday_departure_{day_text}').hour, minute=self.setting(f'workday_departure_{day_text}').minute, second=0)

                diff = minutes_between(now, work_goto)
                if day == 0 and self.ready_to_charge() and in_between(diff, CHARGING_ALLOWED_AFTER_GOTO_TIME, 0):
                    work_goto = work_goto + datetime.timedelta(hours=1)

                work_homecoming = end_of_day_datetime.replace(hour=self.setting(f'workday_homecoming_{day_text}').hour, minute=self.setting(f'workday_homecoming_{day_text}').minute, second=0)
                # away from home on today's workday: the way there is already driven (in the battery level),
                # only the way back (half the distance) is left
                away_on_workday = day == 0 and now < work_homecoming and not self.is_ev_home()
                if away_on_workday:
                    work_goto = max(work_goto, now)

                if (day == 0 and (now < work_goto or away_on_workday)) or day > 0:

                    charging_plan['workday_in_week'] = True
                    charging_plan[day]['workday'] = True

                    charging_plan[day]['work_goto'] = work_goto
                    charging_plan[day]['work_homecoming'] = work_homecoming
                    charging_plan[day]['work_away'] = away_on_workday

                    hour_buffer = int(self.config['ev_car']['last_charging_hour_before_departure_buffer']) if isinstance(self.config['ev_car']['last_charging_hour_before_departure_buffer'], (float, int)) else 2
                    hours_before = max(1, min(hour_buffer, hours_between(now, charging_plan[day]['work_goto'])))

                    charging_plan[day]['work_last_charging'] = reset_time_to_hour(work_goto) - datetime.timedelta(hours=hours_before)

                    charging_plan[day]['work_range_needed'] = self.get_entity_daily_distance(day_text = day_text)
                    if away_on_workday:
                        charging_plan[day]['work_range_needed'] /= 2
                    charging_plan[day]['work_battery_level_needed'] = self.calc_distance_to_battery_level(charging_plan[day]['work_range_needed'])

                    if len(workday_labels) > 0:
                        charging_plan[day]['label'] = workday_labels.pop(0)
                        charging_plan[day]['emoji'] = workday_emoji.pop(0)
                        charging_plan[day]['rules'].append(workday_rules.pop(0))

                    if charging_plan[day]['work_goto'] == charging_plan[day]['work_homecoming']:
                        self.charging_plan_warnings_add(charging_plan_warnings, "work", self.t('ui.cheap_grid_charge_hours.work_goto_equals_work_homecoming', day=self.t(f'ui.calendar.weekday_names.{day_of_week_text(end_of_day_datetime)}')))

                    if charging_plan[day]['work_range_needed'] == 0.0:
                        self.charging_plan_warnings_add(charging_plan_warnings, "work", self.t('ui.cheap_grid_charge_hours.work_distance_zero', day=self.t(f'ui.calendar.weekday_names.{day_of_week_text(end_of_day_datetime)}')))

            if self.is_trip_planned() and trip_date_time and day == days_between(self.now(), trip_date_time):
                charging_plan[day]['trip'] = True
                charging_plan[day]['trip_goto'] = trip_date_time
                charging_plan[day]['trip_homecoming'] = trip_homecoming_date_time

                diff = minutes_between(now, charging_plan[day]['trip_goto'])
                if day == 0 and self.ready_to_charge() and in_between(diff, CHARGING_ALLOWED_AFTER_GOTO_TIME, 0):
                    charging_plan[day]['trip_goto'] = charging_plan[day]['trip_goto'] + datetime.timedelta(hours=1)

                hour_buffer = int(self.config['ev_car']['last_charging_hour_before_departure_buffer']) if isinstance(self.config['ev_car']['last_charging_hour_before_departure_buffer'], (float, int)) else 2
                hours_before = max(1, min(hour_buffer, hours_between(now, charging_plan[day]['trip_goto'])))

                charging_plan[day]['trip_last_charging'] = reset_time_to_hour(charging_plan[day]['trip_goto']) - datetime.timedelta(hours=hours_before)
                charging_plan[day]['rules'].append("trip")

                if trip_range:
                    charging_plan[day]['trip_battery_level_needed'] = self.range_to_battery_level(trip_range, self.get_min_trip_battery_level(), trip_date_time)
                    trip_target_level = charging_plan[day]['trip_battery_level_needed']
                elif trip_target_level:
                    charging_plan[day]['trip_battery_level_needed'] = trip_target_level

                max_recommended_battery_level = max(self.get_max_recommended_charge_limit_battery_level(), self.battery_level())

                if charging_plan[day]['trip_battery_level_needed'] > max_recommended_battery_level and self.now() <= charging_plan[day]['trip_goto']:
                    charging_plan[day]['trip_battery_level_above_max'] = charging_plan[day]['trip_battery_level_needed'] - max_recommended_battery_level
                    charging_plan[day]['trip_battery_level_needed'] -= charging_plan[day]['trip_battery_level_above_max']
                    trip_kwh_needed = self.percentage_to_kwh(charging_plan[day]['trip_battery_level_above_max'], include_charging_loss=True)
                    charging_hours = int(round_up(trip_kwh_needed / self.max_kwh_charging))

                    last_charging = charging_plan[day]['trip_last_charging']

                    if (charging_plan[day]['workday'] and
                    ((charging_plan[day]['trip_last_charging'] < charging_plan[day]['work_homecoming'] and charging_plan[day]['trip_last_charging'] > charging_plan[day]['work_last_charging']) or
                    (self.now() > charging_plan[day]['work_last_charging'] and self.battery_level() < max_recommended_battery_level))):
                        charging_plan[day]['trip_last_charging'] = max(charging_plan[day]['work_last_charging'], self.current_hour())
                        last_charging = charging_plan[day]['trip_last_charging']

                    for i in range(charging_hours):
                        timestamp = last_charging - datetime.timedelta(hours=i)

                        try:
                            hour_in_chargeHours, kwh_available = self.kwh_available_in_hour(chargeHours, timestamp)
                            very_cheap_price, ultra_cheap_price = self.cheap_price_check(cheap_average_price, grid_prices, grid_prices[timestamp])
                            solar_percentage_prediction = self.kwh_to_percentage(solar_kwh_prediction.get(start_of_day(timestamp), solar_kwh_prediction['avg']), include_charging_loss=True)

                            trip_kwh_needed, totalCost, totalkWh, battery_level_added, cost_added = self.add_to_charge_hours(chargeHours, charging_plan, trip_kwh_needed, totalCost, totalkWh, timestamp, grid_prices[timestamp], very_cheap_price, ultra_cheap_price, kwh_available, max_recommended_battery_level, None, trip_target_level, ['trip'])
                            kwh_added = self.percentage_to_kwh(battery_level_added, include_charging_loss=False)
                            charging_plan[day]['trip_kwh_needed'] += kwh_added
                            charging_plan[day]["trip_total_cost"] += chargeHours[timestamp]["Cost"]

                            self.remove_solar_prediction_from_charge_hours(charging_plan, energy_prediction_db, timestamp, day, battery_level_added, solar_percentage_prediction=solar_percentage_prediction)

                            charging_sessions_id = "battery_level_before_work"

                            work_goto = charging_plan[day]['work_goto']
                            if not charging_plan[day]['work_goto']:
                                work_goto = charging_plan[day]['end_of_day']

                            if charging_plan[day]['trip_goto'] >= work_goto:
                                charging_plan[day]['battery_level_after_work'].append(battery_level_added)
                                charging_sessions_id = "battery_level_after_work" if charging_plan[day]['trip_goto'] >= work_goto else "battery_level_before_work"
                            else:
                                charging_plan[day]['battery_level_before_work'].append(battery_level_added)
                                charging_plan[day]['battery_level_after_work'].append(battery_level_added)

                            if charging_sessions_id not in charging_plan[day]['charging_sessions']:
                                charging_plan[day]['charging_sessions'][charging_sessions_id] = {}
                            charging_plan[day]['charging_sessions'][charging_sessions_id][timestamp] = chargeHours[timestamp]
                        except Exception as e:
                            _LOGGER.warning(f"Date not yet in database: {timestamp} ({e})")
            elif self.fill_up_charging_enabled():
                charging_plan[day]['offday'] = True
                charging_plan[day]['offday_range_needed'] = self.get_entity_daily_distance()
                charging_plan[day]['offday_battery_level_needed'] = self.calc_distance_to_battery_level(charging_plan[day]['offday_range_needed'])
                charging_plan[day]['offday_kwh_needed'] = self.percentage_to_kwh(charging_plan[day]['offday_battery_level_needed'], include_charging_loss=True)
                charging_plan[day]['emoji'] = emoji_parse({'fill_up': True})

            charging_plan[day]['trip_battery_level_needed'] = max(charging_plan[day]['trip_battery_level_needed'], 0.0)

            charging_plan[day]['total_needed_battery_level'] = (charging_plan[day]['work_battery_level_needed'] + self.get_min_daily_battery_level()) if charging_plan[day]['workday'] else 0.0
            charging_plan[day]['total_needed_battery_level'] += charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max']
            charging_plan[day]['total_needed_battery_level'] = min(round_up(charging_plan[day]['total_needed_battery_level']), 100.0)

            total_solar_available = 0.0
            total_solar_available_price = []

            if self.solar_charging_enabled():
                if day == 0:# and ((charging_plan[day]['workday'] and self.now() >= charging_plan[day]['work_homecoming']) or not charging_plan[day]['workday']):
                    total_solar_available += solar_kwh_prediction['today']
                    total_solar_available_price.append(solar_price_prediction['today'])
                elif 1 <= day <= 5 and (today + datetime.timedelta(days=day)) in solar_kwh_prediction:
                    total_solar_available += solar_kwh_prediction[today + datetime.timedelta(days=day)]
                    total_solar_available_price.append(solar_price_prediction[today + datetime.timedelta(days=day)])
                else:
                    total_solar_available += solar_kwh_prediction['avg']
                    total_solar_available_price.append(solar_price_prediction['avg'])

            # solar charges at a low power, with its own (measured) charging loss
            charging_plan[day]['solar_prediction'].append(round(self.kwh_to_percentage(total_solar_available, include_charging_loss = True, kw = self.solar_charging_kw()), 2))
            charging_plan[day]['solar_kwh_prediction'].append(round(total_solar_available, 2))
            charging_plan[day]['solar_cost_prediction'].append(round((total_solar_available * average(total_solar_available_price)), 2))

            """charging_plan[day]['solar_prediction'] = max(charging_plan[day]['solar_prediction'], 0.0)
            charging_plan[day]['solar_kwh_prediction'] = max(charging_plan[day]['solar_kwh_prediction'], 0.0)
            charging_plan[day]['solar_cost_prediction'] = max(charging_plan[day]['solar_cost_prediction'], 0.0)"""

            if not charging_plan[day]['rules']:
                charging_plan[day]['rules'].append("no_rule")

        if charging_plan_warnings:
            warning_message = []
            for plan in charging_plan_warnings.keys():
                for warning in charging_plan_warnings[plan]:
                    warning_message.append(f"- **{warning}**\n")
            self.persistent_notification(
                "".join(warning_message),
                title=f"{self.title} {self.t('ui.cheap_grid_charge_hours.workplan_settings_error')}",
                notification_id=f"{self.name}_{func_name}_charging_plan_warnings"
            )

        try:
            totalCost, totalkWh = self.future_charging(battery_expenses, chargeHours, charge_hours_alternative, charging_plan, cheap_average_price, current_hour, energy_prediction_db, fill_up_price_limits, grid_prices, now, solar_over_production, sorted_by_cheapest_price, trip_date_time, trip_homecoming_date_time, trip_range, work_overview, totalCost, totalkWh)
        except KeyError as e:
            _LOGGER.warning(f"Cancelled/Timeout/KeyError in future_charging: {e} {type(e)}")
            return
        except Exception as e:
            _LOGGER.error(f"Error in future_charging: {e} {type(e)}")
            raise Exception(f"Error in future_charging: {e} {type(e)}")

        chargeHours['total_cost'] = totalCost
        chargeHours['total_kwh'] = totalkWh
        chargeHours['total_procent'] = round(self.kwh_to_percentage(totalkWh, include_charging_loss = True), 2)

        expensiveList = []
        expensiveDict = {}

        for timestamp, price in self.get_expensive_hours().items():
            expensiveList.append(timestamp)
            expensiveDict[str(timestamp)] = f"{price:.2f} {self.t('ui.common.valuta')}"

        self.set_attr("forced_charging_daily_battery_level", "expensive_hours", dict(sorted(expensiveDict.items())))
        chargeHours['expensive_hours'] = expensiveList

        todays_max_battery_level = max(sum(charging_plan[0]["battery_level_before_work"]), sum(charging_plan[0]["battery_level_at_midnight"]), sum(charging_plan[0]["battery_level_after_work"]))
        tomorrow_max_battery_level = max(sum(charging_plan[1]["battery_level_before_work"]), sum(charging_plan[1]["battery_level_at_midnight"]), sum(charging_plan[1]["battery_level_after_work"]))
        chargeHours['max_charging_level_today'] = round(max(todays_max_battery_level, tomorrow_max_battery_level, charging_plan[0]['total_needed_battery_level'], charging_plan[1]['total_needed_battery_level'], self.get_min_charge_limit_battery_level()), 2)

        if chargeHours['max_charging_level_today'] > self.get_max_recommended_charge_limit_battery_level():
            chargeHours['max_charging_level_today'] = max(self.get_max_recommended_charge_limit_battery_level(), self.get_trip_target_level())

        '''_LOGGER.error(f"todays_max_battery_level:{todays_max_battery_level}")
        _LOGGER.error(f"tomorrow_max_battery_level:{tomorrow_max_battery_level}")
        _LOGGER.error(f"charging_plan[day]['total_needed_battery_level']:{charging_plan[day]['total_needed_battery_level']}")
        _LOGGER.error(f"get_min_charge_limit_battery_level:{self.get_min_charge_limit_battery_level()}")
        _LOGGER.error(f"chargeHours['max_charging_level_today']:{chargeHours['max_charging_level_today']}")'''

        for day in charging_plan.keys():
            if not isinstance(day, int): continue
            charging_plan[day]['battery_level_before_work_sum'] = round(sum(charging_plan[day]['battery_level_before_work']), 3)
            charging_plan[day]['battery_level_after_work_sum'] = round(sum(charging_plan[day]['battery_level_after_work']), 3)
            charging_plan[day]['battery_level_at_midnight_sum'] = round(sum(charging_plan[day]['battery_level_at_midnight']), 3)
            charging_plan[day]['solar_prediction_sum'] = round(sum(charging_plan[day]['solar_prediction']), 3)
            charging_plan[day]['solar_kwh_prediction_sum'] = round(sum(charging_plan[day]['solar_kwh_prediction']), 3)
            charging_plan[day]['solar_cost_prediction_sum'] = round(sum(charging_plan[day]['solar_cost_prediction']), 3)

        key_list = ["battery_level_before_work", "battery_level_after_work", "battery_level_at_midnight", "solar_prediction", "solar_kwh_prediction", "solar_cost_prediction"]
        for day in charging_plan.keys():
            if not isinstance(day, int): continue
            for key in key_list:
                if key in charging_plan[day]:
                    charging_plan[day][key] = [round(value, 3) for value in charging_plan[day][key]]

        charging_plan_attr = dict()
        charging_hours_attr = dict()
        try:
            for key, value in charging_plan.items():
                if isinstance(key, int):
                    title = f"Day {key} {str(value['day_text']).capitalize().replace('_', ' ')} ({value['label'] if value['label'] else ''})"
                    charging_plan_attr[title] = {}
                    for k, v in value.items():
                        if "_sum" in k or k == "total_needed_battery_level" and v:
                            charging_plan_attr[title][str(k).capitalize().replace("_", " ").replace("sum", "")] = f"{v:.2f}%"

                        if "solar_" in k and "_sum" in k and v:
                            if "kwh" in k:
                                charging_plan_attr[title][str(k).capitalize().replace("_", " ")] = f"{v:.2f} kWh"
                            elif "cost" in k:
                                charging_plan_attr[title][str(k).capitalize().replace("_", " ")] = f"{v:.2f} {self.t('ui.common.valuta')}"
                            else :
                                charging_plan_attr[title][str(k).capitalize().replace("_", " ")] = f"{v:.2f}%"

                        if (k == "workday" and v) or (k == "trip" and v):
                            if k == "workday":
                                charging_plan_attr[title]["Work goto"] = f"{value['work_goto']}"
                                charging_plan_attr[title]["Work homecoming"] = f"{value['work_homecoming']}"
                                charging_plan_attr[title]["Work last_charging"] = f"{value['work_last_charging']}"
                                charging_plan_attr[title]["Work range_needed"] = f"{value['work_range_needed']} {self.t('ui.common.distance_type')}"
                                charging_plan_attr[title]["Work total_cost"] = f"{value['work_total_cost']:.2f} {self.t('ui.common.valuta')}"
                                charging_plan_attr[title]["Work battery level needed"] = f"{value['work_battery_level_needed']:.2f}%"
                                charging_plan_attr[title]["Work kwh_needed"] = f"{value['work_kwh_needed']:.2f} kWh"
                            if k == "trip":
                                charging_plan_attr[title]["Trip goto"] = f"{value['trip_goto']}"
                                charging_plan_attr[title]["Trip homecoming"] = f"{value['trip_homecoming']}"
                                charging_plan_attr[title]["Trip last charging"] = f"{value['trip_last_charging']}"
                                charging_plan_attr[title]["Trip total cost"] = f"{value['trip_total_cost']:.2f} {self.t('ui.common.valuta')}"
                                charging_plan_attr[title]["Trip battery level needed"] = f"{value['trip_battery_level_needed']:.2f}%"
                                charging_plan_attr[title]["Trip battery level above max"] = f"{value['trip_battery_level_above_max']:.2f}%"
                                charging_plan_attr[title]["Trip kWh needed"] = f"{value['trip_kwh_needed']:.2f} kWh"
                    charging_plan_attr[title] = dict(sorted(charging_plan_attr[title].items()))
        except Exception as e:
            _LOGGER.error(f"Failed to create charging plan attributes: {e} {type(e)}")
            _LOGGER.error(f"charging_plan:\n{pformat(charging_plan, width=200, compact=True)}")

        try:
            for key, value in chargeHours.items():
                if key == "expensive_hours":
                    charging_hours_attr[str(key).capitalize().replace("_", " ")] = value
                elif key == "max_charging_level_today":
                    charging_hours_attr[str(key).capitalize().replace("_", " ")] = f"{value:.2f}%" if value else "0.0%"
                elif key == "total_procent":
                    charging_hours_attr[str(key).capitalize().replace("_", " ")] = f"{value:.2f}%"
                elif key == "total_kwh":
                    charging_hours_attr[str(key).capitalize().replace("_", " ")] = f"{value:.2f} kWh"
                elif key == "total_cost":
                    charging_hours_attr[str(key).capitalize().replace("_", " ")] = f"{value:.2f} {self.t('ui.common.valuta')}"
                else:
                    charging_hours_attr[str(key)] = [f"{value['battery_level']:.2f}%", f"{value['kWh']:.2f} kWh", f"{value['Cost']:.2f}{self.t('ui.common.valuta')}"]
        except Exception as e:
            _LOGGER.error(f"Failed to create charging plan charging_hours attributes: {e} {type(e)}")
            _LOGGER.error(f"charging_plan:\n{pformat(charging_plan, width=200, compact=True)}")

        self.set_attr("current_charging_rule", "charging_plan", charging_plan_attr)
        self.set_attr("current_charging_rule", "charging_hours", charging_hours_attr)

        # plain data for the dashboard charts: the planned hours and the expected battery level;
        # end = when the hour's kWh are charged at the hour's capacity (at most the end of the hour)
        self.set_attr("current_charging_rule", "charge_plan", [
            {"time": timestamp.isoformat(), "end": self.charge_plan_end(timestamp, value["kWh"]).isoformat(), "kwh": value["kWh"], "battery_level": value["battery_level"], "price": value["Price"], "cost": value["Cost"], "emoji": emoji_parse(value)}
            for timestamp, value in sorted((k, v) for k, v in chargeHours.items() if isinstance(k, datetime.datetime))
        ])
        driving = self.planned_driving(charging_plan, now)
        self.set_attr("current_charging_rule", "battery_forecast", self.battery_forecast_flow(charging_plan, chargeHours, now, driving))
        # the planned driving for the chart's bars, overlapping driving is added up there
        self.set_attr("current_charging_rule", "distance_forecast", [
            {"time": max(planned["start"], now).isoformat(), "end": planned["end"].isoformat(), "km": round(planned["km"], 0)}
            for planned in driving if planned["end"] > max(planned["start"], now) and planned["km"] > 0.0
        ])

        old_charge_hours = deepcopy(self.charge_hours)

        self.charging_plan = charging_plan
        self.charge_hours = chargeHours

        if check_next_24_hours_diff(old_charge_hours, chargeHours, self.now()) and old_charge_hours:
            self.append_overview_output("Charging plan changed, next 24 hours updated")

        overview = []

        try:
            if last_drive_efficiency:
                cost_per_distance = round(last_drive_efficiency['cost'] / last_drive_efficiency['distance'], 2) if last_drive_efficiency['distance'] > 0 else 0.0

                overview.append("<center>\n")
                overview.append(f"## 🛣️ {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.title')} ##")
                overview.append("|  |  |")
                overview.append("|:---|---:|")
                overview.append(f"| **📅 {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.drive_date')}** | **{date_to_string(last_drive_efficiency['timestamp'], format='%d/%m %H:%M')}** |")
                overview.append(f"| **🚗 {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.last_distance')}** | **{round(last_drive_efficiency['distance'], 1):.1f} {self.t('ui.common.distance_type')}** |")
                overview.append(f"| **🔋 {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.used_battery')}** | **{round(last_drive_efficiency['usedBattery'], 1):.1f}% ({round(last_drive_efficiency['usedkWh'], 2):.2f} kWh)** |")
                overview.append(f"| **💰 {self.t('ui.common.expense')}** | **{round(last_drive_efficiency['cost'], 2):.2f} {self.t('ui.common.valuta')} ({cost_per_distance:.2f} {self.t('ui.common.valuta_distance')})**")
                overview.append(f"| **📊 {self.t('ui.common.drive_efficiency')}** | **{round(last_drive_efficiency['efficiency'], 1):.1f}%** |")
                overview.append(f"| **📏 {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.distance_per_kwh')}** | **{round(last_drive_efficiency['distancePerkWh'], 2):.2f} {self.t('ui.common.distance_kwh')} ({last_drive_efficiency['wh_km']} {self.t('ui.common.wh_distance')})** |")
                overview.append("***")
                overview.append("</center>\n")
        except Exception as e:
            _LOGGER.error(f"Failed to calculate last drive efficiency: {e} {type(e)}")

        try:
            battery_level_expenses_report = battery_expenses.get("battery_level_expenses_cost", 0.0)
            battery_level_expenses_kwh_report = battery_expenses.get("battery_level_expenses_kwh", 0.0)
            battery_level_expenses_solar_percentage_report = battery_expenses.get("battery_level_expenses_solar_percentage", 0.0)
            battery_level_expenses_unit_report = battery_expenses.get('battery_level_expenses_unit', 0.0)

            if battery_level_expenses_kwh_report > 0.0:
                overview.append("<center>\n")
                overview.append(f"## 🔎 {self.t('ui.cheap_grid_charge_hours.battery_level_expenses.title')} ##")
                overview.append("|  |  |")
                overview.append("|:---|---:|")
                overview.append(f"| **🔋 {self.t('ui.cheap_grid_charge_hours.battery_level_expenses.current_battery_level')}** | **{round(self.battery_level(), 0):.0f}% {round(battery_level_expenses_kwh_report, 1):.1f} kWh** |")

                if battery_level_expenses_solar_percentage_report > 0.0:
                    overview.append(f"| **☀️ {self.t('ui.cheap_grid_charge_hours.battery_level_expenses.solar_share')}** | **{round(battery_level_expenses_solar_percentage_report,0):.0f}% {round(self.percentage_to_kwh(battery_level_expenses_solar_percentage_report), 1)} kWh** |")

                overview.append(f"| **💰 {self.t('ui.common.expense')}** | **{round(battery_level_expenses_report, 2):.2f} {self.t('ui.common.valuta')}** |")
                overview.append(f"| **🧮 {self.t('ui.cheap_grid_charge_hours.battery_level_expenses.unit_price')}** | {round(self.percentage_to_kwh(battery_level_expenses_unit_report, include_charging_loss=True), 2):.2f} {self.t('ui.common.valuta_percentage')}<br>**{round(battery_level_expenses_unit_report, 2):.2f} {self.t('ui.common.valuta_kwh')}** |")
                overview.append("</center>\n")
                overview.append("***")
        except Exception as e:
            _LOGGER.error(f"Failed to calculate battery level cost: {e} {type(e)}")

        try:
            def planning_basis_markdown():

                if "last_update" in grid_prices:
                    overview.append(f"\n\n<details><summary>{self.t('ui.cheap_grid_charge_hours.planning_basis')}</summary>\n")
                    overview.extend(self.get_hours_plan())
                    overview.append("</details>\n")

            sorted_charge_hours = sorted(
                {k: v for k, v in chargeHours.items() if isinstance(k, datetime.datetime)}.items(),
                key=lambda kv: kv[0]
            )

            merged_intervals = []
            has_combined = False
            current_interval = None

            for timestamp, value in sorted_charge_hours:
                if current_interval is None:
                    current_interval = {
                        "start": timestamp,
                        "end": timestamp,
                        "type": emoji_parse(value),
                        "percentage": value['battery_level'],
                        "kWh": round(value['kWh'], 2),
                        "cost": round(value['Cost'], 2),
                        "unit": round(value['Price'], 2),
                    }
                else:
                    if timestamp == current_interval["end"].replace(minute=0) + datetime.timedelta(hours=1) and days_between(current_interval["start"], timestamp) == 0:
                        has_combined = True
                        current_interval["end"] = timestamp
                        current_interval["type"] = join_unique_emojis(current_interval["type"], emoji_parse(value))
                        current_interval["percentage"] += value['battery_level']
                        current_interval["kWh"] += round(value['kWh'], 2)
                        current_interval["cost"] += round(value['Cost'], 2)
                    else:
                        merged_intervals.append(current_interval)
                        current_interval = {
                            "start": timestamp,
                            "end": timestamp,
                            "type": emoji_parse(value),
                            "percentage": value['battery_level'],
                            "kWh": round(value['kWh'], 2),
                            "cost": round(value['Cost'], 2),
                            "unit": round(value['Price'], 2),
                        }

            if current_interval:
                merged_intervals.append(current_interval)

            overview.append("<center>\n")
            overview.append(f"## 📖 {self.t('ui.cheap_grid_charge_hours.charging_overview_title')} ##")

            if merged_intervals:
                overview.append(f"|  | {self.t('ui.common.time')} | % | kWh | {self.t('ui.common.valuta_kwh')} | {self.t('ui.common.price')} |")
                overview.append("|---:|:---:|---:|---:|:---:|---:|")

                for d in merged_intervals:
                    if d['start'].strftime('%H') == d['end'].strftime('%H'):
                        time_range = f"{d['start'].strftime('%d/%m %H:%M')}"
                    else:
                        time_range = f"{d['start'].strftime('%d/%m %H')}-{d['end'].strftime('%H:%M')}"

                    overview.append(f"| {emoji_text_format(d['type'], group_size=3)} | **{time_range}** | **{int(round(d['percentage'], 0))}** | **{d['kWh']:.2f}** | **{d['unit']:.2f}** | **{d['cost']:.2f}** |")

                if has_combined:
                    overview.append(f"\n<details><summary><b>{self.t('ui.common.total')} {int(round(chargeHours['total_procent'],0))}% {chargeHours['total_kwh']} kWh {chargeHours['total_cost']:.2f}{self.t('ui.common.valuta')} ({round(chargeHours['total_cost'] / chargeHours['total_kwh'],2)} {self.t('ui.common.valuta_kwh')})</b></summary>")
                    overview.append(f"\n|  | {self.t('ui.common.time')} | % | kWh | {self.t('ui.common.valuta_kwh')} | {self.t('ui.common.price')} |")
                    overview.append("|---:|:---:|---:|---:|:---:|---:|")

                    for timestamp, value in sorted_charge_hours:
                        overview.append(f"| {emoji_parse(value)} | **{timestamp.strftime('%d/%m %H:%M')}** | **{int(round(value['battery_level'], 0))}** | **{round(value['kWh'], 2):.2f}** | **{round(value['Price'], 2):.2f}** | **{round(value['Cost'], 2):.2f}** |")

                    planning_basis_markdown()

                    overview.append("</details>\n")
                else:
                    overview.append(f"\n**{self.t('ui.common.total')} {int(round(chargeHours['total_procent'],0))}% {chargeHours['total_kwh']} kWh {chargeHours['total_cost']:.2f}{self.t('ui.common.valuta')} ({round(chargeHours['total_cost'] / chargeHours['total_kwh'],2)} {self.t('ui.common.valuta_kwh')})**")

                    planning_basis_markdown()

                # all prices offline, or hours the provider has no price for, filled from the price history
                if self.grid_price_cache.get('missing_hours'):
                    def _build_header(n_pairs=4):
                        heads, aligns = [], []
                        for _ in range(n_pairs):
                            heads += [self.t('ui.common.time'), self.t('ui.common.price')]
                            aligns += [":---:", "---:"]
                        header = "| " + " | ".join(heads) + " |"
                        align  = "| " + " | ".join(aligns) + " |"
                        return header + "\n" + align

                    def _row_from_slice(slice_pairs, n_pairs=4):
                        row_cells = []

                        while len(slice_pairs) < n_pairs:
                            slice_pairs.append(("&nbsp;", "&nbsp;"))
                        for t, p in slice_pairs:
                            row_cells += [f"**{t}**", f"*{p}*"]
                        return "| " + " | ".join(row_cells) + " |"

                    overview.append(f"\n\n<details><summary><b>{self.t('ui.cheap_grid_charge_hours.offline_prices')}!!!</b></summary>\n")

                    by_day = defaultdict(list)
                    for ts, price in sorted(self.grid_price_cache["missing_hours"].items(), key=lambda kv: kv[0]):
                        by_day[ts.date()].append((ts.strftime("%H:%M"), f"{price:.2f}{self.t('ui.common.valuta')}"))

                    N_PAIRS = 4

                    for day in sorted(by_day.keys()):
                        rows = by_day[day]
                        overview.append(f"\n<b>{day.strftime('%d/%m')}</b>\n")

                        header = _build_header(n_pairs=N_PAIRS)
                        overview.append(header)

                        for i in range(0, len(rows), N_PAIRS):
                            slice_pairs = rows[i:i+N_PAIRS]
                            md_row = _row_from_slice(slice_pairs[:], n_pairs=N_PAIRS)
                            overview.append(md_row)
                        overview.append("")

                    overview.append("\n</details>\n\n")

                if work_overview:
                    overview.append("***")
            else:
                overview.append(f"**{self.t('ui.cheap_grid_charge_hours.no_upcoming_charging_planned')}**")

                if work_overview and self.is_solar_configured():
                    overview.append(f"**{self.t('ui.cheap_grid_charge_hours.enough_solar_production')}**")

                planning_basis_markdown()

            overview.append("</center>\n")
        except Exception as e:
            _LOGGER.error(f"Failed to create charging plan overview: {e} {type(e)}")
            _LOGGER.error(f"charging_plan:\n{pformat(charging_plan, width=200, compact=True)}")
            _LOGGER.error(f"chargeHours:\n{pformat(chargeHours, width=200, compact=True)}")

        try:
            work_overview_total_kwh = []
            work_overview_total_cost = []

            overview.append("<center>\n")
            overview.append(f"## 🗓️ {self.t('ui.cheap_grid_charge_hours.departure_plan.title')} ##")

            if work_overview:
                solar_header = f"{emoji_parse({'solar': True})}{self.t('ui.cheap_grid_charge_hours.departure_plan.sun')}" if self.is_solar_configured() else ""
                overview.append(f"|  | {self.t('ui.cheap_grid_charge_hours.departure_plan.day')} | {self.t('ui.cheap_grid_charge_hours.departure_plan.need')} | {solar_header} | {self.t('ui.common.price')} |")
                overview.append("|:---|:---:|:---:|:---:|:---:|")

                start_battery_level = self.battery_level()

                for k, d in work_overview.items():
                    work_overview_total_kwh.append(d['kwh_needed'])
                    work_overview_total_cost.append(d['cost'])

                    if charging_plan[d['day']]['workday'] and now < charging_plan[d['day']]['work_goto']:
                        start_battery_level = charging_plan[d['day']]['battery_level_before_work_sum']
                    elif charging_plan[d['day']]['trip'] and now < charging_plan[d['day']]['trip_goto']:
                        if charging_plan[d['day']]['workday'] and charging_plan[d['day']]['trip_goto'] < charging_plan[d['day']]['work_goto']:
                            start_battery_level = charging_plan[d['day']]['battery_level_before_work_sum']
                        else:
                            start_battery_level = charging_plan[d['day']]['battery_level_after_work_sum']
                    else:
                        start_battery_level = charging_plan[max(d['day'] - 1, 0)]['battery_level_at_midnight_sum']

                    battery_usage = ""

                    if k % 1 == 0:
                        if d["event_type"] == "trip":
                            total_trip_battery_level_needed = max((charging_plan[d['day']]['trip_battery_level_needed'] + charging_plan[d['day']]['trip_battery_level_above_max'] - self.get_min_trip_battery_level()), 0.0)
                            #battery_level_before_trip_sum = int(round(charging_plan[d['day']]['battery_level_after_work_sum'],0))
                            battery_level_after_trip_sum = int(round(start_battery_level - total_trip_battery_level_needed,0))
                            battery_usage = f"<br>{emoji_parse({'battery_usage': True})}{int(round(start_battery_level,0))}-{battery_level_after_trip_sum}%"
                            start_battery_level -= total_trip_battery_level_needed
                        elif d["event_type"] == "work":
                            #battery_level_before_work_sum = int(round(charging_plan[d['day']]['battery_level_before_work_sum'],0))
                            battery_level_after_work_sum = int(round(start_battery_level - d['battery_needed'],0))
                            battery_usage = f"<br>{emoji_parse({'battery_usage': True})}{int(round(start_battery_level,0))}-{battery_level_after_work_sum}%"
                            start_battery_level -= d['battery_needed']
                        else:
                            #battery_level_at_midnight_sum = int(round(min(charging_plan[d['day']]['battery_level_at_midnight_sum'] + d['battery_needed'], 100.0),0))
                            battery_level_after_offday_sum = int(round(start_battery_level - d['battery_needed'],0))
                            battery_usage = f"<br>{emoji_parse({'battery_usage': True})}{int(round(start_battery_level,0))}-{battery_level_after_offday_sum}%"
                            start_battery_level -= d['battery_needed']

                    range_label = f"<br>{emoji_parse({'distance': True})}{int(round(d['range_needed'], 0))} {self.t('ui.common.distance_type')}" if round(d.get('range_needed') or 0.0, 0) > 0 else ""

                    d['emoji'] = f"**{emoji_text_format(d['emoji'])}**" if d['emoji'] else ""
                    d['day_string'] = f"**{d['day_string']}**" if d['day_string'] else ""
                    d['date'] = f"**{d['date']}**" if d['date'] else ""
                    d['goto'] = f"**{d['goto']}**" if d['goto'] else ""
                    d['goto'] = f"{d['goto']}**-{d['homecoming']}**" if d['goto'] and d['homecoming'] else d['goto']
                    d['battery_needed'] = f"{int(d['battery_needed'])}" if d['battery_needed'] else ""
                    d['kwh_needed'] = f"**{round(d['kwh_needed'], 1)}**" if d['kwh_needed'] else ""
                    d['cost'] = f"**{d['cost']:.2f}**" if d['cost'] else ""
                    d['from_grid'] = emoji_parse({'grid_charging': True}) if d['from_grid'] > 0.0 else ""
                    d['from_battery'] = emoji_parse({'battery_stored': True}) if d['from_battery'] > 0.0 else ""
                    d['from_battery_solar'] = emoji_parse({'battery_stored': True}) if d['from_battery_solar'] > 0.0 else ""
                    d['solar'] = f"{emoji_parse({'solar': True})}{d['solar']}" if d['solar_kwh'] > 0.0 and self.is_solar_configured() else ""

                    need_label = f"**{d['from_grid']}{d['from_battery']}{d['battery_needed']}% {d['kwh_needed']}kWh{battery_usage}{range_label}**" if d['battery_needed'] or d['kwh_needed'] else ""
                    solar_label = f"**{d['from_battery_solar']}{d['solar']}**" if d['solar'] and self.is_solar_configured() else ""

                    overview.append(f"| {d['emoji']} | {d['day_string']}<br>{d['date']}<br>{d['goto']} | {need_label} | {solar_label} | {d['cost']} |")
            else:
                overview.append(f"**{self.t('ui.cheap_grid_charge_hours.departure_plan.no_departure_planned')}**")

            work_overview_total_kwh_sum = sum(work_overview_total_kwh)
            work_overview_total_cost_sum = sum(work_overview_total_cost)
            work_overview_per_kwh = work_overview_total_cost_sum / work_overview_total_kwh_sum if work_overview_total_kwh_sum > 0.0 else 0.0

            if work_overview_total_kwh_sum > 0.0:
                overview.append(f"\n**{self.t('ui.common.total')} {round(work_overview_total_kwh_sum, 1):.1f}kWh {round(work_overview_total_cost_sum, 2):.2f}{self.t('ui.common.valuta')} ({round(work_overview_per_kwh, 2):.2f} {self.t('ui.common.valuta_kwh')})**")

                work_overview_solar_text = f"{self.t('ui.cheap_grid_charge_hours.departure_plan.solar')} & " if self.is_solar_configured() else ""
                overview.append(f"##### 📎{self.t('ui.cheap_grid_charge_hours.departure_plan.plan_include_estimations', work_overview_solar_text=work_overview_solar_text)} #####")

            overview_alternative = []
            total_kwh_charge_hours_alternative = []
            total_cost_charge_hours_alternative = []

            for timestamp, value in sorted({k: v for k, v in charge_hours_alternative.items() if type(k) is datetime.datetime}.items(), key=lambda kv: (kv[0])):
                timestamp_str = timestamp.strftime('%d/%m %H:%M')
                if "solar" in value:
                    timestamp_str = timestamp.strftime('%d/%m')

                overview_alternative.append(f"| {value['emoji']} | **{timestamp_str}** | **{int(round(self.kwh_to_percentage(value['kWh'], include_charging_loss=True),0))}** | **{round(value['kWh'], 2):.2f}** | **{round(value['Price'], 2):.2f}** | **{round(value['Cost'], 2):.2f}** |")
                total_kwh_charge_hours_alternative.append(value['kWh'])
                total_cost_charge_hours_alternative.append(value['Cost'])

            total_kwh_charge_hours_alternative_sum = sum(total_kwh_charge_hours_alternative)
            total_cost_charge_hours_alternative_sum = sum(total_cost_charge_hours_alternative)
            total_charge_hours_per_kwh_alternative = total_cost_charge_hours_alternative_sum / total_kwh_charge_hours_alternative_sum if total_kwh_charge_hours_alternative_sum > 0.0 else 0.0

            if work_overview_total_kwh_sum > 0.0 and total_kwh_charge_hours_alternative_sum > 0.0:
                fmt = {
                    "alternative_cost": f"{round(total_charge_hours_per_kwh_alternative * work_overview_total_kwh_sum, 2):.2f}",
                    "alternative_per_kwh": f"{round(total_charge_hours_per_kwh_alternative, 2):.2f}",
                    "total_kwh": f"{round(work_overview_total_kwh_sum, 1):.1f}",
                    "difference_cost": f"{round((total_charge_hours_per_kwh_alternative * work_overview_total_kwh_sum) - work_overview_total_cost_sum, 2):.2f}",
                }
                overview.append("<details>")
                overview.append(f"<summary>{self.t('ui.cheap_grid_charge_hours.departure_plan.estimated_daily_charging', **fmt)}</summary>\n")

                overview.append(f"### {self.t('ui.cheap_grid_charge_hours.departure_plan.estimated_daily_charging_plan')} ###")
                overview.append(f"|  | {self.t('ui.common.time')} | % | kWh | {self.t('ui.common.valuta_kwh')} | {self.t('ui.common.price')} |")
                overview.append("|---:|:---:|---:|---:|:---:|---:|")

                overview.extend(overview_alternative)

                overview.append(f"\n**{self.t('ui.common.total')} {round(total_kwh_charge_hours_alternative_sum, 1):.1f}kWh {round(total_cost_charge_hours_alternative_sum, 2):.2f}{self.t('ui.common.valuta')} {round(total_charge_hours_per_kwh_alternative, 2):.2f}{self.t('ui.common.valuta_kwh')}**")

                overview.append("</details>\n")

            if self.is_solar_configured():
                overview.append("***")
                overview.append("</center>\n")
        except Exception as e:
            _LOGGER.error(f"Failed to create work overview: {e} {type(e)}")
            _LOGGER.error(f"work_overview: {work_overview}")
            _LOGGER.error(f"charging_plan:\n{pformat(charging_plan, width=200, compact=True)}")
            _LOGGER.error(f"chargeHours:\n{pformat(chargeHours, width=200, compact=True)}")

        try:
            if self.is_solar_configured():
                overview.append("<center>\n")

                if solar_over_production:
                    overview.append(f"## 🌞 {self.t('ui.cheap_grid_charge_hours.solar_production_title')} ##")

                    overview.append(f"| {self.t('ui.common.time')} |  |  |  | % |  | kWh |")
                    overview.append("|---|---:|:---|---:|---:|---|---:|")

                    for d in solar_over_production.values():
                        d['day'] = f"**{d['day']}**" if d['day'] else ""
                        d['date'] = f"**{d['date']}**" if d['date'] else ""
                        d['when'] = f"**{d['when']}**" if d['when'] else ""
                        d['emoji'] = f"**{emoji_text_format(d['emoji'])}**" if d['emoji'] else ""
                        d['percentage'] = f"**{round(d['percentage'], 1)}**" if d['percentage'] else "**0**"
                        d['kWh'] = f"**{round(d['kWh'], 1)}**" if d['kWh'] else "**0.0**"

                        if d['corrected']:
                            d['emoji'] = emoji_parse({'solar_corrected': True})

                        overview.append(f"| {d['day']} | {d['date']} | {d['when']} | {d['emoji']} | {d['percentage']} |  | {d['kWh']} |")
                else:
                    overview.append(f"### 🌞 {self.t('ui.cheap_grid_charge_hours.no_solar_production')} ##")

                overview.append("</center>\n")
        except Exception as e:
            _LOGGER.error(f"Failed to create solar over production overview: {e} {type(e)}")
            _LOGGER.error(f"solar_over_production: {solar_over_production}")
            _LOGGER.error(f"charging_plan:\n{pformat(charging_plan, width=200, compact=True)}")
            _LOGGER.error(f"chargeHours:\n{pformat(chargeHours, width=200, compact=True)}")

        if overview:
            overview.append("<center>\n")
            overview.append(f"##### {self.t('ui.cheap_grid_charge_hours.last_scheduled')} {self.now()} #####")
            overview.append("</center>\n")

            self.set_attr("overview", "overview", "\n".join(overview))
        else:
            self.set_attr("overview", "overview", self.t("ui.cheap_grid_charge_hours.no_data"))

        self.save_hashable_snapshot()

        return chargeHours
