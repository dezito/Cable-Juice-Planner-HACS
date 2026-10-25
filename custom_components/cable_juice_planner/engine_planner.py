"""The charging plan (engine mixin): port of ev.py cheap_grid_charge_hours.

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

from .const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_READY_STATUS,
    CHARGING_ALLOWED_AFTER_GOTO_TIME,
)
from .formatting import emoji_parse, emoji_text_format, join_unique_emojis
from .helpers import (
    average,
    check_next_24_hours_diff,
    date_to_string,
    day_of_week_text,
    days_between,
    end_of_day,
    hours_between,
    in_between,
    minutes_between,
    reset_datetime,
    reset_time_to_hour,
    round_up,
    start_of_day,
)

_LOGGER = logging.getLogger(__name__)

FILL_UP_PRICE_FRACTION = 0.3  # fill up to the max level only in the cheapest 30 % of the known hours
FILL_UP_LOOK_AHEAD_TOLERANCE = 0.1  # ... and only when no hour until the end of the next day is more than 10 % cheaper

COLOR_THRESHOLDS = [
    ("price1", "#0064FF"),
    ("price2", "#0096FF"),
    ("price3", "#00B496"),
    ("price4", "#00FA50"),
    ("price5", "#00FF32"),
    ("price6", "#00FF00"),
    ("price7", "#FFFF00"),
    ("price8", "#FFA500"),
    ("price9", "#FF5000"),
    ("price10", "#C81919"),
    ("price11", "#C800C8"),
]


class PlannerMixin:
    """The charging plan, the departure plan and the overview markdown."""

    @staticmethod
    def calculate_price_levels(prices):
        """ Calculates price levels based on the provided prices. """
        lowest_price = min(prices)
        highest_price = max(prices)
        mean_price = sum(prices) / len(prices)
        step_under = (mean_price - lowest_price) / 5
        step_over = (highest_price - mean_price) / 5

        return {
            "price1": lowest_price,
            "price2": mean_price - step_under * 4,
            "price3": mean_price - step_under * 3,
            "price4": mean_price - step_under * 2,
            "price5": mean_price - step_under * 1,
            "price6": mean_price,
            "price7": mean_price + step_over * 1,
            "price8": mean_price + step_over * 2,
            "price9": mean_price + step_over * 3,
            "price10": mean_price + step_over * 4,
            "price11": highest_price,
        }

    @staticmethod
    def get_color(price, price_levels):
        """ Gets the color based on the price and predefined thresholds. """
        for key, color in reversed(COLOR_THRESHOLDS):
            if price >= price_levels[key]:
                return color
        return "#FFFFFF"

    def get_hours_plan(self):
        output = []

        if "prices" in self.get_grid_prices():
            prices = self.get_grid_prices()["prices"]
            if not prices:
                return output

            price_levels = self.calculate_price_levels(prices.values())

            data = {}
            now = self.current_hour()
            date_objects = set()
            not_home_color = "#666666"

            for timestamp, price in prices.items():
                date_obj = timestamp.date()
                date_objects.add(date_obj)
                date_str = f"{self.t('ui.calendar.weekday_names.' + timestamp.strftime('%a').lower())}<br>{timestamp.strftime('%-d/%-m')}"
                time_str = timestamp.strftime("%H:%M")
                color = self.get_color(price, price_levels)

                if time_str not in data:
                    data[time_str] = {}

                text_format_start, text_format_end = "**", "**"
                color_start, color_end = "", ""
                emojis = ""
                not_home_start_emoji, not_home_end_emoji = "", ""

                day = days_between(now, timestamp)
                if day < 0:
                    continue

                if day in self.charging_plan:
                    if timestamp < now:
                        text_format_start, text_format_end = "", ""
                        color = not_home_color
                        price = ""

                    if self.charging_plan[day]["trip"]:
                        trip_start = reset_time_to_hour(self.charging_plan[day]["trip_goto"])
                        trip_end = reset_time_to_hour(self.charging_plan[day]["trip_homecoming"])
                        if in_between(timestamp, trip_start, trip_end + datetime.timedelta(seconds=1)):
                            text_format_start, text_format_end = "~~", "~~"
                            color = not_home_color
                            if timestamp == trip_start:
                                not_home_start_emoji += emoji_parse({'trip': True})
                            elif timestamp == trip_end:
                                not_home_end_emoji += emoji_parse({'trip': True})

                    if self.charging_plan[day]["workday"]:
                        workday_start = reset_time_to_hour(self.charging_plan[day]["work_goto"])
                        workday_end = reset_time_to_hour(self.charging_plan[day]["work_homecoming"])
                        if in_between(timestamp, workday_start, workday_end + datetime.timedelta(seconds=1)):
                            text_format_start, text_format_end = "~~", "~~"
                            color = not_home_color
                            if timestamp == workday_start:
                                not_home_start_emoji += self.charging_plan[day]["emoji"]
                            elif timestamp == workday_end:
                                not_home_end_emoji += self.charging_plan[day]["emoji"]

                emojis = emoji_parse(self.charge_hours.get(timestamp, dict()))

                color_start, color_end = (f'<font color="{color}">', "</font>") if color else ("", "")

                emojis = f"<br>{emoji_text_format(emojis, group_size=2)}" if emojis else ""

                not_home_start_emoji = f"{not_home_start_emoji}⤵️<br>" if not_home_start_emoji else ""
                not_home_end_emoji = f"<br>{not_home_end_emoji}⤴️" if not_home_end_emoji else ""

                data[time_str][date_str] = f'{not_home_start_emoji}{color_start}{text_format_start}{price}{text_format_end}{color_end}{emojis}{not_home_end_emoji}'

            sorted_dates = [f"{self.t('ui.calendar.weekday_names.' + dt.strftime('%a').lower())}<br>{dt.strftime('%-d/%-m')}" for dt in sorted(date_objects)]
            sorted_hours = sorted({dt.strftime("%H:%M") for dt in prices})

            prices_output = []
            prices_output.append(f"### {self.t('ui.get_hours_plan.header')} ###")

            prices_output.append("| " + " | ".join([""] + sorted_dates) + " |")
            prices_output.append("|" + "|".join([":---:"] * (len(sorted_dates) + 1)) + "|")

            for hour in sorted_hours:
                row = [f"**{hour}**"] + [data[hour].get(date, "") for date in sorted_dates]
                prices_output.append("| " + " | ".join(row) + " |")

            output.append("\n".join(prices_output))
        return output

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

        def change_timestamp_with_minutes(timestamp: datetime.datetime):
            sub_func_name = "change_timestamp_with_minutes"

            work_minute = 0
            trip_minute = 0

            try:
                what_day = days_between(self.now(), timestamp)
                if what_day < 0:
                    return timestamp

                if isinstance(charging_plan[what_day]['work_last_charging'], datetime.datetime):
                    if charging_plan[what_day]['work_last_charging'].hour == timestamp.hour and charging_plan[what_day]['work_last_charging'].minute != 0:
                        work_minute = max(charging_plan[what_day]['work_last_charging'].minute, work_minute)
                if isinstance(charging_plan[what_day]['work_homecoming'], datetime.datetime):
                    if charging_plan[what_day]['work_homecoming'].hour == timestamp.hour and charging_plan[what_day]['work_homecoming'].minute != 0:
                        work_minute = max(charging_plan[what_day]['work_homecoming'].minute, work_minute)

                if isinstance(charging_plan[what_day]['trip_last_charging'], datetime.datetime):
                    if charging_plan[what_day]['trip_last_charging'].hour == timestamp.hour and charging_plan[what_day]['trip_last_charging'].minute != 0:
                        trip_minute = max(charging_plan[what_day]['trip_last_charging'].minute, trip_minute)
                if isinstance(charging_plan[what_day]['trip_homecoming'], datetime.datetime):
                    if charging_plan[what_day]['trip_homecoming'].hour == timestamp.hour and charging_plan[what_day]['trip_homecoming'].minute != 0:
                        trip_minute = max(charging_plan[what_day]['trip_homecoming'].minute, trip_minute)
            except Exception as e:
                _LOGGER.error(f"timestamp:{timestamp} error:{e} {type(e)}")
                self.persistent_notification(
                    f"Error changing timestamp with minutes timestamp:{timestamp} error:{e} {type(e)}",
                    title=f"{self.title} error",
                    notification_id=f"{self.name}_{func_name}_{sub_func_name}_error"
                )

            return timestamp.replace(minute=max(work_minute, trip_minute, 0))

        def available_for_charging_prediction(timestamp: datetime.datetime, trip_datetime = None, trip_homecoming_datetime = None):
            sub_func_name = "available_for_charging_prediction"

            working = False
            on_trip = False

            try:
                day = days_between(self.now(), timestamp)

                if day < 0:
                    return working, on_trip

                if charging_plan[day]['workday']:
                    working = in_between(timestamp, charging_plan[day]['work_goto'], charging_plan[day]['work_homecoming'])

                if trip_datetime:
                    on_trip = in_between(timestamp, trip_datetime, trip_homecoming_datetime)
            except Exception as e:
                _LOGGER.error(f"timestamp:{timestamp} trip_datetime:{trip_datetime} trip_homecoming_datetime:{trip_homecoming_datetime} error:{e} {type(e)}")
                self.persistent_notification(
                    f"Error checking available for charging prediction timestamp:{timestamp} trip_datetime:{trip_datetime} trip_homecoming_datetime:{trip_homecoming_datetime} error:{e} {type(e)}",
                    title=f"{self.title} error",
                    notification_id=f"{self.name}_{func_name}_{sub_func_name}_error"
                )
            return working, on_trip

        def kwh_available_in_hour(hour):
            sub_func_name = "kwh_available_in_hour"

            hour_in_chargeHours = False
            kwh_available = False

            try:
                kwh = self.hour_kwh_capacity(hour) - 1.0

                percentage_missed = hour.minute / 60
                kwh_missed = kwh * percentage_missed

                kwh -= kwh_missed
                if hour in chargeHours:
                    hour_in_chargeHours = True
                    if chargeHours[hour]['kWh'] < kwh:
                        kwh_available = True
            except Exception as e:
                _LOGGER.error(f"hour:{hour} error:{e} {type(e)}")
                self.persistent_notification(
                    f"Error checking kWh available in hour:{hour} error:{e} {type(e)}",
                    title=f"{self.title} error",
                    notification_id=f"{self.name}_{func_name}_{sub_func_name}_error"
                )
            return [hour_in_chargeHours, kwh_available]

        def check_max_battery_level_allowed(day, what_day, battery_level_id, max_recommended_battery_level, battery_level_to_added):
            sub_func_name = "check_max_battery_level_allowed"

            charging_sessions_id = "battery_level_before_work" if battery_level_id == "battery_level_before_work" else "battery_level_after_work"
            max_battery_level = 0.0
            try:
                if what_day > day:
                    # the hour just after midnight (the +1 hour window): counts in day's midnight level
                    max_battery_level = sum(charging_plan[day]['battery_level_at_midnight']) + battery_level_to_added
                for i in range(max(what_day - 1, 0), day + 1):
                    if charging_sessions_id == "battery_level_before_work":
                        if i == max(what_day - 1, 0) and day > 0:
                            max_battery_level = max(
                                sum(charging_plan[i]['battery_level_at_midnight']) + battery_level_to_added,
                                max_battery_level
                                )

                        if  i >= what_day:
                            max_battery_level = max(
                                sum(charging_plan[i]['battery_level_at_midnight']) + battery_level_to_added,
                                sum(charging_plan[i]['battery_level_after_work']) + battery_level_to_added,
                                sum(charging_plan[i]['battery_level_before_work']) + battery_level_to_added,
                                max_battery_level
                                )
                    elif charging_sessions_id == "battery_level_after_work":
                        if i >= what_day:
                            max_battery_level = max(
                                sum(charging_plan[i]['battery_level_at_midnight']) + battery_level_to_added,
                                sum(charging_plan[i]['battery_level_after_work']) + battery_level_to_added,
                                max_battery_level
                                )

                        if i > what_day:
                            max_battery_level = max(
                                sum(charging_plan[i]['battery_level_before_work']) + battery_level_to_added,
                                max_battery_level
                                )
                if max_battery_level > max_recommended_battery_level:
                    kwh_allowed = self.percentage_to_kwh(max_recommended_battery_level - max_battery_level, include_charging_loss = True)
                    return kwh_allowed
            except Exception as e:
                _LOGGER.error(f"day:{day} what_day:{what_day} battery_level_id:{battery_level_id} max_recommended_battery_level:{max_recommended_battery_level} battery_level_to_added:{battery_level_to_added} max_battery_level:{max_battery_level} error:{e} {type(e)}")
                self.persistent_notification(
                    f"Error checking max battery level allowed day:{day} what_day:{what_day} battery_level_id:{battery_level_id} max_recommended_battery_level:{max_recommended_battery_level} battery_level_to_added:{battery_level_to_added} max_battery_level:{max_battery_level} error:{e} {type(e)}",
                    title=f"{self.title} error",
                    notification_id=f"{self.name}_{func_name}_{sub_func_name}_error"
                )

            return self.percentage_to_kwh(battery_level_to_added, include_charging_loss = True)

        def add_to_charge_hours(kwhNeeded, totalCost, totalkWh, hour, price, very_cheap_price, ultra_cheap_price, kwh_available, battery_level = None, check_max_charging_plan = None, max_recommended_battery_level = None, rules = None):
            sub_func_name = "add_to_charge_hours"

            # a copy: the price rules of this hour must not stick to the caller's list
            rules = list(rules or [])

            min_charging_kwh_cron = 0.0
            min_charging_kwh_ev = 0.0

            try:
                min_charging_kwh_cron = self.config['cron_interval'] / 60 * self.max_kwh_charging / 2
            except:
                pass

            try:
                min_charging_kwh_ev = self.config['ev_car']['battery_size'] / 100 / 2
            except:
                pass

            try:
                cost = 0.0
                kwh = self.hour_kwh_capacity(hour)
                min_charging_kwh = max(min(min_charging_kwh_cron, min_charging_kwh_ev), 0.2)
                # the charging loss at this hour's power (measured model); kwhNeeded is at the
                # default loss, so the same battery level takes kwh_ratio times the kWh this hour
                hour_kw = kwh
                kwh_ratio = (1 + self.charging_loss_factor(hour_kw)) / (1 + self.charging_loss_factor())

                battery_level_added = False

                price = float(price)
                kwhNeeded = float(kwhNeeded)

                percentage_missed = hour.minute / 60
                kwh_missed = kwh * percentage_missed
                kwh -= kwh_missed

                if battery_level:
                    if max_recommended_battery_level is None:
                        max_recommended_battery_level = self.get_max_recommended_charge_limit_battery_level()

                    battery_level_diff = round(max_recommended_battery_level - (battery_level + self.kwh_to_percentage(kwh, include_charging_loss = True, kw = hour_kw)),2)
                    kwh_diff = round(self.percentage_to_kwh(battery_level_diff, include_charging_loss = True, kw = hour_kw),2)
                    if kwh_diff < 0.0:
                        kwh = max(kwh + kwh_diff, 0.0)

                if kwh_available == True and hour in chargeHours:
                    kwh -= chargeHours[hour]['kWh']

                if check_max_charging_plan and check_max_charging_plan['day'] is not None:
                    kwh_allowed = check_max_battery_level_allowed(check_max_charging_plan['day'], check_max_charging_plan['what_day'], check_max_charging_plan['battery_level_id'], max_recommended_battery_level, self.kwh_to_percentage(kwh, include_charging_loss = True, kw = hour_kw))
                    kwh = (kwh + kwh_allowed * kwh_ratio) if kwh_allowed < 0.0 else kwh_allowed * kwh_ratio

                if kwh > min_charging_kwh:
                    if hour not in chargeHours:
                        chargeHours[hour] = {
                            "Cost": 0.0,
                            "kWh": 0.0,
                            "battery_level": 0.0,
                            "Price": round(price, 2),
                            "ChargingAmps": self.hour_charging_amps(hour)
                        }

                    if (kwhNeeded * kwh_ratio - kwh) < 0.0:
                        kwh = kwhNeeded * kwh_ratio
                        kwhNeeded = 0.0
                    else:
                        kwhNeeded = round(kwhNeeded - kwh / kwh_ratio, 2)

                    cost = kwh * price
                    totalCost = totalCost + cost
                    battery_level_added = self.kwh_to_percentage(kwh, include_charging_loss = True, kw = hour_kw)

                    chargeHours[hour]['Cost'] = round(chargeHours[hour]['Cost'] + cost, 2)
                    chargeHours[hour]['kWh'] = round(chargeHours[hour]['kWh'] + kwh, 2)
                    chargeHours[hour]['battery_level'] = round(chargeHours[hour]['battery_level'] + battery_level_added, 2)
                    totalkWh = round(totalkWh + kwh, 2)

                    if ultra_cheap_price:
                        rules.append("half_min_avg_price")
                    elif very_cheap_price:
                        rules.append("under_min_avg_price")

                    if rules == []:
                        rules.append("no_rule")

                    for rule in rules:
                        if rule not in chargeHours[hour]:
                            chargeHours[hour][rule] = True

                    _LOGGER.info(f"Adding {hour} {price} valuta/kWh {chargeHours[hour]['battery_level']}% {chargeHours[hour]['kWh']}kWh with keys {rules}")
            except Exception as e:
                _LOGGER.error(f"Error adding to charge hours kwhNeeded:{kwhNeeded} totalCost:{totalCost} totalkWh:{totalkWh} hour:{hour} price:{price} very_cheap_price:{very_cheap_price} ultra_cheap_price:{ultra_cheap_price} kwh_available:{kwh_available} battery_level:{battery_level} check_max_charging_plan:{check_max_charging_plan} max_recommended_battery_level:{max_recommended_battery_level} rules:{rules} error:{e} {type(e)}")
                self.persistent_notification(
                    f"Error adding to charge hours kwhNeeded:{kwhNeeded} totalCost:{totalCost} totalkWh:{totalkWh} hour:{hour} price:{price} very_cheap_price:{very_cheap_price} ultra_cheap_price:{ultra_cheap_price} kwh_available:{kwh_available} battery_level:{battery_level} check_max_charging_plan:{check_max_charging_plan} max_recommended_battery_level:{max_recommended_battery_level} rules:{rules} error:{e} {type(e)}",
                    title=f"{self.title} error",
                    notification_id=f"{self.name}_{func_name}_{sub_func_name}_error"
                )

            return [kwhNeeded, totalCost, totalkWh, battery_level_added, cost]

        cheap_average_price = []  # the average of the lowest prices, calculated once
        fill_up_price_limits = []

        def fill_up_price_limit():
            """Fill up above the floor only in the cheapest FILL_UP_PRICE_FRACTION of the known hours."""
            if not fill_up_price_limits:
                prices = sorted(grid_prices.values())
                fill_up_price_limits.append(prices[max(int(len(prices) * FILL_UP_PRICE_FRACTION) - 1, 0)] if prices else 0.0)
            return fill_up_price_limits[0]

        def fill_up_cheap_enough(timestamp, price):
            """A cheap hour for filling up. The planner goes day by day, so when a cheaper hour
            comes before the end of the next day (any time of day), the next day fills up instead."""
            if price > fill_up_price_limit():
                return False
            hour = reset_time_to_hour(timestamp)
            horizon = end_of_day(hour) + datetime.timedelta(days=1)
            ahead = [p for t, p in grid_prices.items() if hour < t <= horizon]
            return not ahead or price <= min(ahead) * (1 + FILL_UP_LOOK_AHEAD_TOLERANCE)

        def cheap_price_check(price):

            very_cheap_price = False
            ultra_cheap_price = False

            try:
                if not cheap_average_price:
                    lowest_values = sorted(grid_prices.values())[:self.config['database']['kwh_avg_prices_db_data_to_save']]
                    cheap_average_price.append(round(average(lowest_values), 3))
                average_price = cheap_average_price[0]
                if round(price, 3) <= average_price:
                    very_cheap_price = True
                if round(price, 3) <= (average_price * 0.75):
                    ultra_cheap_price = True
            except Exception as e:
                if "min" not in self.kwh_avg_prices_db or not isinstance(self.kwh_avg_prices_db['min'], list) or len(self.kwh_avg_prices_db['min']) < 1:
                    _LOGGER.error(f"Missing min in self.kwh_avg_prices_db or not a list or len < 1: {self.kwh_avg_prices_db}")

                _LOGGER.warning(f"Using local low prices to calc very/ultra cheap price: {e} {type(e)}")
                forecast_price = round(self.get_forecast_value(self.kwh_avg_prices_db['min']), 3)
                if round(price, 3) <= forecast_price:
                    very_cheap_price = True
                if round(price, 3) <= (forecast_price * 0.75):
                    ultra_cheap_price = True

            return [very_cheap_price, ultra_cheap_price]

        def remove_solar_prediction_from_charge_hours(timestamp, day, battery_level_added, solar_percentage_prediction = None):
            sub_func_name = "remove_solar_prediction_from_charge_hours"

            try:
                if solar_percentage_prediction is None:
                    solar_percentage_prediction = sum(charging_plan[day]['solar_prediction'])

                if timestamp in energy_prediction_db["solar_prediction_timestamps"]:
                    solar_percentage_added = min(battery_level_added, solar_percentage_prediction)
                    solar_kwh_added = self.percentage_to_kwh(solar_percentage_added, include_charging_loss=True)

                    _LOGGER.info(f"Added {timestamp} charging, removing solar prediction {solar_kwh_added}kWh {solar_percentage_added}% from day {day}")

                    charging_plan[day]['solar_prediction'].append(-abs(solar_percentage_added))
                    charging_plan[day]['solar_kwh_prediction'].append(-abs(solar_kwh_added))

                    if solar_percentage_prediction is not None:
                        for key in ("solar_prediction", "solar_kwh_prediction"):
                            total = sum(charging_plan[day][key])
                            if total < 0.0:
                                charging_plan[day][key].append(-total)
            except Exception as e:
                _LOGGER.error(f"Error in {sub_func_name} timestamp:{timestamp} day:{day} battery_level_added:{battery_level_added} solar_percentage_prediction:{solar_percentage_prediction} error:{e} {type(e)}")
                self.persistent_notification(
                    f"Error in {sub_func_name} timestamp:{timestamp} day:{day} battery_level_added:{battery_level_added} solar_percentage_prediction:{solar_percentage_prediction} error:{e} {type(e)}",
                    title=f"{self.title} error",
                    notification_id=f"{self.name}_{func_name}_{sub_func_name}_error"
                )
        def future_charging(totalCost, totalkWh):
            sub_func_name = "future_charging"


            def what_battery_level(what_day, hour, price, day, max_level=None):
                """max_level: a higher limit than the max recommended (the full charge for battery health)."""
                sub_sub_func_name = "what_battery_level"

                battery_level_id = "battery_level_at_midnight"
                max_recommended_charge_limit_battery_level = max_level if max_level is not None else self.get_max_recommended_charge_limit_battery_level()
                return_fail_list = [False, max_recommended_charge_limit_battery_level]

                try:
                    if what_day < 0:
                        _LOGGER.warning(f"Error in grid_prices: {hour} is before current time {self.now()} continue to next cheapest hour/price")
                        return return_fail_list

                    total_trip_battery_level_needed = charging_plan[day]["trip_battery_level_needed"] + charging_plan[day]["trip_battery_level_above_max"]

                    if charging_plan[day]["trip"] and in_between(day - what_day, 0, 2) and max_recommended_charge_limit_battery_level < total_trip_battery_level_needed: # if charging_plan[day]["trip"] and in_between(day - what_day, 1, 0) and max_recommended_charge_limit_battery_level < total_trip_battery_level_needed:
                        max_recommended_charge_limit_battery_level = total_trip_battery_level_needed

                    what_day_battery_level_before_work = sum(charging_plan[what_day]['battery_level_before_work'])
                    what_day_battery_level_after_work = max(sum(charging_plan[what_day]['battery_level_after_work']), sum(charging_plan[what_day]['battery_level_at_midnight']))
                    after_what_day_battery_level_after_work = max(sum(charging_plan[min(what_day + 1, 7)]['battery_level_after_work']), sum(charging_plan[min(what_day + 1, 7)]['battery_level_at_midnight']))

                    if price >= 0.0:
                        if charging_plan[what_day]['workday'] and hour < charging_plan[what_day]['work_goto']:
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
                        notification_id=f"{self.name}_{func_name}_{sub_func_name}_{sub_sub_func_name}_error"
                    )
                    raise Exception(f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} price:{price} day:{day}: {e} {type(e)}")

                return [battery_level_id, max_recommended_charge_limit_battery_level]

            def add_charging_session_to_day(hour, what_day, battery_level_id):
                sub_sub_func_name = "add_charging_session_to_day"

                charging_sessions_id = "battery_level_before_work" if battery_level_id == "battery_level_before_work" else "battery_level_after_work"

                try:
                    if battery_level_id not in charging_plan[what_day]['charging_sessions']:
                        charging_plan[what_day]['charging_sessions'][charging_sessions_id] = {}
                    charging_plan[what_day]['charging_sessions'][charging_sessions_id][hour] = chargeHours[hour]
                except Exception as e:
                    _LOGGER.error(f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} battery_level_id:{battery_level_id}: {e} {type(e)}")
                    self.persistent_notification(
                        f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} battery_level_id:{battery_level_id}: {e} {type(e)}",
                        title=f"{self.title} error",
                        notification_id=f"{self.name}_{func_name}_{sub_func_name}_{sub_sub_func_name}_error"
                    )
                    raise Exception(f"Error in {sub_sub_func_name} what_day:{what_day} hour:{hour} battery_level_id:{battery_level_id}: {e} {type(e)}")

                return charging_sessions_id

            def add_charging_to_days(day, what_day, charging_sessions_id, battery_level_added):
                sub_sub_func_name = "add_charging_to_days"

                try:
                    if what_day > day:
                        # the hour just after midnight (the +1 hour window): the next day starts from day's
                        # midnight level, its own lists are filled when that day is planned
                        charging_plan[day]['battery_level_at_midnight'].append(battery_level_added)
                        return
                    for i in range(max(what_day - 1, 0), day + 1):
                        if charging_sessions_id == "battery_level_before_work":
                            if i == max(what_day - 1, 0) and day > 0:
                                charging_plan[i]['battery_level_at_midnight'].append(battery_level_added)

                            if  i >= what_day:
                                charging_plan[i]['battery_level_at_midnight'].append(battery_level_added)
                                charging_plan[i]['battery_level_after_work'].append(battery_level_added)
                                charging_plan[i]['battery_level_before_work'].append(battery_level_added)
                        elif charging_sessions_id == "battery_level_after_work":
                            if i >= what_day:
                                charging_plan[i]['battery_level_at_midnight'].append(battery_level_added)
                                charging_plan[i]['battery_level_after_work'].append(battery_level_added)

                            if i > what_day:
                                charging_plan[i]['battery_level_before_work'].append(battery_level_added)
                except Exception as e:
                    _LOGGER.error(f"Error in {sub_sub_func_name} what_day:{what_day} day:{day} charging_sessions_id:{charging_sessions_id} battery_level_added:{battery_level_added}: {e} {type(e)}")
                    self.persistent_notification(
                        f"Error in {sub_sub_func_name} what_day:{what_day} day:{day} charging_sessions_id:{charging_sessions_id} battery_level_added:{battery_level_added}: {e} {type(e)}",
                        title=f"{self.title} error",
                        notification_id=f"{self.name}_{func_name}_{sub_func_name}_{sub_sub_func_name}_error"
                    )
                    raise Exception(f"Error in {sub_sub_func_name} what_day:{what_day} day:{day} charging_sessions_id:{charging_sessions_id} battery_level_added:{battery_level_added}: {e} {type(e)}")

            def battery_level_full_on_next_departure(what_day):
                sub_sub_func_name = "battery_level_full_on_next_departure"

                try:
                    if what_day == 7:
                        return False

                    for day in range(what_day + 1,8):
                        if charging_plan[day]['trip']:
                            return False

                        if charging_plan[day]['workday']:
                            if round(sum(charging_plan[day]["battery_level_before_work"]),0) >= self.get_max_recommended_charge_limit_battery_level():
                                return True
                            break
                except Exception as e:
                    _LOGGER.error(f"Error in battery_level_full_on_next_departure what_day:{what_day}: {e} {type(e)}")
                    self.persistent_notification(
                        f"Error in battery_level_full_on_next_departure what_day:{what_day}: {e} {type(e)}",
                        title=f"{self.title} error",
                        notification_id=f"{self.name}_{func_name}_{sub_func_name}_{sub_sub_func_name}_error"
                    )

                return False

            def scheduled_charging_added(day, what_day, timestamp, battery_level_id, battery_level_added, cost_added):
                """Book an hour added by scheduled_planner: solar, the cost of trip/work and the battery levels."""
                remove_solar_prediction_from_charge_hours(timestamp, day, battery_level_added)

                total_trip_battery_level_needed = charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max']
                battery_level_sum = total_trip_battery_level_needed + charging_plan[day]['work_battery_level_needed']

                if battery_level_sum > 0.0:
                    if "trip" in charging_plan[day]['rules']:
                        cost_trip = (total_trip_battery_level_needed / battery_level_sum) * cost_added
                        charging_plan[day]['trip_total_cost'] += cost_trip

                    for rule in charging_plan[day].get('rules', []):
                        if 'workday_preparation' in rule:
                            cost_work = (charging_plan[day]['work_battery_level_needed'] / battery_level_sum) * cost_added
                            charging_plan[day]['work_total_cost'] += cost_work
                            break

                charging_sessions_id = add_charging_session_to_day(timestamp, what_day, battery_level_id)
                add_charging_to_days(day, what_day, charging_sessions_id, battery_level_added)

            def scheduled_planner(day):
                sub_sub_func_name = "scheduled_planner"

                nonlocal totalCost, totalkWh, last_charging

                try:
                    if charging_plan[day]['workday'] or charging_plan[day]['trip']:
                        if charging_plan[day]['workday']:
                            last_charging = charging_plan[day]['work_last_charging']
                            next_departure = charging_plan[day]['work_goto']

                        if charging_plan[day]['trip']:
                            last_charging = min(charging_plan[day]['work_last_charging'], charging_plan[day]['trip_last_charging']) if charging_plan[day]['workday'] else charging_plan[day]['trip_last_charging']
                            next_departure = min(charging_plan[day]['work_goto'], charging_plan[day]['trip_goto']) if charging_plan[day]['workday'] else charging_plan[day]['trip_goto']

                        kwh_needed_today = max(self.kwh_needed_for_charging(charging_plan[day]['work_battery_level_needed'] + self.get_min_daily_battery_level(), sum(charging_plan[day]['battery_level_before_work'])), 0.0)
                        charging_plan[day]['work_kwh_needed'] = kwh_needed_today
                        kwh_needed_today += charging_plan[day]['trip_kwh_needed']

                        #Workaround for cold weather
                        if kwh_needed_today <= max((self.config['ev_car']['battery_size'] / 100), 0.3):
                            kwh_needed_today = 0.0

                        while_count = 0
                        while_loop = True

                        while while_loop and while_count < 2:
                            for timestamp, price in sorted_by_cheapest_price:
                                if charging_plan[day]['workday'] and charging_plan[day]['trip'] and while_count == 0:
                                    #_LOGGER.info(f"DEBUG {timestamp} {price} {charging_plan[day]['work_homecoming']} < {charging_plan[day]['trip_last_charging']} {charging_plan[day]['work_homecoming'] < charging_plan[day]['trip_last_charging']} {sum(charging_plan[day]['battery_level_before_work'])} >= ({charging_plan[day]['work_battery_level_needed']} + {self.get_min_daily_battery_level()}) {sum(charging_plan[day]['battery_level_before_work']) >= (charging_plan[day]['work_battery_level_needed'] + self.get_min_daily_battery_level())}")

                                    if (charging_plan[day]['work_homecoming'] < charging_plan[day]['trip_last_charging']
                                        and sum(charging_plan[day]['battery_level_before_work']) >= (charging_plan[day]['work_battery_level_needed'] + self.get_min_trip_battery_level())):
                                        last_charging = charging_plan[day]['trip_last_charging']
                                        _LOGGER.info(f"Enought battery level for work, planning for trip {sum(charging_plan[day]['battery_level_before_work'])} >= ({charging_plan[day]['work_battery_level_needed']} + {self.get_min_trip_battery_level()})")
                                        break

                                timestamp = change_timestamp_with_minutes(timestamp)

                                if not in_between(timestamp, current_hour, last_charging + datetime.timedelta(hours=1)):
                                    continue

                                hour_in_chargeHours, kwh_available = kwh_available_in_hour(timestamp)
                                if hour_in_chargeHours and not kwh_available:
                                    continue

                                what_day = days_between(self.now(), timestamp)
                                if what_day < 0:
                                    continue

                                battery_level_id, max_recommended_charge_limit_battery_level = what_battery_level(what_day, timestamp, price, day)
                                if not battery_level_id:
                                    _LOGGER.debug(f"battery_level_id not found for day ({what_day}) {timestamp} {price}. continue to next cheapest timestamp/price")
                                    continue

                                if battery_level_full_on_next_departure(what_day):
                                    _LOGGER.debug(f"Max battery level reached for day ({what_day}) before work {timestamp} {price}. continue to next cheapest timestamp/price")
                                    continue

                                working, on_trip = available_for_charging_prediction(timestamp, trip_date_time, trip_homecoming_date_time)
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

                                    kwh_needed_today, totalCost, totalkWh, battery_level_added, cost_added = add_to_charge_hours(kwh_needed_today, totalCost, totalkWh, timestamp, price, None, None, kwh_available, sum(charging_plan[what_day][battery_level_id]), check_max_charging_plan={"day": day, "what_day": what_day, "battery_level_id": battery_level_id}, max_recommended_battery_level=max_recommended_charge_limit_battery_level, rules=charging_plan[day]['rules'])

                                    if timestamp in chargeHours and battery_level_added:
                                        scheduled_charging_added(day, what_day, timestamp, battery_level_id, battery_level_added, cost_added)
                                else:
                                    while_loop = False
                                    break
                            while_count += 1

                        if kwh_needed_today > 0.0 and self.kwh_to_percentage(kwh_needed_today, include_charging_loss = True) > 0.0:
                            _LOGGER.warning(f"Not enought charging planned for day {day} kwh_needed_today {kwh_needed_today}kWh, using all available hours until {next_departure} to fullfill needed kWh")
                            for i in range(hours_between(current_hour, next_departure), 0, -1):
                                timestamp = next_departure - datetime.timedelta(hours=i)
                                hour_in_chargeHours, kwh_available = kwh_available_in_hour(timestamp)
                                if hour_in_chargeHours and not kwh_available:
                                    continue

                                what_day = days_between(self.now(), timestamp)
                                if what_day < 0:
                                    continue

                                battery_level_id, max_recommended_charge_limit_battery_level = what_battery_level(what_day, timestamp, price, day)
                                if not battery_level_id:
                                    continue

                                if round(kwh_needed_today, 1) > 0.0 and self.kwh_to_percentage(kwh_needed_today, include_charging_loss = True) > 0.0:
                                    '''if round(sum(charging_plan[what_day][battery_level_id]), 0) >= self.get_max_recommended_charge_limit_battery_level() - 1.0:
                                        #Workaround for cold battery percentage: ex. 90% normal temp = 89% cold temp
                                        continue'''
                                    kwh_needed_today, totalCost, totalkWh, battery_level_added, cost_added = add_to_charge_hours(kwh_needed_today, totalCost, totalkWh, timestamp, price, None, None, kwh_available, sum(charging_plan[what_day][battery_level_id]), check_max_charging_plan={"day": day, "what_day": what_day, "battery_level_id": battery_level_id}, max_recommended_battery_level=max_recommended_charge_limit_battery_level, rules=charging_plan[day]['rules'])

                                    if timestamp in chargeHours and battery_level_added:
                                        scheduled_charging_added(day, what_day, timestamp, battery_level_id, battery_level_added, cost_added)
                            if round(kwh_needed_today, 1) > 0.0 and self.kwh_to_percentage(kwh_needed_today, include_charging_loss = True) > 0.0:
                                _LOGGER.warning(f"Battery level above max recommended charge limit {self.get_max_recommended_charge_limit_battery_level()}%, could not fullfill needed kWh for day {day}, left kwh_needed_today {kwh_needed_today}kWh")
                except Exception as e:
                    _LOGGER.error(f"Error in scheduled_planner day:{day}: {e} {type(e)}")
                    self.persistent_notification(
                        f"Error in scheduled_planner day:{day}: {e} {type(e)}",
                        title=f"{self.title} error",
                        notification_id=f"{self.name}_{func_name}_{sub_func_name}_{sub_sub_func_name}_error"
                    )
                    raise Exception(f"Error in scheduled_planner day:{day}: {e} {type(e)}")

            def unscheduled_planner(day):

                nonlocal totalCost, totalkWh

                try:
                    need_recommended_full_charge = days_need_between_recommended_full_charge != 0 and days_since_last_fully_charged > days_need_between_recommended_full_charge and day == 1

                    if need_recommended_full_charge:
                        _LOGGER.info(f"Days since last fully charged: {days_since_last_fully_charged}. Need full charge: {need_recommended_full_charge}")
                    fill_up = self.fill_up_charging_enabled()
                    cheap_grid_charging = self.cheap_grid_charging_enabled()

                    if fill_up or need_recommended_full_charge or cheap_grid_charging:
                        midnight_battery_level = sum(charging_plan[day]['battery_level_at_midnight'])

                        # fill up: the floor (the day's driving + min daily level) in the cheapest hours,
                        # the rest up to the max recommended level only in the cheap hours of the week
                        fill_up_floor_kwh = 0.0
                        fill_up_top_kwh = 0.0
                        if fill_up:
                            fill_up_floor_kwh = self.kwh_needed_for_charging(charging_plan[day]['offday_battery_level_needed'] + self.get_min_daily_battery_level(), midnight_battery_level)
                            fill_up_top_kwh = self.kwh_needed_for_charging(self.get_max_recommended_charge_limit_battery_level(), midnight_battery_level)

                        full_charge_kwh = 0.0
                        if need_recommended_full_charge:
                            # from the day's own level (after the earlier days' charging and driving)
                            full_charge_kwh = self.kwh_needed_for_charging(100.0, midnight_battery_level)
                            _LOGGER.info(f"Needed for full charge: {round(full_charge_kwh,1)}kWh")

                        if cheap_grid_charging:
                            # after the scheduled planner, so its charging counts
                            very_cheap_kwh_needed_today = self.kwh_needed_for_charging(very_cheap_max_battery_level, midnight_battery_level)
                            ultra_cheap_kwh_needed_today = self.kwh_needed_for_charging(ultra_cheap_max_battery_level, midnight_battery_level)
                        else:
                            very_cheap_kwh_needed_today = ultra_cheap_kwh_needed_today = 0.0

                        def kwh_still_needed():
                            return max(fill_up_floor_kwh, fill_up_top_kwh, full_charge_kwh, very_cheap_kwh_needed_today, ultra_cheap_kwh_needed_today)

                        if kwh_still_needed() > 0.0:
                            for timestamp, price in sorted_by_cheapest_price:

                                if not in_between(timestamp, current_hour, last_charging + datetime.timedelta(hours=1)):
                                    continue

                                timestamp = change_timestamp_with_minutes(timestamp)

                                hour_in_chargeHours, kwh_available = kwh_available_in_hour(timestamp)
                                if hour_in_chargeHours and not kwh_available:
                                    continue

                                what_day = days_between(self.now(), timestamp)
                                if what_day < 0:
                                    continue

                                # the full charge may go above the max recommended level (else the hour is refused at it)
                                full_charge_hour = full_charge_kwh > 0.01
                                battery_level_id, max_recommended_charge_limit_battery_level = what_battery_level(what_day, timestamp, price, day, max_level=100.0 if full_charge_hour else None)

                                if not battery_level_id:
                                    continue

                                if full_charge_hour:
                                    if not self.is_ev_configured(): #Sets battery level to 0.0 at midnight
                                        battery_level_id = "battery_level_at_midnight"

                                        '''if battery_level_id not in charging_plan[what_day]:
                                            battery_level_id = "battery_level_after_work"

                                        if battery_level_id not in charging_plan[what_day]:
                                            battery_level_id = "battery_level_before_work"'''

                                very_cheap_price, ultra_cheap_price = cheap_price_check(price)

                                # 0.01: a cold battery may stop at 99%, don't keep asking the car for a tiny rest
                                if kwh_still_needed() <= 0.01:
                                    break

                                full_charge_allowed = full_charge_hour and (very_cheap_price or ultra_cheap_price or not charging_plan['workday_in_week'])
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
                                elif fill_up_top_kwh > 0.01 and fill_up_cheap_enough(timestamp, price):
                                    kwh_needed = fill_up_top_kwh
                                    hour_rules = ["fill_up"]
                                elif ultra_cheap_price and ultra_cheap_kwh_needed_today > 0.01:
                                    kwh_needed = ultra_cheap_kwh_needed_today
                                    hour_rules = []
                                    max_recommended_charge_limit_battery_level = min(max_recommended_charge_limit_battery_level, ultra_cheap_max_battery_level)
                                elif very_cheap_price and very_cheap_kwh_needed_today > 0.01:
                                    kwh_needed = very_cheap_kwh_needed_today
                                    hour_rules = []
                                    max_recommended_charge_limit_battery_level = min(max_recommended_charge_limit_battery_level, very_cheap_max_battery_level)
                                else:
                                    continue

                                kwh_left, totalCost, totalkWh, battery_level_added, cost_added = add_to_charge_hours(kwh_needed, totalCost, totalkWh, timestamp, price, very_cheap_price, ultra_cheap_price, kwh_available, sum(charging_plan[what_day][battery_level_id]), check_max_charging_plan={"day": day, "what_day": what_day, "battery_level_id": battery_level_id}, max_recommended_battery_level=max_recommended_charge_limit_battery_level, rules=hour_rules)
                                # the charged kWh count for every target level
                                kwh_added = self.percentage_to_kwh(battery_level_added, include_charging_loss=True)
                                fill_up_floor_kwh -= kwh_added
                                fill_up_top_kwh -= kwh_added
                                full_charge_kwh -= kwh_added
                                very_cheap_kwh_needed_today -= kwh_added
                                ultra_cheap_kwh_needed_today -= kwh_added
                                remove_solar_prediction_from_charge_hours(timestamp, day, battery_level_added)

                                if timestamp in chargeHours and battery_level_added:
                                    charging_sessions_id = add_charging_session_to_day(timestamp, what_day, battery_level_id)
                                    add_charging_to_days(day, what_day, charging_sessions_id, battery_level_added)
                                    if charging_plan[day]['offday']:
                                        charging_plan[day]['offday_total_cost'] += cost_added
                except Exception as e:
                    error_message = f"Error in fill up or need recommended full charge for day {day}: {e} {type(e)}"
                    _LOGGER.warning(error_message)

                    self.persistent_notification(error_message, f"{self.title} error", notification_id=f"{self.name}_{func_name}_{sub_func_name}_fill_up_or_need_recommended_full_charge_error")

            def departure_planner(day):

                nonlocal solar_unit, ignored_reference_battery_level

                try:
                    solar_kwh = 0.0
                    solar_percentage = 0.0
                    solar_unit = 0.0

                    if sum(unused_solar_kwh.values()) > 0.0:
                        total_solar_kwh = sum(unused_solar_kwh.values())
                        total_solar_price = sum(unused_solar_cost.values())

                        if total_solar_kwh > 0.0:
                            solar_unit = total_solar_price / total_solar_kwh

                        solar_percentage = self.kwh_to_percentage(total_solar_kwh, include_charging_loss=True)

                    temp_events = []
                    for event_type in ('workday', 'trip', 'offday'):
                        from_battery = 0.0
                        from_battery_solar = 0.0
                        if charging_plan[day][event_type]:
                            if event_type in ('workday', 'trip') and charging_plan[day][event_type]:
                                goto_key = 'trip_goto' if event_type == 'trip' else 'work_goto'
                                event_time_start = charging_plan[day][goto_key]
                                event_time_end = charging_plan[day]['trip_homecoming'] if event_type == 'trip' else charging_plan[day]['work_homecoming']

                                emoji = emoji_parse({'trip': True}) if event_type == 'trip' else charging_plan[day]['emoji']
                                battery_level_needed_key = 'trip_battery_level_needed' if event_type == 'trip' else 'work_battery_level_needed'
                                battery_level_needed = charging_plan[day][battery_level_needed_key] if battery_level_needed_key =="work_battery_level_needed" else charging_plan[day][battery_level_needed_key] + charging_plan[day]["trip_battery_level_above_max"]
                                kwh_needed = charging_plan[day]['trip_kwh_needed' if event_type == 'trip' else 'work_kwh_needed']
                                range_needed = (trip_range or 0.0) if event_type == 'trip' else charging_plan[day]['work_range_needed']
                                cost = charging_plan[day]["trip_total_cost" if event_type == "trip" else "work_total_cost"]
                            elif event_type == 'offday' and charging_plan[day][event_type] and day > 0:
                                event_time_start = charging_plan[day]['start_of_day']
                                event_time_end = charging_plan[day]['end_of_day']

                                emoji = charging_plan[day]['emoji']
                                battery_level_needed = charging_plan[day]['offday_battery_level_needed']
                                kwh_needed = max(charging_plan[day]['offday_kwh_needed'], 0.0)
                                range_needed = charging_plan[day]['offday_range_needed']
                                cost = charging_plan[day]["offday_total_cost"]
                            else:
                                continue

                            unused_solar_kwh.clear()
                            unused_solar_cost.clear()

                            if solar_percentage > battery_level_needed:
                                diff = solar_percentage - battery_level_needed
                                diff_kwh = self.percentage_to_kwh(diff, include_charging_loss=True)
                                unused_solar_kwh[self.time_plus_days(day).date()] = diff_kwh
                                unused_solar_cost[self.time_plus_days(day).date()] = diff_kwh * solar_unit

                            solar_percentage = min(solar_percentage, battery_level_needed)
                            grid_battery_level_needed = battery_level_needed - solar_percentage

                            solar_kwh += self.percentage_to_kwh(solar_percentage, include_charging_loss=True)
                            solar_cost = solar_unit * solar_kwh
                            cost += solar_cost

                            if event_type in ('workday', 'trip'):
                                kwh_needed += solar_kwh
                            elif event_type == 'offday':
                                #kwh_needed = max(charging_plan[day]['offday_kwh_needed'] - kwh_needed, 0.0)
                                kwh_needed += solar_kwh

                            battery_level_expenses_solar_percentage_loop = 0.0
                            battery_level_expenses_solar_kwh_loop = 0.0
                            reference_battery_level = self.get_min_daily_battery_level() if event_type in ("workday", "offday") else self.get_min_trip_battery_level()

                            battery_level_expenses_percentage = battery_expenses.get("battery_level_expenses_percentage", 0.0)
                            battery_level_expenses_solar_percentage = battery_expenses.get("battery_level_expenses_solar_percentage", 0.0)

                            for key in sorted([k for k in battery_expenses.keys() if type(k) is datetime.datetime]):

                                if ignored_reference_battery_level < reference_battery_level:
                                    ignored_reference_battery_level += battery_expenses[key]["percentage"]

                                    if ignored_reference_battery_level > reference_battery_level:
                                        diff = ignored_reference_battery_level - reference_battery_level

                                        amount = diff / battery_expenses[key]["percentage"] if battery_expenses[key]["percentage"] > 0.0 else 0.0
                                        battery_expenses[key]["percentage"] *= amount
                                        battery_expenses[key]["solar_percentage"] *= amount

                                    battery_level_expenses_percentage -= battery_expenses[key]["percentage"]

                                if ignored_reference_battery_level >= reference_battery_level and grid_battery_level_needed > 0.0 and battery_level_expenses_percentage > 0.0:
                                    battery_level_expenses_grid_amount = min(grid_battery_level_needed, battery_expenses[key]["percentage"])
                                    grid_battery_level_needed -= battery_level_expenses_grid_amount
                                    battery_level_expenses_percentage -= battery_level_expenses_grid_amount

                                    battery_level_expenses_grid_percentage_loop = battery_level_expenses_grid_amount
                                    battery_level_expenses_grid_kwh_loop = self.percentage_to_kwh(battery_level_expenses_grid_percentage_loop, include_charging_loss=True)
                                    battery_level_expenses_grid_cost_loop = battery_expenses['battery_level_expenses_unit'] * battery_level_expenses_grid_kwh_loop

                                    kwh_needed += battery_level_expenses_grid_kwh_loop
                                    cost += battery_level_expenses_grid_cost_loop

                                    if round(battery_level_expenses_grid_kwh_loop, 1) > 0.0:
                                        from_battery += battery_level_expenses_grid_kwh_loop

                                    if battery_level_expenses_solar_percentage > 0.0:
                                        battery_level_expenses_solar_amount = min(battery_level_expenses_grid_percentage_loop, battery_expenses[key]["solar_percentage"])
                                        battery_level_expenses_solar_percentage -= battery_level_expenses_solar_amount

                                        battery_level_expenses_solar_percentage_loop += min(battery_level_expenses_solar_amount, grid_battery_level_needed)
                                        battery_level_expenses_solar_kwh_loop += self.percentage_to_kwh(battery_level_expenses_solar_amount, include_charging_loss=True)

                                        if round(battery_level_expenses_solar_kwh_loop, 1) > 0.0:
                                            from_battery_solar = battery_level_expenses_solar_kwh_loop

                                battery_expenses[key]["percentage"] = round(battery_expenses[key]["percentage"], 1)
                                battery_expenses[key]["solar_percentage"] = round(battery_expenses[key]["solar_percentage"], 1)
                                battery_expenses["battery_level_expenses_percentage"] = round(battery_expenses["battery_level_expenses_percentage"], 1)
                                battery_expenses["battery_level_expenses_solar_percentage"] = round(battery_expenses["battery_level_expenses_solar_percentage"], 1)


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
                        work_overview[work_overview_key] = event["data"]
                except Exception as e:
                    _LOGGER.warning(f"Cant create work overview for day {day}: {e} {type(e)}")

            def alternate_charging_planner(day):

                try: #Alternative charging estimate

                    dates = [date for date in [charging_plan[day]['work_homecoming'], charging_plan[day]['trip_homecoming'], self.now() if day == 0 and charger_status in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS else None] if date is not None]
                    homecoming_alternative = min(dates) if dates else charging_plan[day_before]['start_of_day']

                    dates = [date for date in [charging_plan[day_after]['work_last_charging'], charging_plan[day_after]['trip_last_charging'], charging_plan[day]['trip_last_charging']] if date is not None]
                    last_charging_alternative = min(dates) if dates else charging_plan[day_after]['end_of_day']

                    if now <= last_charging_alternative and day < 7:
                        if day == 0:
                            used_battery_level_alternative = max(self.get_max_recommended_charge_limit_battery_level() - self.battery_level(), 0.0)
                        else:
                            work_battery_level_needed_alternative = charging_plan[day]['work_battery_level_needed']
                            trip_battery_level_needed_alternative = charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max'] + charging_plan[day_after]['trip_battery_level_needed'] + charging_plan[day_after]['trip_battery_level_above_max']
                            typical_daily_battery_level_needed_alternative = self.calc_distance_to_battery_level(self.get_entity_daily_distance()) if self.fill_up_charging_enabled() else 0.0
                            total_battery_level_needed_alternative = work_battery_level_needed_alternative + trip_battery_level_needed_alternative + typical_daily_battery_level_needed_alternative
                            used_battery_level_alternative = max(total_battery_level_needed_alternative, 0.0)

                            if charging_plan[day]["trip"]:
                                diff_min_alternative = max(self.get_min_daily_battery_level() - self.get_min_trip_battery_level(), 0.0) if used_battery_level_alternative > 0.0 else 0.0
                                used_battery_level_alternative += charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max'] + diff_min_alternative

                        kwh_needed_today_alternative = self.kwh_needed_for_charging(used_battery_level_alternative, 0.0)
                        kwh_solar_alternative = min(sum(charging_plan[day_before]['solar_kwh_prediction']), kwh_needed_today_alternative) if day > 0 else 0.0
                        kwh_needed_today_alternative -= kwh_solar_alternative

                        if kwh_solar_alternative > 0.0:
                            solar_price = average(charging_plan[day_before]['solar_cost_prediction'])

                            sunrise = self.get_sun_events(charging_plan[day]['start_of_day'])['sunrise']
                            charge_hours_alternative[sunrise] = {
                                "emoji": emoji_parse({'solar': True}),
                                "day": f"{day} {charging_plan[day]['day_text']}",
                                "used_battery_level_alternative": used_battery_level_alternative,
                                "Price": solar_price,
                                "kWh": kwh_solar_alternative,
                                "Cost": kwh_solar_alternative * solar_price,
                                "solar": True
                            }

                        for timestamp, price in sorted_by_cheapest_price:
                            if timestamp not in charge_hours_alternative and in_between(timestamp, homecoming_alternative - datetime.timedelta(hours=1), last_charging_alternative + datetime.timedelta(hours=1)):
                                working, on_trip = available_for_charging_prediction(timestamp, trip_date_time, trip_homecoming_date_time)
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

                                    emoji = charging_plan[day_after]['emoji'] if charging_plan[day_after]['emoji'] else "⛽"
                                    charge_hours_alternative[timestamp] = {
                                        "emoji": f"{emoji}",
                                        "day": f"{day_after} {charging_plan[day_after]['day_text']}",
                                        "used_battery_level_alternative": used_battery_level_alternative,
                                        "Price": price,
                                        "kWh": kwh,
                                        "Cost": cost
                                    }
                                else:
                                    break
                except Exception as e:
                    _LOGGER.warning(f"Cant create alternative charging estimate for day {day}: {e} {type(e)}")

            def solar_prediction_planner(day):

                try:
                    if self.solar_charging_enabled() and sum(charging_plan[day]['solar_prediction']) > 0.0:
                        date = date_to_string(date = charging_plan[day]['start_of_day'], format = "%d/%m")

                        sun_events = self.get_sun_events(charging_plan[day]['start_of_day'])
                        sunrise = sun_events["sunrise"]
                        sunrise_text = f"{emoji_parse({'sunrise': True})}{date_to_string(date = sunrise, format = '%H:%M')}"
                        sunset = sun_events["sunset"]
                        sunset_text = f"{emoji_parse({'sunset': True})}{date_to_string(date = sunset, format = '%H:%M')}"

                        timeperiod = []
                        if charging_plan[day]['workday']:
                            if sunrise <= charging_plan[day]['work_goto']:
                                timeperiod.append(f"{sunrise_text}-{emoji_parse({'goto': True})}{date_to_string(date = charging_plan[day]['work_goto'], format = '%H:%M')}")
                                if sunset >= charging_plan[day]['work_homecoming']:
                                    timeperiod.append("<br>")
                            if sunset >= charging_plan[day]['work_homecoming']:
                                timeperiod.append(f"{emoji_parse({'homecoming': True})}{date_to_string(date = charging_plan[day]['work_homecoming'], format = '%H:%M')}-{sunset_text}")
                        elif charging_plan[day]['trip']:
                            if sunrise <= charging_plan[day]['trip_goto']:
                                timeperiod.append(f"{sunrise_text}-{emoji_parse({'trip': True})}{date_to_string(date = charging_plan[day]['trip_goto'], format = '%H:%M')}")
                                if sunset >= charging_plan[day]['trip_homecoming']:
                                    timeperiod.append("<br>")
                            if sunset >= charging_plan[day]['trip_homecoming']:
                                timeperiod.append(f"{emoji_parse({'homecoming': True})}{date_to_string(date = charging_plan[day]['trip_homecoming'], format = '%H:%M')}-{sunset_text}")
                        else:
                            timeperiod.append(f"{sunrise_text}-{sunset_text}")

                        solar_over_production[day] = {
                            "day": self.t('ui.calendar.weekday_names.' + day_of_week_text(charging_plan[day]['start_of_day'])),
                            "date": date,
                            "when": " ".join(timeperiod),
                            "emoji": emoji_parse({'solar': True}),
                            "percentage": sum(charging_plan[day]['solar_prediction']),
                            "kWh": sum(charging_plan[day]['solar_kwh_prediction']),
                            "corrected": charging_plan[day]['solar_prediction_corrected'],
                        }
                except Exception as e:
                    _LOGGER.warning(f"Cant create solar over production prediction for day {day}: {e} {type(e)}")

            unused_solar_kwh = {}
            unused_solar_cost = {}
            solar_unit = self.get_solar_sell_price()

            ignored_reference_battery_level = 0.0
            very_cheap_max_battery_level = self.get_very_cheap_grid_charging_max_battery_level()
            ultra_cheap_max_battery_level = self.get_ultra_cheap_grid_charging_max_battery_level()

            days_need_between_recommended_full_charge = self.number_state("full_charge_recommended", error_state=0.0)
            last_full_charge = self.sensor_states.get("last_full_charge")
            days_since_last_fully_charged = days_between(last_full_charge if isinstance(last_full_charge, datetime.datetime) else reset_datetime(), self.now())
            charger_status = self.get_state(self.config['charger']['entity_ids']['status_entity_id'], error_state="unavailable")

            for day in sorted([key for key in charging_plan.keys() if isinstance(key, int)]):
                day_before = max(day - 1, 0)
                day_after = min(day + 1, 7)
                last_charging = end_of_day(self.time_plus_days(day))

                starting_battery_level = min(sum(charging_plan[day_before]['battery_level_at_midnight']), 100.0) if day > 0 else self.battery_level()

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
                    scheduled_planner(day)
                except Exception:  # noqa: BLE001
                    pass

                if self.solar_charging_enabled():
                    if day > 0:
                        unused_solar_kwh[self.time_plus_days(day).date()] = sum(charging_plan[day_before]['solar_kwh_prediction'])
                        unused_solar_cost[self.time_plus_days(day).date()] = sum(charging_plan[day_before]['solar_cost_prediction'])

                unscheduled_planner(day)

                departure_planner(day)
                alternate_charging_planner(day)
                solar_prediction_planner(day)

                if charging_plan[day]['trip']:
                    total_trip_battery_level_needed = max((charging_plan[day]['trip_battery_level_needed'] + charging_plan[day]['trip_battery_level_above_max'] - self.get_min_trip_battery_level()), 0.0) * -1
                    charging_plan[day]['battery_level_at_midnight'].append(total_trip_battery_level_needed)

                if charging_plan[day]['offday']:
                    charging_plan[day]['battery_level_at_midnight'].append(charging_plan[day]['offday_battery_level_needed'] * -1)
            #_LOGGER.error(f"charge_hours_alternative:\n{pformat(charge_hours_alternative, width=200, compact=True)}")
            return [totalCost, totalkWh]

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

        def charging_plan_warnings_add(plan, warning):

            if plan not in charging_plan_warnings:
                charging_plan_warnings[plan] = []
            charging_plan_warnings[plan].append(warning)

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
                        charging_plan_warnings_add("work", self.t('ui.cheap_grid_charge_hours.work_goto_equals_work_homecoming', day=self.t(f'ui.calendar.weekday_names.{day_of_week_text(end_of_day_datetime)}')))

                    if charging_plan[day]['work_range_needed'] == 0.0:
                        charging_plan_warnings_add("work", self.t('ui.cheap_grid_charge_hours.work_distance_zero', day=self.t(f'ui.calendar.weekday_names.{day_of_week_text(end_of_day_datetime)}')))

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
                            hour_in_chargeHours, kwh_available = kwh_available_in_hour(timestamp)
                            very_cheap_price, ultra_cheap_price = cheap_price_check(grid_prices[timestamp])
                            solar_percentage_prediction = self.kwh_to_percentage(solar_kwh_prediction.get(start_of_day(timestamp), solar_kwh_prediction['avg']), include_charging_loss=True)

                            trip_kwh_needed, totalCost, totalkWh, battery_level_added, cost_added = add_to_charge_hours(trip_kwh_needed, totalCost, totalkWh, timestamp, grid_prices[timestamp], very_cheap_price, ultra_cheap_price, kwh_available, max_recommended_battery_level, None, trip_target_level, ['trip'])
                            kwh_added = self.percentage_to_kwh(battery_level_added, include_charging_loss=False)
                            charging_plan[day]['trip_kwh_needed'] += kwh_added
                            charging_plan[day]["trip_total_cost"] += chargeHours[timestamp]["Cost"]

                            remove_solar_prediction_from_charge_hours(timestamp, day, battery_level_added, solar_percentage_prediction=solar_percentage_prediction)

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
            totalCost, totalkWh = future_charging(totalCost, totalkWh)
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
        def charge_plan_end(timestamp, kwh):
            hour_end = reset_time_to_hour(timestamp) + datetime.timedelta(hours=1)
            capacity = self.hour_kwh_capacity(timestamp)
            if capacity <= 0.0:
                return hour_end
            return min(timestamp + datetime.timedelta(hours=kwh / capacity), hour_end)

        self.set_attr("current_charging_rule", "charge_plan", [
            {"time": timestamp.isoformat(), "end": charge_plan_end(timestamp, value["kWh"]).isoformat(), "kwh": value["kWh"], "battery_level": value["battery_level"], "price": value["Price"], "emoji": emoji_parse(value)}
            for timestamp, value in sorted((k, v) for k, v in chargeHours.items() if isinstance(k, datetime.datetime))
        ])
        battery_forecast = [{"time": now.isoformat(), "battery_level": round(self.battery_level(), 1)}]
        for day in sorted(key for key in charging_plan if isinstance(key, int)):
            plan_day = charging_plan[day]
            points = []
            if plan_day["workday"]:
                points.append((plan_day["work_goto"], plan_day["battery_level_before_work_sum"]))
                points.append((plan_day["work_homecoming"], plan_day["battery_level_after_work_sum"]))
            points.append((plan_day["end_of_day"], plan_day["battery_level_at_midnight_sum"]))
            for time, level in points:
                if time > now:
                    battery_forecast.append({"time": time.isoformat(), "battery_level": round(min(max(level, 0.0), 100.0), 1)})
        self.set_attr("current_charging_rule", "battery_forecast", battery_forecast)

        # the planned distance per day (work, trip and fill up together) for the dashboard chart
        distance_forecast = []
        for day in sorted(key for key in charging_plan if isinstance(key, int)):
            plan_day = charging_plan[day]
            km = 0.0
            if plan_day["workday"]:
                km += plan_day["work_range_needed"]
            if plan_day["trip"] and trip_range:
                km += trip_range
            if plan_day["offday"]:
                km += plan_day["offday_range_needed"]
            start = max(plan_day["start_of_day"], now)
            if plan_day["end_of_day"] > start:
                distance_forecast.append({"time": start.isoformat(), "end": plan_day["end_of_day"].isoformat(), "km": round(km, 0)})
        self.set_attr("current_charging_rule", "distance_forecast", distance_forecast)

        old_charge_hours = deepcopy(self.charge_hours)

        self.charging_plan = charging_plan
        self.charge_hours = chargeHours

        if check_next_24_hours_diff(old_charge_hours, chargeHours, self.now()) and old_charge_hours:
            self.append_overview_output("Charging plan changed, next 24 hours updated")

        overview = []

        try:
            if last_drive_efficiency:
                kr_km = round(last_drive_efficiency['cost'] / last_drive_efficiency['distance'], 2) if last_drive_efficiency['distance'] > 0 else 0.0

                overview.append("<center>\n")
                overview.append(f"## 🛣️ {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.title')} ##")
                overview.append("|  |  |")
                overview.append("|:---|---:|")
                overview.append(f"| **📅 {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.drive_date')}** | **{date_to_string(last_drive_efficiency['timestamp'], format='%d/%m %H:%M')}** |")
                overview.append(f"| **🚗 {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.last_distance')}** | **{round(last_drive_efficiency['distance'], 1):.1f} {self.t('ui.common.distance_type')}** |")
                overview.append(f"| **🔋 {self.t('ui.cheap_grid_charge_hours.last_drive_efficiency.used_battery')}** | **{round(last_drive_efficiency['usedBattery'], 1):.1f}% ({round(last_drive_efficiency['usedkWh'], 2):.2f} kWh)** |")
                overview.append(f"| **💰 {self.t('ui.common.expense')}** | **{round(last_drive_efficiency['cost'], 2):.2f} {self.t('ui.common.valuta')} ({kr_km:.2f} {self.t('ui.common.valuta_distance')})**")
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
