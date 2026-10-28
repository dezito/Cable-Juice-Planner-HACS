"""Combining sessions into the charging history and its totals."""
from __future__ import annotations

import datetime
import logging

from ...const import ENTITY_UNAVAILABLE_STATES
from ...utils.formatting import (
    emoji_parse,
    emoji_sorting,
    emoji_text_format,
    emoji_update_local_energy,
    join_unique_emojis,
)
from ...utils.helpers import (
    add_months,
    days_between,
    get_closest_key,
    hours_between,
    minutes_between,
    month_first_day,
)
from .efficiency import EFFICIENCY_FACTORS

_LOGGER = logging.getLogger(__name__)


class HistoryCombine:
    """Combining sessions into the charging history and its totals."""

    def charging_history_combine_and_set(self, get_ending_byte_size=False) -> None:  # noqa: C901
        t = self.t
        history: list[str] = []
        combined_db: dict = {}
        current_history_bytes_size = 0
        db = self.charging_history_db
        now = self.now()

        def calculate_estimated_km(kwh, month):
            try:
                efficiency_factor = EFFICIENCY_FACTORS.get(month, 1.0)
                km_per_kwh = float(self.own_state("km_per_kwh", float_type=True, error_state=0.0))
                try:
                    km_per_kwh = float(self.own_attr("km_per_kwh", "average", error_state=None).split(" ")[0])
                except (TypeError, AttributeError, ValueError):
                    pass
                return round(kwh * km_per_kwh * efficiency_factor, 1), True
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error calculating estimated range: %s %s", err, type(err))
                return 0.0, False

        def history_loop_append(lines):
            nonlocal current_history_bytes_size
            if isinstance(lines, list):
                history.extend(lines)
                current_history_bytes_size += len("\n".join(lines).encode("utf-8"))
            else:
                history.append(lines)
                current_history_bytes_size += len(lines.encode("utf-8"))

        if db:
            total = {
                "cost": {"total": 0.0},
                "public_cost": {"total": 0.0},
                "kwh": {"total": 0.0},
                "percentage": {"total": 0.0},
                "kWh_from_local_energy": {"total": 0.0},
                "kWh_from_public": {"total": 0.0},
                "kWh_from_grid": {"total": 0.0},
                "charging_kwh_day": {"total": 0.0},
                "charging_kwh_night": {"total": 0.0},
                "km": {"total": 0.0},
                "odometer": {},
                "odometer_first_charge_datetime": None,
            }
            details = False
            length = 10
            sub_length = 50
            sub_details_count = 0
            max_history_bytes_size = 16384 - (0 if self.charging_history_ending_byte_size is None else self.charging_history_ending_byte_size)
            history_to_big_anounced = False
            current_history_counter = 0
            skip_counter = 0
            solar_in_months = False
            sorted_db = sorted(db.items(), key=lambda item: item[0], reverse=True)
            sorted_db_length = len(sorted_db)
            header = f"| {t('ui.common.time')} |  | % | kWh | {t('ui.common.price')} |"
            align = "|:---:|:---|:---:|:---:|:---:|"
            add_header = False

            history_loop_append("<center>\n")
            history_loop_append(header)
            history_loop_append(align)

            for i, (when, data) in enumerate(sorted_db):
                if skip_counter == 0:
                    if not history_to_big_anounced:
                        if current_history_bytes_size >= max_history_bytes_size:
                            history_to_big_anounced = True
                            history_loop_append(f"##### {t('ui.charging_history_combine_and_set.too_big_warning')}\n")
                        elif current_history_counter == length and not details:
                            details = True
                            history_loop_append(["<details>", f"<summary><b>{t('ui.charging_history_combine_and_set.more_history')}</b></summary>\n"])
                            add_header = True
                        elif (
                            current_history_bytes_size < max_history_bytes_size - 1
                            and current_history_counter > length
                            and current_history_counter >= sub_length
                            and current_history_counter % sub_length == 0
                            and (len(db) - i) >= sub_length
                        ):
                            history_loop_append(["\n", "<details>", f"<summary><b>{t('ui.charging_history_combine_and_set.more_history')}</b></summary>\n"])
                            sub_details_count += 1
                            add_header = True

                    started = when
                    charging_session = data["charging_session"] if "charging_session" in data else None
                    ended = data["ended"]
                    emoji = data["emoji"]
                    percentage = data.get("percentage", 0.0)
                    kWh = data.get("kWh", 0.0)  # noqa: N806
                    kWh_from_public = data.get("kWh_from_public", 0.0)  # noqa: N806
                    kWh_from_grid = data.get("kWh_from_grid", 0.0)  # noqa: N806
                    kWh_from_local_energy = data.get("kWh_from_local_energy", 0.0)  # noqa: N806
                    solar_kwh_of_local_energy = data.get("solar_kwh_of_local_energy", 0.0)
                    powerwall_kwh_of_local_energy = data.get("powerwall_kwh_of_local_energy", 0.0)
                    cost = data.get("cost", 0.0)
                    public_cost = data.get("public_cost", 0.0)
                    unit = data["unit"]
                    start_charger_meter = None
                    end_charger_meter = None
                    odometer = None
                    charging_type: list = []

                    if "start_charger_meter" in data and "end_charger_meter" in data:
                        start_charger_meter = data["start_charger_meter"]
                        end_charger_meter = data["end_charger_meter"]
                    if "odometer" in data:
                        odometer = data["odometer"]
                        if not total["odometer_first_charge_datetime"]:
                            total["odometer_first_charge_datetime"] = when
                        else:
                            total["odometer_first_charge_datetime"] = min(total["odometer_first_charge_datetime"], when)
                    if "charging_type" in data:
                        if isinstance(data["charging_type"], str):
                            data["charging_type"] = [data["charging_type"]]
                        charging_type.extend(data["charging_type"])

                    from_to = "-"
                    if i > 5 and ended != ">":
                        charging_session = None
                        for x in range(i + 1, sorted_db_length + 1):
                            if x + 1 > sorted_db_length:
                                break
                            next_when, next_data = sorted_db[x]
                            if days_between(when, now) > 7:
                                if days_between(when, next_when) != 0:
                                    break
                                emoji = join_unique_emojis(emoji, next_data["emoji"])
                            elif not (days_between(when, next_when) == 0 and hours_between(when, next_when) <= 1 and emoji == next_data["emoji"]):
                                break
                            started = next_when
                            percentage += next_data["percentage"]
                            kWh += next_data["kWh"]  # noqa: N806
                            kWh_from_public += next_data.get("kWh_from_public", 0.0)  # noqa: N806
                            kWh_from_grid += next_data.get("kWh_from_grid", 0.0)  # noqa: N806
                            kWh_from_local_energy += next_data.get("kWh_from_local_energy", 0.0)  # noqa: N806
                            solar_kwh_of_local_energy += next_data.get("solar_kwh_of_local_energy", 0.0)
                            powerwall_kwh_of_local_energy += next_data.get("powerwall_kwh_of_local_energy", 0.0)
                            cost += next_data["cost"]
                            public_cost += next_data.get("public_cost", 0.0)
                            if kWh > 0.0:
                                unit = cost / kWh
                            if "start_charger_meter" in next_data:
                                start_charger_meter = next_data["start_charger_meter"]
                            if "odometer" in next_data and odometer:
                                odometer = min(odometer, next_data["odometer"])
                            if "charging_type" in next_data and next_data["charging_type"] not in charging_type:
                                if isinstance(next_data["charging_type"], str):
                                    next_data["charging_type"] = [next_data["charging_type"]]
                                charging_type.extend(next_data["charging_type"])
                                charging_type = list(set(charging_type))
                            skip_counter += 1

                    emoji = emoji_sorting(emoji_update_local_energy(emojis=emoji, kwh_from_local_energy=kWh_from_local_energy, solar_kwh_of_local_energy=solar_kwh_of_local_energy, powerwall_kwh_of_local_energy=powerwall_kwh_of_local_energy))
                    combined_db[started] = {
                        "cost": round(cost, 3),
                        "public_cost": round(public_cost, 3),
                        "emoji": emoji,
                        "ended": ended,
                        "kWh": round(kWh, 3),
                        "kWh_from_public": round(kWh_from_public, 3),
                        "kWh_from_grid": round(kWh_from_grid, 3),
                        "kWh_from_local_energy": round(kWh_from_local_energy, 3),
                        "solar_kwh_of_local_energy": round(solar_kwh_of_local_energy, 3),
                        "powerwall_kwh_of_local_energy": round(powerwall_kwh_of_local_energy, 3),
                        "percentage": round(percentage, 1),
                        "unit": round(unit, 3),
                    }
                    if start_charger_meter is not None and end_charger_meter is not None:
                        combined_db[started]["start_charger_meter"] = start_charger_meter
                        combined_db[started]["end_charger_meter"] = end_charger_meter
                    if odometer is not None:
                        combined_db[started]["odometer"] = odometer
                    if charging_type:
                        combined_db[started]["charging_type"] = charging_type
                    if charging_session:
                        combined_db[started]["charging_session"] = charging_session

                    if current_history_bytes_size < max_history_bytes_size:
                        if add_header:
                            add_header = False
                            history_loop_append([header, align])
                        ended_hour = int(ended.split(":")[0]) if ended != ">" and len(ended) > 0 else now.hour
                        if started.hour != ended_hour:
                            from_to = "»"
                        started_text = f"**{started.strftime('%d/%m %H:%M')}**" if started else ""
                        ended_text = f"**{ended}**" if ended else ">"
                        emoji_text = f"**{emoji_text_format(emoji)}**" if emoji else ""
                        if isinstance(percentage, (float, int)):
                            shown = get_closest_key(percentage, {1 / 4: "¼", 1 / 2: "½", 3 / 4: "¾"}) if 0.0 < percentage < 1.0 else int(percentage)
                            percentage_text = f"**{shown}**"
                        else:
                            percentage_text = "**0**"
                        kwh_text = f"**{kWh:.1f}**" if isinstance(kWh, (float, int)) else "**0.0**"
                        cost_text = f"**{cost:.2f}**" if isinstance(cost, (float, int)) else ""
                        unit_text = f"**{unit:.2f}**" if isinstance(unit, (float, int)) else ""
                        history_loop_append(f"| {started_text}{from_to}{ended_text} | {emoji_text} | {percentage_text} | {kwh_text} | {cost_text} ({unit_text}) |")
                        current_history_counter += 1
                else:
                    skip_counter = max(skip_counter - 1, 0)

                if current_history_counter > length and (len(db) - i) == 1:
                    history_loop_append(["</details>", "\n"] * (sub_details_count + 1))

                month = month_first_day(when)
                if month not in total["cost"]:
                    for key in ("cost", "public_cost", "kwh", "percentage", "kWh_from_public", "kWh_from_local_energy", "charging_kwh_day", "charging_kwh_night", "km"):
                        total[key][month] = 0.0
                total["cost"][month] += data.get("cost", 0.0)
                total["kwh"][month] += data.get("kWh", 0.0)
                total["percentage"][month] += data.get("percentage", 0.0)
                day_or_night = "charging_kwh_day" if self.is_day(when) else "charging_kwh_night"
                total[day_or_night][month] += data.get("kWh", 0.0)
                total[day_or_night]["total"] += data.get("kWh", 0.0)
                if "kWh_from_local_energy" in data and data["kWh_from_local_energy"] > 0.0:
                    total["kWh_from_local_energy"][month] += data.get("kWh_from_local_energy", 0.0)
                    total["kWh_from_local_energy"]["total"] += data.get("kWh_from_local_energy", 0.0)
                    solar_in_months = True
                if "kWh_from_public" in data and data["kWh_from_public"] > 0.0:
                    total["kWh_from_public"][month] += data.get("kWh_from_public", 0.0)
                    total["kWh_from_public"]["total"] += data.get("kWh_from_public", 0.0)
                    total["public_cost"][month] += data.get("public_cost", 0.0)
                    total["public_cost"]["total"] += data.get("public_cost", 0.0)
                total["cost"]["total"] += data.get("cost", 0.0)
                total["kwh"]["total"] += data.get("kWh", 0.0)
                total["percentage"]["total"] += data.get("percentage", 0.0)
                if "odometer" in data:
                    if month not in total["odometer"]:
                        total["odometer"][month] = data["odometer"]
                    else:
                        total["odometer"][month] = min(total["odometer"][month], data["odometer"])

            if details:
                history.extend(["</details>", "\n"])

            odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
            for month in total["odometer"]:
                if add_months(month, 1) in total["odometer"]:
                    total["km"][month] = round(total["odometer"][add_months(month, 1)] - total["odometer"][month], 1)
                else:
                    total["km"][month] = round(self.get_state(odometer_entity, float_type=True, error_state=total["odometer"][month]) - total["odometer"][month], 1)
                total["km"]["total"] += total["km"][month]

            estimated_values_used = False
            if odometer_entity:
                for month in total["km"]:
                    if month == "total":
                        continue
                    estimated_km = 0.0
                    if total["km"][month] == 0.0:
                        estimated_km, estimated_values_used = calculate_estimated_km(total["kwh"].get(month, 0.0), month.month)
                        total["km"][month] = estimated_km
                        total["km"]["total"] += estimated_km
                    elif month == month_first_day(total["odometer_first_charge_datetime"]):
                        kwh = sum(value["kWh"] for key, value in db.items() if month_first_day(key) == month and "odometer" not in value)
                        estimated_km, estimated_values_used = calculate_estimated_km(kwh, month.month)
                        total["km"][month] = total["km"][month] + estimated_km
                        total["km"]["total"] += estimated_km
                    km_kwh = 0.0
                    wh_km = 0.0
                    if total["km"][month] > 0.0 and total["kwh"][month] > 0.0:
                        km_kwh = round(total["km"][month] / total["kwh"][month], 2)
                        wh_km = round(1000 / km_kwh, 2) if km_kwh > 0.0 else 0.0
                    efficiency_label = f"{km_kwh:.1f}<br>({wh_km:.1f})" if km_kwh > 0.0 else ""
                    total["km"][month] = [total["km"][month], f"{'~' if estimated_km > 0.0 else ''}{int(round(total['km'][month], 0))}", efficiency_label]
            else:
                for month in total["km"]:
                    if month != "total":
                        total["km"][month] = [total["km"][month], "", ""]

            if total["kwh"]["total"] > 0.0:
                self._history_totals(history, total, solar_in_months, estimated_values_used)
            history.append("</center>")

        total_history_bytes_size = len("\n".join(history).encode("utf-8"))
        self.charging_history_ending_byte_size = total_history_bytes_size - current_history_bytes_size + 1000

        if not get_ending_byte_size:
            self.set_attr("charging_history", "history", "\n".join(history) if history else f"**{self.t('ui.charging_history_combine_and_set.no_history')}**")
            self.charging_history_db = combined_db
            self.save_charging_history()

    def _history_totals(self, history: list, total: dict, solar_in_months: bool, estimated_values_used: bool) -> None:
        """The monthly totals table and the day/night allocation below the history."""
        t = self.t
        valuta = t("ui.common.valuta")
        solar_string = ""
        if total["kWh_from_local_energy"]["total"] > 0.0:
            total_local_energy_percentage = round(total["kWh_from_local_energy"]["total"] / total["kwh"]["total"] * 100.0, 1)
            solar_string = f" ({emoji_parse({'solar': True})}{total['kWh_from_local_energy']['total']:.1f}/{total_local_energy_percentage}%)"

        history.append("---")
        history.append("<details>")
        history.append(
            f"\n<summary><b>{t('ui.common.total')} {round(total['kwh']['total'], 1)}kWh {solar_string} {round(total['cost']['total'], 2):.2f}{valuta} ({round(total['cost']['total'] / total['kwh']['total'], 2):.2f})</b></summary>\n"
        )
        solar_header = f"<br>({emoji_parse({'solar': True})})" if solar_in_months else ""
        km_header = t("ui.common.distance_type").capitalize() if total["km"]["total"] > 0.0 else ""
        km_kwh_header = f"{t('ui.common.distance_kwh')}<br>({t('ui.common.wh_distance')})" if total["km"]["total"] > 0.0 else ""
        history.extend([
            f"| {t('ui.charging_history_combine_and_set.month')} | {km_header} | {km_kwh_header} | kWh{solar_header} | {t('ui.common.price')}<br>{t('ui.common.valuta_kwh')}<br>({t('ui.common.valuta_distance')}) |",
            "|:---:|:---:|:---:|:---:|:---:|",
        ])

        datetime_keys = [key for key in total["cost"] if isinstance(key, datetime.datetime)]
        for i, month in enumerate(sorted(datetime_keys)):
            local_energy = ""
            solar_percentage = ""
            if total["kWh_from_local_energy"][month] > 0.0 and total["kwh"][month] > 0.0:
                percentage = round(total["kWh_from_local_energy"][month] / total["kwh"][month] * 100.0, 1)
                local_energy = f"<br>({round(total['kWh_from_local_energy'][month], 1)})"
                solar_percentage = f"<br>({round(percentage, 1)}%)"
            unit_price = round(total["cost"][month] / total["kwh"][month], 2) if total["kwh"][month] > 0.0 else 0.0
            if total["km"][month][0] > 0.0:
                unit_string = f"<br>{unit_price:.2f}<br>({round(total['cost'][month] / total['km'][month][0], 2):.2f})"
            else:
                unit_string = f"<br>{unit_price:.2f}"
            start, end = ("<font color=grey>", "</font>") if i % 2 == 0 else ("", "")
            month_name = t(f"ui.calendar.month_names.{month.strftime('%b').lower()}")
            history.append(
                f"| {start}{month_name}<br>{month.strftime('%Y')}{end} | {start}{total['km'][month][1]}{end} | {start}{total['km'][month][2]}{end}"
                f" | {start}{round(total['kwh'][month], 1)}{local_energy}{solar_percentage}{end} | {start}{round(total['cost'][month], 2):.2f}{unit_string}{end} |"
            )

        total_local_energy = ""
        if total["kWh_from_local_energy"]["total"] > 0.0 and total["kwh"]["total"] > 0.0:
            percentage = round(total["kWh_from_local_energy"]["total"] / total["kwh"]["total"] * 100.0, 1)
            total_local_energy = f"<br>**({round(total['kWh_from_local_energy']['total'], 1)})**<br>**({round(percentage, 1)}%)**"
        km_kwh = 0.0
        wh_km = 0.0
        if total["km"]["total"] > 0.0 and total["kwh"]["total"] > 0.0:
            km_kwh = round(total["km"]["total"] / total["kwh"]["total"], 2)
            wh_km = round(1000 / km_kwh, 2)
        efficiency_label = f"**{km_kwh:.1f}<br>({wh_km:.1f})**" if km_kwh > 0.0 else ""
        total_km = f"**{round(total['km']['total'], 1)}**" if total["km"]["total"] > 0.0 else ""
        unit_price = round(total["cost"]["total"] / total["kwh"]["total"], 2) if total["kwh"]["total"] > 0.0 else 0.0
        if total["km"]["total"] > 0.0:
            unit_string = f"{unit_price:.2f}<br>({round(total['cost']['total'] / total['km']['total'], 2):.2f})"
        else:
            unit_string = f"{unit_price:.2f}"
        history.append(
            f"| **{t('ui.common.total')}** | {total_km} | {efficiency_label} | **{round(total['kwh']['total'], 1)}**{total_local_energy} | **{round(total['cost']['total'], 2):.2f}<br>{unit_string}** |"
        )
        if estimated_values_used:
            history.append(f"\n##### ~ = {t('ui.charging_history_combine_and_set.estimated_distance')}\n")

        history.append("---")
        history.append(f"\n**{t('ui.charging_history_combine_and_set.charging_allocation')}**")
        history.append(f"| {t('ui.charging_history_combine_and_set.month')} | {t('ui.charging_history_combine_and_set.day_kwh')} | {t('ui.charging_history_combine_and_set.night_kwh')} |")
        history.append("|:---|:---:|:---:|")
        for month in sorted(key for key in total["charging_kwh_day"] if isinstance(key, datetime.datetime)):
            procent_day = round(total["charging_kwh_day"][month] / total["kwh"][month] * 100.0, 1) if total["kwh"][month] > 0.0 else 0.0
            procent_night = round(total["charging_kwh_night"][month] / total["kwh"][month] * 100.0, 1) if total["kwh"][month] > 0.0 else 0.0
            month_name = t(f"ui.calendar.month_names.{month.strftime('%b').lower()}")
            history.append(
                f"| {month_name} {month.strftime('%Y')} | {round(total['charging_kwh_day'][month], 1)} ({procent_day}%) | {round(total['charging_kwh_night'][month], 1)} ({procent_night}%) |"
            )
        procent_day = round(total["charging_kwh_day"]["total"] / total["kwh"]["total"] * 100.0, 1) if total["kwh"]["total"] > 0.0 else 0.0
        procent_night = round(total["charging_kwh_night"]["total"] / total["kwh"]["total"] * 100.0, 1) if total["kwh"]["total"] > 0.0 else 0.0
        history.append(
            f"| **{t('ui.common.total')}** | **{round(total['charging_kwh_day']['total'], 1)} ({procent_day}%)** | **{round(total['charging_kwh_night']['total'], 1)} ({procent_night}%)** |"
        )
        if total["kWh_from_public"]["total"] > 0.0 and total["kwh"]["total"] > 0.0:
            total_public_cost = f"{round(total['public_cost']['total'], 2)}{valuta}"
            percentage = round(total["kWh_from_public"]["total"] / total["kwh"]["total"] * 100.0, 1)
            total_public_charging = f"{total_public_cost} {round(total['kWh_from_public']['total'], 1)}kWh ({round(percentage, 1)}%)"
            history.append("---")
            history.append(f"\n**{emoji_parse({'public_charging': True})} {t('ui.charging_history_combine_and_set.public_charing_total')} {total_public_charging}**")
        history.append("\n</details>\n")

    def charging_power_to_emulated_battery_level(self) -> None:
        """Without a car integration the virtual battery level follows the charger meter."""
        if self.is_ev_configured() or not self.current_charging_session.get("start"):
            return
        now = self.now()
        past = now - datetime.timedelta(minutes=self.config["cron_interval"])
        charger = self.config["charger"]["entity_ids"]
        watt = self.history.get_average_value(charger["power_consumtion_entity_id"], past, now, convert_to="W", error_state=0.0)
        if watt == 0.0:
            return
        current_charger_meter = float(self.get_state(charger["kwh_meter_entity_id"], float_type=True))
        added_kwh = round(current_charger_meter - self.current_charging_session.get("start_charger_meter", 0.0), 1)
        if "last_charger_meter" in self.current_charging_session:
            added_kwh = round(current_charger_meter - self.current_charging_session["last_charger_meter"], 1)
        self.current_charging_session["last_charger_meter"] = current_charger_meter
        added_percentage = round(self.kwh_to_percentage(added_kwh, include_charging_loss=True))
        current_battery_level = self.battery_level()
        charging_ceiling = max(current_battery_level, self.get_completed_battery_level() - 1.0)
        new_battery_level = min(round(current_battery_level + added_percentage, 0), charging_ceiling)
        _LOGGER.debug("Adding %s%% to virtual battery level: %s%% -> %s%%", added_percentage, current_battery_level, new_battery_level)
        self.set_setting("battery_level", new_battery_level)

    def charging_history(self, charging_data=None, charging_type="") -> dict | None:
        """Start, update or end the current charging session (the old queue worker)."""
        try:
            result = self._charging_history(charging_data=charging_data, charging_type=charging_type)
            self.charging_history_last_result = result
            return result
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Exception in charging_history_worker: %s %s", err, type(err))
            self.persistent_notification(f"Charging history queue failed: {err} {type(err)}", f"{self.title} Failed", f"{self.name}_charging_history_worker_failed")
            return None

    def _charging_history(self, charging_data=None, charging_type="") -> dict | None:  # noqa: C901
        db = self.charging_history_db
        session = self.current_charging_session
        charger = self.config["charger"]["entity_ids"]
        added_kwh = None
        start = None

        def get_current_statistic(charging_ended=False):
            session = self.current_charging_session
            when = session.get("start", None)
            charger_meter = self.get_state(charger["kwh_meter_entity_id"], float_type=True, error_state=None)
            if when is None:
                raise ValueError("No current charging session start time, cannot update charging history")
            if charger_meter in ENTITY_UNAVAILABLE_STATES:
                raise ValueError(f"Charger meter entity {charger['kwh_meter_entity_id']} is not available, cannot update charging history")
            kwh = round(charger_meter - session["start_charger_meter"], 1)
            added_percentage = round(self.kwh_to_percentage(kwh, include_charging_loss=True), 1)
            minutes = minutes_between(session["start"], self.now(), error_value=self.now().minute)
            kwh_from_local_energy = solar_kwh_of_local_energy = powerwall_kwh_of_local_energy = 0.0
            price = 0.0
            try:
                kwh_from_local_energy, solar_kwh_of_local_energy, powerwall_kwh_of_local_energy = self.calc_local_energy_kwh(minutes, kwh, solar_period_current_hour=False)
                price = round(self.calc_kwh_price(minutes, solar_period_current_hour=True), 3)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error calculating local energy kWh or price: %s %s", err, type(err))
                self.persistent_notification(
                    f"Charging history failed to calculate local energy kWh or price: {err} {type(err)}",
                    f"{self.title} failed",
                    f"{self.name}__charging_history_calc_local_energy_kwh_or_price_failed",
                )
            cost = round(kwh * price, 2)
            entry = db[when]
            entry["percentage"] = round(added_percentage, 1)
            entry["kWh"] = round(kwh, 3)
            entry["kWh_from_grid"] = round(max(kwh - kwh_from_local_energy, 0.0), 3)
            entry["kWh_from_local_energy"] = round(kwh_from_local_energy, 3)
            entry["solar_kwh_of_local_energy"] = solar_kwh_of_local_energy
            entry["powerwall_kwh_of_local_energy"] = powerwall_kwh_of_local_energy
            entry["cost"] = round(cost, 3)
            entry["unit"] = round(price, 3)
            entry["start_charger_meter"] = session["start_charger_meter"]
            entry["end_charger_meter"] = charger_meter
            entry["charging_session"] = session
            if charging_ended:
                ended = self.now().strftime("%H:%M")
                _LOGGER.info(
                    "Charging session ended %s: %s%% %skWh %s (%s/kWh) %s %s",
                    ended, added_percentage, kwh, cost, price, session["charging_type"], session["emoji"],
                )
                entry["ended"] = ended
                if entry["charging_session"]["charging_type"] == "local_energy":
                    if entry["kWh_from_local_energy"] > 0.0:
                        solar_ratio = entry["kWh_from_local_energy"] / entry["kWh"]
                        if solar_ratio < 0.5:
                            entry["emoji"] = join_unique_emojis(entry["emoji"], emoji_parse({"grid_charging": True}))
            return [kwh, when]

        try:
            update_entity = False
            if len(db) == 0:
                self.load_charging_history(db)
                db = self.charging_history_db

            if session["charging_type"] is None and session["start"] is None:
                for i, when in enumerate(sorted(db.keys(), reverse=True)):
                    if i > 10:
                        break
                    if db[when]["ended"] == ">" or db[when]["kWh"] == 0.0:
                        _LOGGER.warning("Removing unfinished string: %s", db[when])
                        del db[when]
                        update_entity = True

            if session["start"] is not None and charging_type != session["charging_type"]:
                added_kwh, when = get_current_statistic(True)
                self.charging_power_to_emulated_battery_level()
                self.calc_co2_emitted(minutes_between(session["start"], self.now(), error_value=self.now().minute), added_kwh=added_kwh)
                if added_kwh <= 0.1 and f"{emoji_parse({'charging_error': True})}" not in session["emoji"]:
                    _LOGGER.warning("Removing unfinished string added_kwh:%s <= min_kwh:0.1: %s", added_kwh, db[when])
                    del db[when]
                update_entity = True
                self.reset_current_charging_session()
                session = self.current_charging_session

            if session["start"] is None and charging_data:
                start = self.now()
                start_charger_meter = self.get_state(charger["kwh_meter_entity_id"], float_type=True, error_state=None)
                if start_charger_meter in ENTITY_UNAVAILABLE_STATES:
                    self.reset_current_charging_session()
                    raise ValueError(f"Charger meter entity {charger['kwh_meter_entity_id']} is not available, cannot start charging session")
                start_charger_meter = float(start_charger_meter)
                session["charging_type"] = charging_type
                session["start"] = start
                session["start_charger_meter"] = start_charger_meter

                if not charger["other_ev_using_this_charger_entity_ids"]:
                    if db and len(db) > 1:
                        last_data = sorted(db.items(), key=lambda item: item[0], reverse=True)[0][1]
                        if "end_charger_meter" in last_data and last_data["end_charger_meter"] is None:
                            _LOGGER.warning("Missing end charger meter from last session, using charger meter value at start of current session")
                        elif "end_charger_meter" in last_data and last_data["end_charger_meter"] is not None:
                            if start_charger_meter < last_data["end_charger_meter"]:
                                _LOGGER.warning("Start charger meter %s is less than end charger meter from last session %s", start_charger_meter, last_data["end_charger_meter"])
                            else:
                                session["start_charger_meter"] = last_data["end_charger_meter"]
                                _LOGGER.info("Using end charger meter from last session %s", session["start_charger_meter"])
                        else:
                            _LOGGER.warning("Missing end charger meter from last session, cannot use it for start of current session")

                session["emoji"] = f"{emoji_parse(charging_data)}"
                session["data"] = charging_data
                db[start] = {
                    "ended": ">",
                    "emoji": session["emoji"],
                    "percentage": round(charging_data.get("battery_level", 0.0), 1),
                    "kWh": round(charging_data.get("kWh", 0.0), 3),
                    "cost": round(charging_data.get("Cost", 0.0), 3),
                    "unit": round(charging_data.get("Price", 0.0), 3),
                    "charging_session": session,
                    "start_charger_meter": start_charger_meter,
                    "end_charger_meter": start_charger_meter,
                    "charging_type": charging_type,
                }
                odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
                if odometer_entity:
                    odometer = self.get_state(odometer_entity, float_type=True, error_state=None)
                    if odometer:
                        db[start]["odometer"] = int(odometer)
                _LOGGER.info("Charging session started: %s %s", charging_type, session["emoji"])
                self.charging_power_to_emulated_battery_level()
                update_entity = True
            else:
                solar = f"{emoji_parse({'solar': True})}"
                updating = (solar, f"{emoji_parse({'manual': True, 'solar': True})}", f"{emoji_parse({'manual': True})}")
                for i, when in enumerate(sorted(db.keys(), reverse=True)):
                    if i > 3:
                        break
                    if db[when]["ended"] == ">" and db[when]["emoji"] in updating:
                        added_kwh, when = get_current_statistic()
                        self.charging_power_to_emulated_battery_level()
                        update_entity = True
                        _LOGGER.debug("Updating current charging session %s", when)
                        break

            if self.now().minute == 59 and self.charging_history_recalc_price():
                self.charging_power_to_emulated_battery_level()
                update_entity = True

            if update_entity:
                self.charging_history_combine_and_set()

            return {
                "added_kwh": added_kwh if added_kwh is not None else 0.0,
                "charging_type": charging_type,
                "charging_data": charging_data,
                "session_started": start if start is not None else False,
                "session_ended": charging_type != self.current_charging_session["charging_type"],
                "session_removed": added_kwh <= 0.1 if added_kwh is not None else False,
            }
        except Exception as err:  # noqa: BLE001
            message = f"Error in _charging_history CURRENT_CHARGING_SESSION: {self.current_charging_session} charging_data: {charging_data}, charging_type: {charging_type}, error: {err} {type(err)}"
            _LOGGER.error(message)
            self.persistent_notification(
                f"Charging history failed with\ncharging_data: {charging_data},\ncharging_type: {charging_type},\nerror: {err} {type(err)}",
                f"{self.title} error",
                f"{self.name}__charging_history_failed",
            )
            return None
