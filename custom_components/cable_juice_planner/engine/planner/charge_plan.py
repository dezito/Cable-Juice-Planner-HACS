"""Helpers of the charging plan: hours, limits, prices and warnings."""
from __future__ import annotations

import datetime
import logging

from ...utils.helpers import average, days_between, end_of_day, in_between, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)

FUNC_NAME = "cheap_grid_charge_hours"
FILL_UP_PRICE_FRACTION = 0.3  # fill up to the max level only in the cheapest 30 % of the known hours
FILL_UP_LOOK_AHEAD_TOLERANCE = 0.1  # ... and only when no hour until the end of the next day is more than 10 % cheaper
FORECAST_DRIVE_TIME = datetime.timedelta(minutes=30)  # dashboard chart only: one way to work or on a trip


class ChargePlan:
    """Helpers of the charging plan: hours, limits, prices and warnings."""

    def change_timestamp_with_minutes(self, charging_plan, timestamp: datetime.datetime):
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
                notification_id=f"{self.name}_{FUNC_NAME}_{sub_func_name}_error"
            )

        return timestamp.replace(minute=max(work_minute, trip_minute, 0))

    def available_for_charging_prediction(self, charging_plan, timestamp: datetime.datetime, trip_datetime = None, trip_homecoming_datetime = None):
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
                notification_id=f"{self.name}_{FUNC_NAME}_{sub_func_name}_error"
            )
        return working, on_trip

    def kwh_available_in_hour(self, chargeHours, hour):
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
                notification_id=f"{self.name}_{FUNC_NAME}_{sub_func_name}_error"
            )
        return [hour_in_chargeHours, kwh_available]

    def check_max_battery_level_allowed(self, charging_plan, day, what_day, battery_level_id, max_recommended_battery_level, battery_level_to_added):
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
                notification_id=f"{self.name}_{FUNC_NAME}_{sub_func_name}_error"
            )

        return self.percentage_to_kwh(battery_level_to_added, include_charging_loss = True)

    def add_to_charge_hours(self, chargeHours, charging_plan, kwhNeeded, totalCost, totalkWh, hour, price, very_cheap_price, ultra_cheap_price, kwh_available, battery_level = None, check_max_charging_plan = None, max_recommended_battery_level = None, rules = None):
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
                kwh_allowed = self.check_max_battery_level_allowed(charging_plan, check_max_charging_plan['day'], check_max_charging_plan['what_day'], check_max_charging_plan['battery_level_id'], max_recommended_battery_level, self.kwh_to_percentage(kwh, include_charging_loss = True, kw = hour_kw))
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
                notification_id=f"{self.name}_{FUNC_NAME}_{sub_func_name}_error"
            )

        return [kwhNeeded, totalCost, totalkWh, battery_level_added, cost]

    def fill_up_price_limit(self, fill_up_price_limits, grid_prices):
        """Fill up above the floor only in the cheapest FILL_UP_PRICE_FRACTION of the known hours."""
        if not fill_up_price_limits:
            prices = sorted(grid_prices.values())
            fill_up_price_limits.append(prices[max(int(len(prices) * FILL_UP_PRICE_FRACTION) - 1, 0)] if prices else 0.0)
        return fill_up_price_limits[0]

    def fill_up_cheap_enough(self, fill_up_price_limits, grid_prices, timestamp, price):
        """A cheap hour for filling up. The planner goes day by day, so when a cheaper hour
        comes before the end of the next day (any time of day), the next day fills up instead."""
        if price > self.fill_up_price_limit(fill_up_price_limits, grid_prices):
            return False
        hour = reset_time_to_hour(timestamp)
        horizon = end_of_day(hour) + datetime.timedelta(days=1)
        ahead = [p for t, p in grid_prices.items() if hour < t <= horizon]
        return not ahead or price <= min(ahead) * (1 + FILL_UP_LOOK_AHEAD_TOLERANCE)

    def cheap_price_check(self, cheap_average_price, grid_prices, price):

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

    def remove_solar_prediction_from_charge_hours(self, charging_plan, energy_prediction_db, timestamp, day, battery_level_added, solar_percentage_prediction = None):
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
                notification_id=f"{self.name}_{FUNC_NAME}_{sub_func_name}_error"
            )

    def charging_plan_warnings_add(self, charging_plan_warnings, plan, warning):

        if plan not in charging_plan_warnings:
            charging_plan_warnings[plan] = []
        charging_plan_warnings[plan].append(warning)

    def charge_plan_end(self, timestamp, kwh):
        hour_end = reset_time_to_hour(timestamp) + datetime.timedelta(hours=1)
        capacity = self.hour_kwh_capacity(timestamp)
        if capacity <= 0.0:
            return hour_end
        return min(timestamp + datetime.timedelta(hours=kwh / capacity), hour_end)

    def planned_driving(self, charging_plan, now):
        """The driving the dashboard chart shows, for the bars and the battery level alike: work
        and trip from departure to homecoming (a trip set as a battery level is shown as the
        distance of that level), and the typical daily distance spread over each day without
        work, also on top of a trip that day. Today only the part of the day that is left."""
        trip_range = self.get_trip_range()
        workplan_today = self.workplan_charging_enabled() and self.switch_state(f"workday_{charging_plan[0]['day_text']}") == "on"
        trips = [(plan_day["trip_goto"], plan_day["trip_homecoming"] or end_of_day(plan_day["trip_goto"]))
                 for day, plan_day in charging_plan.items() if isinstance(day, int) and plan_day["trip"]]
        driving = []
        for day in sorted(key for key in charging_plan if isinstance(key, int)):
            plan_day = charging_plan[day]
            start, end = max(plan_day["start_of_day"], now), plan_day["end_of_day"]
            if plan_day["workday"]:
                driving.append({"start": plan_day["work_goto"], "end": plan_day["work_homecoming"], "km": plan_day["work_range_needed"],
                                "battery_level": plan_day["work_battery_level_needed"], "away": plan_day["work_away"], "spread": False})
            elif not (day == 0 and workplan_today) and not any(goto <= plan_day["start_of_day"] and end <= homecoming for goto, homecoming in trips) and end > start:
                # whole days on a trip drive the trip; a workday of today that is over drove its work
                km = plan_day["offday_range_needed"] if plan_day["offday"] else self.get_entity_daily_distance()
                part = (end - start) / (end - plan_day["start_of_day"])
                driving.append({"start": start, "end": end, "km": km * part,
                                "battery_level": self.calc_distance_to_battery_level(km) * part, "away": False, "spread": True})
            if plan_day["trip"]:
                used = max(plan_day["trip_battery_level_needed"] + plan_day["trip_battery_level_above_max"] - self.get_min_trip_battery_level(), 0.0)
                driving.append({"start": plan_day["trip_goto"], "end": plan_day["trip_homecoming"] or end_of_day(plan_day["trip_goto"]),
                                "km": trip_range or self.calc_battery_level_to_distance(used), "battery_level": used, "away": False, "spread": False})
        return driving

    def battery_forecast_flow(self, charging_plan, chargeHours, now, driving):
        """The expected battery level for the dashboard chart: a point every hour (and where a
        change starts or ends) from the current level, the planned charging over each hour's
        charging time, the solar prediction over the daylight at home, and the driving: half
        on the way out and half on the way home (all on the way home when the car is already
        away), a day without departure time spread over the day."""
        days = [charging_plan[day] for day in sorted(key for key in charging_plan if isinstance(key, int))]
        horizon = max(plan_day["end_of_day"] for plan_day in days)
        changes = []  # (start, end, percentage) spread evenly from start to end

        def add(start, end, percentage):
            start = max(start, now)
            if percentage and end > start:
                changes.append((start, min(end, horizon), percentage))

        def drive(goto, homecoming, percentage, away=False):
            if percentage <= 0.0:
                return
            drive_time = min(FORECAST_DRIVE_TIME, max(homecoming - goto, datetime.timedelta(minutes=2)) / 2)
            if not away:
                # already gone: the way out is in the current battery level
                if goto > now:
                    add(goto, goto + drive_time, -percentage / 2)
                percentage /= 2
            add(homecoming - drive_time, homecoming, -percentage)

        for timestamp, value in chargeHours.items():
            if not isinstance(timestamp, datetime.datetime):
                continue
            end = self.charge_plan_end(timestamp, value["kWh"])
            if end <= now:
                # this hour's charging takes longer than planned: the rest of the hour
                end = reset_time_to_hour(timestamp) + datetime.timedelta(hours=1)
            add(timestamp, end, value["battery_level"])

        away = []
        for planned in driving:
            if planned["spread"]:
                add(planned["start"], planned["end"], -planned["battery_level"])
            else:
                drive(planned["start"], planned["end"], planned["battery_level"], planned["away"])
                away.append((planned["start"], planned["end"]))

        if self.solar_charging_enabled():
            for plan_day in days:
                solar = sum(plan_day["solar_prediction"])
                sun_events = self.get_sun_events(plan_day["start_of_day"])
                if solar <= 0.0 or not sun_events["sunrise"] or not sun_events["sunset"]:
                    continue
                home = [(max(sun_events["sunrise"], now), sun_events["sunset"])]
                for away_start, away_end in away:
                    home = [part for start, end in home for part in ((start, min(end, away_start)), (max(start, away_end), end))]
                home = [(start, end) for start, end in home if end > start]
                daylight = sum((end - start for start, end in home), datetime.timedelta())
                for start, end in home:
                    add(start, end, solar * ((end - start) / daylight))

        times = {now, horizon}
        hour = reset_time_to_hour(now) + datetime.timedelta(hours=1)
        while hour < horizon:
            times.add(hour)
            hour += datetime.timedelta(hours=1)
        times.update(time for start, end, _ in changes for time in (start, end))

        start_level = self.battery_level()
        flow = []
        for time in sorted(times):
            level = start_level + sum(percentage * min(max((time - start) / (end - start), 0.0), 1.0) for start, end, percentage in changes)
            flow.append({"time": time.isoformat(), "battery_level": round(min(max(level, 0.0), 100.0), 1)})
        return flow
