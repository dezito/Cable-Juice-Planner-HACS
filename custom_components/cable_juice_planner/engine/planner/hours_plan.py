"""The plan per hour."""
from __future__ import annotations

import datetime

from ...utils.formatting import emoji_parse, emoji_text_format
from ...utils.helpers import days_between, in_between, reset_time_to_hour


class HoursPlan:
    """The plan per hour."""

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
