"""The solar prediction of the plan."""
from __future__ import annotations

import logging

from ....utils.formatting import emoji_parse
from ....utils.helpers import date_to_string, day_of_week_text

_LOGGER = logging.getLogger(__name__)


class FutureSolar:
    """Solar prediction of the plan."""

    def solar_prediction_planner(self, ctx, day):

        try:
            if self.solar_charging_enabled() and sum(ctx.charging_plan[day]['solar_prediction']) > 0.0:
                date = date_to_string(date = ctx.charging_plan[day]['start_of_day'], format = "%d/%m")

                sun_events = self.get_sun_events(ctx.charging_plan[day]['start_of_day'])
                sunrise = sun_events["sunrise"]
                sunrise_text = f"{emoji_parse({'sunrise': True})}{date_to_string(date = sunrise, format = '%H:%M')}"
                sunset = sun_events["sunset"]
                sunset_text = f"{emoji_parse({'sunset': True})}{date_to_string(date = sunset, format = '%H:%M')}"

                timeperiod = []
                if ctx.charging_plan[day]['workday']:
                    if sunrise <= ctx.charging_plan[day]['work_goto']:
                        timeperiod.append(f"{sunrise_text}-{emoji_parse({'goto': True})}{date_to_string(date = ctx.charging_plan[day]['work_goto'], format = '%H:%M')}")
                        if sunset >= ctx.charging_plan[day]['work_homecoming']:
                            timeperiod.append("<br>")
                    if sunset >= ctx.charging_plan[day]['work_homecoming']:
                        timeperiod.append(f"{emoji_parse({'homecoming': True})}{date_to_string(date = ctx.charging_plan[day]['work_homecoming'], format = '%H:%M')}-{sunset_text}")
                elif ctx.charging_plan[day]['trip']:
                    if sunrise <= ctx.charging_plan[day]['trip_goto']:
                        timeperiod.append(f"{sunrise_text}-{emoji_parse({'trip': True})}{date_to_string(date = ctx.charging_plan[day]['trip_goto'], format = '%H:%M')}")
                        if sunset >= ctx.charging_plan[day]['trip_homecoming']:
                            timeperiod.append("<br>")
                    if sunset >= ctx.charging_plan[day]['trip_homecoming']:
                        timeperiod.append(f"{emoji_parse({'homecoming': True})}{date_to_string(date = ctx.charging_plan[day]['trip_homecoming'], format = '%H:%M')}-{sunset_text}")
                else:
                    timeperiod.append(f"{sunrise_text}-{sunset_text}")

                ctx.solar_over_production[day] = {
                    "day": self.t('ui.calendar.weekday_names.' + day_of_week_text(ctx.charging_plan[day]['start_of_day'])),
                    "date": date,
                    "when": " ".join(timeperiod),
                    "emoji": emoji_parse({'solar': True}),
                    "percentage": sum(ctx.charging_plan[day]['solar_prediction']),
                    "kWh": sum(ctx.charging_plan[day]['solar_kwh_prediction']),
                    "corrected": ctx.charging_plan[day]['solar_prediction_corrected'],
                }
        except Exception as e:
            _LOGGER.warning(f"Cant create solar over production prediction for day {day}: {e} {type(e)}")
