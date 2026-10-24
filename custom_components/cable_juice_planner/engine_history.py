"""Charging history, battery expenses, drive efficiency and public charging (engine mixin).

Port of the corresponding ev.py functions. The PyScript version handled the charging
history in a queue worker task; here the calls run inline in the worker thread, which
serialises them the same way.
"""
from __future__ import annotations

import datetime
import logging
from pprint import pformat

from .const import (
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    ENTITY_UNAVAILABLE_STATES,
    EV_PLUGGED_STATES,
    EV_UNPLUGGED_STATES,
)
from .formatting import (
    emoji_parse,
    emoji_sorting,
    emoji_text_format,
    emoji_update_local_energy,
    get_list_values,
    join_unique_emojis,
)
from .helpers import (
    add_months,
    average,
    calculate_ema,
    calculate_trend,
    days_between,
    get_closest_key,
    hours_between,
    minutes_between,
    month_first_day,
    months_between,
    reverse_list,
    to_datetime,
)

_LOGGER = logging.getLogger(__name__)

EFFICIENCY_ADJUSTMENT = 1.15
EFFICIENCY_FACTORS = {
    month: factor * EFFICIENCY_ADJUSTMENT
    for month, factor in {1: 0.85, 2: 0.87, 3: 0.90, 4: 0.95, 5: 1.00, 6: 1.02, 7: 1.03, 8: 1.02, 9: 1.00, 10: 0.95, 11: 0.90, 12: 0.87}.items()
}
HEATING_STATES = ("preheat_start", "preheat_cancel")
FULL_CHARGE_BATTERY_LEVEL = 99.0  # sets last_full_charge (full_charge_recommended)


class HistoryMixin:
    """Charging sessions, the charging history markdown and drive efficiency."""

    # ------------------------------------------------------------------ #
    # Charging history database
    # ------------------------------------------------------------------ #

    def reset_current_charging_session(self) -> None:
        self.current_charging_session = {"charging_type": None, "start": None, "start_charger_meter": 0.0, "emoji": "", "data": {}}

    def load_charging_history(self, database, odometer_history: dict | None = None) -> None:
        def rename_key_recursive(data, old_key, new_key):
            renamed = False
            if isinstance(data, dict):
                if old_key in data:
                    data[new_key] = data.pop(old_key)
                    renamed = True
                for key, value in data.items():
                    data[key], changed = rename_key_recursive(value, old_key, new_key)
                    renamed = renamed or changed
            elif isinstance(data, list):
                for i, item in enumerate(data):
                    data[i], changed = rename_key_recursive(item, old_key, new_key)
                    renamed = renamed or changed
            return data, renamed

        self.charging_history_db = database if isinstance(database, dict) and database else {}
        if not self.charging_history_db:
            self.save_charging_history()

        self.charging_history_db, renamed = rename_key_recursive(self.charging_history_db, "kWh_solar", "kWh_from_local_energy")
        if renamed:
            _LOGGER.info("Renamed key 'kWh_solar' to 'kWh_from_local_energy' in the charging history")
            self.save_charging_history()

        now = self.now()
        if self.charging_history_db:
            last_key, last_value = sorted(self.charging_history_db.items(), key=lambda item: item[0], reverse=True)[0]
            if hours_between(now, last_key) == 0 and now.hour == last_key.hour:
                try:
                    if "charging_session" in last_value:
                        self.current_charging_session = last_value["charging_session"]
                        self.charging_history_db[last_key]["ended"] = ">"
                        _LOGGER.info("Adding last charging session to CURRENT_CHARGING_SESSION:\n %s", pformat(self.current_charging_session, width=200, compact=True))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Cant add last charging session to CURRENT_CHARGING_SESSION: %s (%s)", err, last_value)

        odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
        if self.is_entity_available(odometer_entity) and odometer_history is not None:
            added_odometer = False
            for key, value in self.charging_history_db.items():
                if "odometer" in value:
                    continue
                values = self._window_values(odometer_history.get(key), odometer_entity, key, key + datetime.timedelta(minutes=1))
                if values:
                    added_odometer = True
                    _LOGGER.info("Adding odometer to history key %s: %s", key, int(min(values)))
                    self.charging_history_db[key]["odometer"] = int(min(values))
            if added_odometer:
                self.save_charging_history()

        self.set_state("charging_history", self.t("ui.load_charging_history.default_state", name=self.name))
        self.charging_history_combine_and_set(get_ending_byte_size=self.charging_history_ending_byte_size is None)
        self.charging_history_combine_and_set()

    def _window_values(self, rows, entity_id, start, end):
        """get_values(float_type=True) for a window loaded separately from the recorder."""
        if not rows:
            return None
        from .history import History  # noqa: PLC0415

        return History({entity_id: rows}, {}, self._snap.units, self._snap.tz, self.now()).get_values(entity_id, start, end, float_type=True)

    def history_keys_without_odometer(self) -> list[datetime.datetime]:
        return [key for key, value in self.charging_history_db.items() if "odometer" not in value]

    def save_charging_history(self) -> None:
        if not self.charging_history_db:
            return
        now = self.now()
        keep = self.config["database"]["charging_history_db_data_to_save"]
        try:
            for key in sorted(self.charging_history_db, reverse=True):
                if months_between(key, now, error_value=0) > keep + 1:
                    _LOGGER.warning("Removing %s from CHARGING_HISTORY_DB", key)
                    del self.charging_history_db[key]
            self.charging_history_db = dict(sorted(self.charging_history_db.items(), key=lambda item: item[0], reverse=True))
            self.mark_dirty("charging_history")
        except Exception as err:  # noqa: BLE001
            message = f"Cant save the charging history: {err} {type(err)}"
            _LOGGER.error(message)
            self.persistent_notification(message, f"{self.title} error", f"{self.name}_save_charging_history")

    def charging_history_recalc_price(self) -> bool:
        db = self.charging_history_db
        if not db:
            return False
        now = self.now()
        sorted_keys = sorted(db.keys(), reverse=True)
        start = sorted_keys[0]
        # a reference like ev.py: with one session in the hour the comparison below is False
        last_session = db[start]
        remove_keys = [start]
        if not (hours_between(now, start) == 0 and now.hour == start.hour):
            return False
        try:
            start_charger_meter = db[start]["start_charger_meter"]
            ended = db[start]["ended"]
            emoji = db[start]["emoji"]
            if ended == ">":
                return False
            if "charging_type" in db[start] and "public_charging" in db[start]["charging_type"]:
                return False
            if len(sorted_keys) > 1:
                for i in range(1, min(len(sorted_keys), 20)):
                    if hours_between(now, sorted_keys[i]) == 0 and now.hour == sorted_keys[i].hour:
                        start = sorted_keys[i]
                        _LOGGER.info("Found another charging session in current hour %s: %s", start, db[start])
                        start_charger_meter = db[start]["start_charger_meter"]
                        emoji = join_unique_emojis(emoji, db[sorted_keys[i]]["emoji"])
                        remove_keys.append(start)
                    else:
                        break
            for key in remove_keys:
                if key != start:
                    _LOGGER.info("Removing key %s from CHARGING_HISTORY_DB", key)
                    del db[key]

            end_charger_meter = float(self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True))
            added_kwh = end_charger_meter - start_charger_meter
            added_percentage = self.kwh_to_percentage(added_kwh, include_charging_loss=True)

            kwh_from_local_energy = solar_kwh_of_local_energy = powerwall_kwh_of_local_energy = 0.0
            price = 0.0
            try:
                kwh_from_local_energy, solar_kwh_of_local_energy, powerwall_kwh_of_local_energy = self.calc_local_energy_kwh(now.minute, added_kwh, solar_period_current_hour=True)
                price = self.calc_kwh_price(now.minute, solar_period_current_hour=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error calculating local energy kWh or price: %s %s", err, type(err))
                self.persistent_notification(f"Error calculating local energy kWh or price: {err} {type(err)}", f"{self.title} error", f"{self.name}_charging_history_recalc_price")

            cost = added_kwh * price
            emoji = emoji_sorting(emoji_update_local_energy(emojis=emoji, kwh_from_local_energy=kwh_from_local_energy, solar_kwh_of_local_energy=solar_kwh_of_local_energy, powerwall_kwh_of_local_energy=powerwall_kwh_of_local_energy))
            db[start]["kWh"] = round(added_kwh, 3)
            db[start]["kWh_from_grid"] = max(round(added_kwh - kwh_from_local_energy, 3), 0.0)
            db[start]["kWh_from_local_energy"] = round(kwh_from_local_energy, 3)
            db[start]["solar_kwh_of_local_energy"] = solar_kwh_of_local_energy
            db[start]["powerwall_kwh_of_local_energy"] = powerwall_kwh_of_local_energy
            db[start]["percentage"] = round(added_percentage, 1)
            db[start]["unit"] = round(price, 3)
            db[start]["cost"] = round(cost, 3)
            db[start]["emoji"] = emoji
            db[start]["ended"] = ended
            db[start]["start_charger_meter"] = start_charger_meter
            db[start]["end_charger_meter"] = end_charger_meter
            if last_session != db[start]:
                _LOGGER.info("Recalculated current hour charging sessions to: %s", pformat(db[start], width=200, compact=True))
                return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant calculate last charging session to CHARGING_HISTORY_DB(%s): %s %s", start, err, type(err))
            self.persistent_notification(
                f"Cant calculate last charging session to CHARGING_HISTORY_DB({start}): {err} {type(err)}",
                f"{self.title} error",
                f"{self.name}_charging_history_recalc_price_calculate_last_charging_session",
            )
        return False

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

    # ------------------------------------------------------------------ #
    # Charging sessions
    # ------------------------------------------------------------------ #

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
        new_battery_level = min(round(current_battery_level + added_percentage, 0), self.get_completed_battery_level())
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

    def stop_current_charging_session(self) -> None:
        self.charging_history(None, "")

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
                    "Charging session ended %s: %s%% %skWh %skr (%skr/kWh) %s %s",
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

    def current_battery_level_expenses(self) -> dict:
        expenses = {
            "battery_level_expenses_kwh": 0.0,
            "battery_level_expenses_percentage": 0.0,
            "battery_level_expenses_solar_percentage": 0.0,
            "battery_level_expenses_cost": 0.0,
            "battery_level_expenses_unit": None,
        }
        self.battery_level_expenses = expenses
        try:
            current_battery_level = self.battery_level()
            db = self.charging_history_db
            if db:
                for key in sorted(db, reverse=True):
                    if round(expenses["battery_level_expenses_percentage"]) >= round(current_battery_level):
                        break
                    entry = db[key]
                    if "cost" not in entry or "kWh" not in entry or "kWh_from_local_energy" not in entry or "percentage" not in entry:
                        continue
                    cost = entry["cost"]
                    kwh = entry["kWh"]
                    percentage = entry["percentage"]
                    solar_percentage = self.kwh_to_percentage(entry["kWh_from_local_energy"], include_charging_loss=True)
                    new_battery_level = percentage + expenses["battery_level_expenses_percentage"]
                    if new_battery_level > current_battery_level and percentage > 0.0:
                        diff = (percentage - (new_battery_level - current_battery_level)) / percentage
                        cost = cost * diff
                        kwh = kwh * diff
                        percentage = percentage * diff
                        solar_percentage = solar_percentage * diff
                    expenses[key] = {
                        "cost": round(cost, 3),
                        "kWh": round(kwh, 3),
                        "percentage": round(percentage, 1),
                        "solar_percentage": round(solar_percentage, 1),
                        "unit": round(cost / kwh, 3) if kwh > 0.0 else 0.0,
                    }
                    expenses["battery_level_expenses_kwh"] = round(expenses["battery_level_expenses_kwh"] + kwh, 3)
                    expenses["battery_level_expenses_percentage"] = round(expenses["battery_level_expenses_percentage"] + percentage, 1)
                    expenses["battery_level_expenses_solar_percentage"] = round(expenses["battery_level_expenses_solar_percentage"] + solar_percentage, 1)
                    expenses["battery_level_expenses_cost"] = round(expenses["battery_level_expenses_cost"] + cost, 3)
                if expenses["battery_level_expenses_kwh"] > 0.0:
                    expenses["battery_level_expenses_unit"] = round(expenses["battery_level_expenses_cost"] / expenses["battery_level_expenses_kwh"], 3)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Error in battery level cost calculation: %s %s", err, type(err))
        return expenses

    # ------------------------------------------------------------------ #
    # Drive efficiency
    # ------------------------------------------------------------------ #

    def load_drive_efficiency(self, database) -> None:
        if not self.is_ev_configured():
            return
        self.drive_efficiency_db = database if isinstance(database, list) and database else []
        self.mark_dirty("drive_efficiency")
        self.set_state_drive_efficiency()

    def save_drive_efficiency(self) -> None:
        if not self.is_ev_configured():
            return
        if len(self.drive_efficiency_db) > 0:
            self.mark_dirty("drive_efficiency")
        self.set_state_drive_efficiency()

    def set_state_drive_efficiency(self) -> None:
        if not self.is_ev_configured():
            return
        key = "drive_efficiency"
        try:
            values = get_list_values(self.drive_efficiency_db)
            if not values:
                _LOGGER.warning("DRIVE_EFFICIENCY_DB is empty or contains no valid float values.")
                return
            self.set_state(key, values[0])
            self.set_attr(key, "ema", float(calculate_ema(reverse_list(values))))
            self.set_attr(key, "average", float(average(values)))
            self.set_attr(key, "trend", float(calculate_trend(reverse_list(values))))
            for item in list(self.own_attr(key, error_state={})):
                if "date" in item:
                    self.del_attr(key, item)
            for i, item in enumerate(self.drive_efficiency_db):
                try:
                    self.set_attr(key, f"date_{item[0]}", round(item[1], 2))
                except Exception:  # noqa: BLE001
                    self.set_attr(key, f"date_{i}", round(item, 2))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant set drive efficiency: %s %s", err, type(err))

    def load_km_kwh_efficiency(self, database) -> None:
        if not self.is_ev_configured():
            return
        self.km_kwh_efficiency_db = database if isinstance(database, list) and database else []
        self.mark_dirty("km_kwh_efficiency")
        self.set_state_km_kwh_efficiency()

    def save_km_kwh_efficiency(self) -> None:
        if not self.is_ev_configured():
            return
        if len(self.km_kwh_efficiency_db) > 0:
            self.mark_dirty("km_kwh_efficiency")
        self.set_state_km_kwh_efficiency()

    def set_state_km_kwh_efficiency(self) -> None:
        if not self.is_ev_configured():
            return
        key = "km_per_kwh"
        distance_kwh = self.t("ui.common.distance_kwh")
        wh_distance = self.t("ui.common.wh_distance")
        try:
            values = get_list_values(self.km_kwh_efficiency_db)
            self.set_state(key, round(values[0] if values else 3.0, 2))
            if values:
                for name, value in (
                    ("ema", float(calculate_ema(reverse_list(values)))),
                    ("average", float(average(values))),
                    ("trend", float(calculate_trend(reverse_list(values)))),
                ):
                    km_kwh = round(value, 2)
                    self.set_attr(key, name, f"{km_kwh:.2f} {distance_kwh} - {round(1000 / km_kwh, 2):.2f} {wh_distance}")
            for item in list(self.own_attr(key, error_state={})):
                if "date" in item:
                    self.del_attr(key, item)
            for i, item in enumerate(self.km_kwh_efficiency_db):
                try:
                    km_kwh = round(item[1], 2)
                    self.set_attr(key, f"date_{item[0]}", f"{km_kwh:.2f} {distance_kwh} - {round(1000 / km_kwh, 2):.2f} {wh_distance}")
                except (IndexError, TypeError):
                    km_kwh = round(item, 2)
                    self.set_attr(key, f"date_{i}", f"{km_kwh:.2f} {distance_kwh} - {round(1000 / km_kwh, 2):.2f} {wh_distance}")
            self.set_estimated_range()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant set %s efficiency: %s %s", distance_kwh, err, type(err))
            self.persistent_notification(f"Cant set km/kwh efficiency: {err} {type(err)}", f"{self.title} warning", f"{self.name}_set_state_km_kwh_efficiency")

    def set_estimated_range(self) -> None:
        if not self.is_ev_configured():
            return
        try:
            if not self.km_kwh_efficiency_db:
                _LOGGER.warning("KM_KWH_EFFICIENCY_DB is empty or contains no valid float values.")
                return
            range_per_percentage = self.km_kwh_to_km_percentage(float(self.get_forecast_value(self.km_kwh_efficiency_db)))
            if range_per_percentage <= 0:
                _LOGGER.warning("Calculated range_per_percentage is not valid (<= 0). Skipping update.")
                return
            range_total = round(range_per_percentage * 100.0, 2)
            self.set_setting_limit("trip_range_needed", round(range_total + 100.0, 0))
            self.set_state("estimated_range", round(range_per_percentage * self.battery_level(), 2))
            self.set_attr("estimated_range", "total", range_total)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Cant set estimated range: %s %s", err, type(err))
            self.persistent_notification(f"Cant set estimated range: {err} {type(err)}", f"{self.title} warning", f"{self.name}_set_estimated_range")

    def set_last_drive_efficiency_attributes(self, kilometers, used_kwh, used_battery, cost, efficiency, distance_per_kwh, wh_km) -> None:
        if not self.is_ev_configured():
            return
        distance_type = self.t("ui.common.distance_type")
        for key in ("drive_efficiency", "km_per_kwh", "estimated_range"):
            self.set_attr(key, "last_drive_distance", f"{round(kilometers, 1)}{distance_type}")
            self.set_attr(key, "last_drive_used", f"{round(used_kwh, 2)}kWh ({round(used_battery, 1)}%)")
            self.set_attr(key, "last_drive_cost", f"{round(cost, 2)}kr")
            self.set_attr(key, "last_drive_efficiency", f"{round(efficiency, 1)}% {round(distance_per_kwh, 2)} {self.t('ui.common.distance_kwh')} ({wh_km} {self.t('ui.common.wh_distance')})")

    def peak_battery_level_after_last_plug(self) -> float:
        max_battery_level = 0.0
        try:
            if not self.is_charger_configured():
                return 0.0
            to_time_stamp = self.now() - datetime.timedelta(minutes=1)
            from_time_stamp = to_time_stamp - datetime.timedelta(days=7)
            status_dict = self.history.get_values(self.config["charger"]["entity_ids"]["status_entity_id"], from_time_stamp, to_time_stamp, include_timestamps=True, error_state={})
            last_connected = None
            for ts in sorted(status_dict.keys(), reverse=True):
                value = status_dict[ts]
                if value in CHARGER_NOT_READY_STATUS:
                    break
                if value in CHARGER_READY_STATUS:
                    last_connected = ts
            if last_connected is None:
                return 0.0
            ev_entity_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"] if self.is_ev_configured() else self.own_entity_id("number", "battery_level")
            max_battery_level = max(self.history.get_values(ev_entity_id, last_connected, to_time_stamp, error_state=[]), default=0.0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("peak_battery_level_after_last_plug(): %s %s", err, type(err))
        return max_battery_level

    def is_battery_fully_charged(self) -> None:
        # a cold battery stops at 99%, that counts as the full charge too
        if self.battery_level() < FULL_CHARGE_BATTERY_LEVEL:
            return
        self.set_state("last_full_charge", self.now())
        self.mark_dirty("runtime")

    def drive_efficiency_save_car_stats(self, bootup=False) -> None:
        def set_last_odometer():
            odometer_value = float(self.get_state(self.config["ev_car"]["entity_ids"]["odometer_entity_id"], float_type=True, error_state="unknown"))
            self.set_state("drive_efficiency_last_odometer", odometer_value)
            self.mark_dirty("runtime")

        def set_last_battery_level():
            key = "drive_efficiency_last_battery_level"
            self.set_state(key, max(self.battery_level(), self.peak_battery_level_after_last_plug()))
            for item in list(self.own_attr(key, error_state={})):
                if item != "description":
                    self.del_attr(key, item)
            self.mark_dirty("runtime")
            expenses = self.battery_level_expenses
            names = ("battery_level_expenses_cost", "battery_level_expenses_kwh", "battery_level_expenses_percentage", "battery_level_expenses_solar_percentage")
            if not all(name in expenses for name in names):
                return
            for name in (*names, "battery_level_expenses_unit"):
                self.set_attr(key, name, expenses[name])

        def set_last_drive_efficiency_data():
            if self.last_drive_efficiency_data:
                return
            key = "drive_efficiency_last_battery_level"
            if "last_drive_efficiency_data" not in self.own_attr(key, error_state={}):
                if not self.testing:
                    return
                self.last_drive_efficiency_data = {
                    "timestamp": self.now(), "distance": 100.0, "usedkWh": 20.0, "usedBattery": 30.0,
                    "cost": 15.0, "efficiency": 90.0, "distancePerkWh": 5.0, "wh_km": 175.0,
                }
                self.set_attr(key, "last_drive_efficiency_data", self.last_drive_efficiency_data)
            attributes = self.own_attr(key)["last_drive_efficiency_data"]
            for name in ("timestamp", "distance", "usedkWh", "usedBattery", "cost", "efficiency", "distancePerkWh", "wh_km", "reported"):
                if name not in attributes:
                    return
            _LOGGER.info("Setting last drive efficiency data")
            self.last_drive_efficiency_data = {
                "timestamp": to_datetime(attributes["timestamp"]),
                "distance": float(attributes["distance"]),
                "usedkWh": float(attributes["usedkWh"]),
                "usedBattery": float(attributes["usedBattery"]),
                "cost": float(attributes["cost"]),
                "efficiency": float(attributes["efficiency"]),
                "distancePerkWh": float(attributes["distancePerkWh"]),
                "wh_km": float(attributes["wh_km"]),
                "reported": attributes["reported"],
            }

        if bootup:
            if self.is_ev_configured() and self.sensor_states.get("drive_efficiency_last_odometer") in ENTITY_UNAVAILABLE_STATES:
                _LOGGER.info("Setting last odometer")
                set_last_odometer()
            if self.sensor_states.get("drive_efficiency_last_battery_level") in ENTITY_UNAVAILABLE_STATES:
                _LOGGER.info("Setting last battery level")
                set_last_battery_level()
            set_last_drive_efficiency_data()
        else:
            if "reported" in self.last_drive_efficiency_data:
                self.last_drive_efficiency_data["reported"] = False
            if self.is_ev_configured():
                set_last_odometer()
            set_last_battery_level()

    def drive_efficiency(self, state=None) -> None:  # noqa: C901
        state = str(state).lower()
        states = (*CHARGER_READY_STATUS, *CHARGER_NOT_READY_STATUS, *EV_PLUGGED_STATES, *EV_UNPLUGGED_STATES, *HEATING_STATES)
        if state not in states:
            _LOGGER.warning("Ignoring state not in %s: %s", states, state)
            return
        t = self.t
        try:
            if state == "preheat_start":
                self.drive_efficiency_save_car_stats()
                self.preheating = True
                return
            if state == "preheat_cancel":
                self.preheating = False
                return
            if state in EV_UNPLUGGED_STATES + CHARGER_NOT_READY_STATUS:
                if self.battery_level() <= 0.0:
                    return
                if not self.preheating:
                    self.drive_efficiency_save_car_stats()
                    if self.is_ev_configured():
                        self.analysis_unplugged()
                self.preheating = False
                return
            if state not in EV_PLUGGED_STATES + CHARGER_READY_STATUS:
                return
            if "reported" in self.last_drive_efficiency_data and self.last_drive_efficiency_data["reported"]:
                return

            if not self.is_ev_configured():
                distance_per_kwh = self.km_percentage_to_km_kwh(self.distance_per_percentage())
                efficiency = 100.0
                last_battery_level = float(self.own_state("drive_efficiency_last_battery_level", float_type=True, error_state=self.battery_level()))
                if last_battery_level == 0.0:
                    last_battery_level = self.get_max_recommended_charge_limit_battery_level() - self.battery_level()
                    _LOGGER.warning("Last battery level is 0.0, recalculating from current battery level: %s", last_battery_level)
                self.current_battery_level_expenses()
                used_battery = last_battery_level - self.battery_level()
                used_kwh = self.percentage_to_kwh(used_battery)
                kilometers = round(self.get_estimated_total_range() * (used_battery / 100.0), 1)
                # entered level above the saved one (charged elsewhere or no unplug seen): no drive to report
                if used_kwh <= 0.0 or used_battery <= 0.0:
                    _LOGGER.warning("Used kWh (%s) or Used Battery (%s) is 0.0 or less, ignoring drive", used_kwh, used_battery)
                    return
            else:
                odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
                if self.sensor_states.get("drive_efficiency_last_odometer") in ENTITY_UNAVAILABLE_STATES:
                    raise ValueError("drive_efficiency_last_odometer is not available, ignoring drive")
                if self.sensor_states.get("drive_efficiency_last_battery_level") in ENTITY_UNAVAILABLE_STATES:
                    raise ValueError("drive_efficiency_last_battery_level is not available, ignoring drive")
                if not self.is_entity_available(odometer_entity):
                    raise ValueError(f"{odometer_entity} is not available, ignoring drive")
                current_odometer = float(self.get_state(odometer_entity, float_type=True))
                last_odometer = float(self.own_state("drive_efficiency_last_odometer", float_type=True, error_state=current_odometer))
                last_battery_level = float(self.own_state("drive_efficiency_last_battery_level", float_type=True, error_state=self.battery_level()))
                used_battery = last_battery_level - self.battery_level()
                kilometers = current_odometer - last_odometer
                used_kwh = self.percentage_to_kwh(used_battery)
                if used_kwh == 0.0 or used_battery == 0.0:
                    _LOGGER.warning("Used kWh or Used Battery is 0.0, ignoring drive")
                    return
                distance_per_kwh = kilometers / used_kwh
                distance_per_percentage = kilometers / used_battery
                cars_distance_per_percentage = round(self.battery_range() / self.battery_level(), 2)
                efficiency = abs(round((distance_per_percentage / cars_distance_per_percentage) * 100.0, 2))
                if kilometers <= 5.0 or used_battery <= 2.0:
                    _LOGGER.warning("%s <= 5.0 or %s usedBattery <= 2.0, ignoring drive", kilometers, used_battery)
                    return
                if efficiency > 150.0:
                    _LOGGER.warning("Efficiency too high: %s%%. Ignoring. UsedBattery: %s, usedkWh: %s, kilometers: %s", efficiency, used_battery, used_kwh, kilometers)
                    return
                self.drive_efficiency_db.insert(0, [self.now(), efficiency])
                self.drive_efficiency_db = self.drive_efficiency_db[: self.config["database"]["drive_efficiency_db_data_to_save"]]
                self.save_drive_efficiency()
                self.analysis_drive(kilometers, used_battery)
                self.km_kwh_efficiency_db.insert(0, [self.now(), distance_per_kwh])
                self.km_kwh_efficiency_db = self.km_kwh_efficiency_db[: self.config["database"]["km_kwh_efficiency_db_data_to_save"]]
                self.save_km_kwh_efficiency()

            cost = 0.0
            kr_km = 0.0
            if self.battery_level_expenses.get("battery_level_expenses_unit") is not None:
                cost = round(self.battery_level_expenses["battery_level_expenses_unit"] * used_kwh, 2)
                kr_km = round(cost / kilometers, 2) if kilometers > 0 else 0.0
            wh_km = round(1000 / distance_per_kwh, 2)
            self.last_drive_efficiency_data = {
                "timestamp": self.now(),
                "distance": kilometers,
                "usedkWh": used_kwh,
                "usedBattery": used_battery,
                "cost": cost,
                "efficiency": efficiency,
                "distancePerkWh": distance_per_kwh,
                "wh_km": wh_km,
                "reported": True,
            }
            self.set_attr("drive_efficiency_last_battery_level", "last_drive_efficiency_data", dict(self.last_drive_efficiency_data))
            self.mark_dirty("runtime")
            _LOGGER.info("Drive: %.1f km, %.2f kWh (%.1f%%), %.1f%% efficiency, %.2f km/kWh", kilometers, used_kwh, used_battery, efficiency, distance_per_kwh)

            if self.config["notification"]["efficiency_on_cable_plug_in"]:
                distance_type = t("ui.common.distance_type")
                lines = [
                    f"{t('ui.drive_efficiency.distance')}: {round(kilometers, 1)}{distance_type}",
                    f"{t('ui.drive_efficiency.used')}: {round(used_kwh, 2)}kWh ({round(used_battery, 1)}%)",
                    f"{t('ui.common.price')}: {cost:.2f}kr ({kr_km:.2f} kr/{distance_type})",
                    f"{t('ui.common.drive_efficiency')}: {round(efficiency, 1)}%",
                    f"{round(distance_per_kwh, 2)} {distance_type}/kWh ({wh_km} Wh/{distance_type})",
                ]
                indent = " " * 24
                message = "\n" + "".join(f"{indent}{line}\n" for line in lines) + indent
                self.my_notify(message=message, title=f"{self.title} {t('ui.drive_efficiency.title')}", always=True)
            self.set_last_drive_efficiency_attributes(kilometers, used_kwh, used_battery, cost, efficiency, distance_per_kwh, wh_km)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in drive_efficiency: %s %s", err, type(err))
            self.persistent_notification(f"Error in drive_efficiency:\n{err} {type(err)}", f"{self.title} warning", f"{self.name}_drive_efficiency")

    # ------------------------------------------------------------------ #
    # Public charging
    # ------------------------------------------------------------------ #

    def _is_not_home(self) -> bool:
        current_location = self.get_state(self.config["ev_car"]["entity_ids"]["location_entity_id"], error_state="away")
        return not (current_location == "home" or current_location in ENTITY_UNAVAILABLE_STATES)

    def _set_public_session_done(self, value: str) -> None:
        old = self.sensor_states.get("public_charging_session_done")
        self.set_state("public_charging_session_done", value)
        self.mark_dirty("runtime")
        if old != value:
            # state_trigger_public_charging_session_done
            self.public_charging_session_done_changed(value)

    def public_charging_session(self, entity_state: str):
        """Charge port/cable changed away from home (or the cost was entered: add_to_history)."""
        t = self.t
        sessions = self.public_charging_sessions
        if entity_state in EV_PLUGGED_STATES and self._is_not_home():
            if sessions and "end_battery_level" not in sessions[-1]:
                return None
            sessions.append({"start_battery_level": self.battery_level()})
            self.mark_dirty("runtime")
        elif sessions and entity_state in EV_UNPLUGGED_STATES and self._is_not_home():
            sessions[-1].update({
                "end_battery_level": self.battery_level(),
                "added_percentage": round(self.battery_level() - sessions[-1]["start_battery_level"], 1),
                "timestamp": self.now(),
            })
            self.mark_dirty("runtime")
            if sessions[-1]["added_percentage"] <= 0.0:
                _LOGGER.warning("Public charging session: End battery level %s%% is less than start battery level %s%%", sessions[-1]["end_battery_level"], sessions[-1]["start_battery_level"])
                sessions.pop(-1)
                if not sessions:
                    self._set_public_session_done("off")
                return None
            self._set_public_session_done("on")
            self.set_attr("public_charging_session_done", "public_charging_session", [dict(s) for s in sessions])
            self.set_setting("public_charging_session_cost", -2.0)
            data = {"actions": [{"action": "URI", "title": t("ui.public_charging_session.action"), "uri": f"entityId:{self.own_entity_id('number', 'public_charging_session_cost')}"}]}
            self.my_notify(
                t(
                    "ui.public_charging_session.message",
                    start_battery_level=sessions[-1]["start_battery_level"],
                    end_battery_level=sessions[-1]["end_battery_level"],
                    added_battery_level=sessions[-1]["added_percentage"],
                ),
                title=f"{self.title} {t('ui.public_charging_session.title')}",
                data=data,
                always=True,
            )
        elif self.get_public_charging_session_done() and sessions and entity_state == "add_to_history":
            cost = self.number_state("public_charging_session_cost")
            if not isinstance(cost, float):
                _LOGGER.warning("Public charging session: Invalid cost %s, not adding to history", cost)
                self.set_setting("public_charging_session_cost", -2.0)
                return None
            if cost == 0.0:
                return None
            if cost == -1.0:
                _LOGGER.info("Public charging session cost reset to -1.0, not adding to history")
                sessions.pop(0)
                self.mark_dirty("runtime")
                if not sessions:
                    self._set_public_session_done("off")
                else:
                    self.set_setting("public_charging_session_cost", -2.0)
                    self.set_attr("public_charging_session_done", "public_charging_session", [dict(s) for s in sessions])
                return None
            if cost < 0.0:
                self.set_setting("public_charging_session_cost", 0.0)
                return None
            current_session = sessions.pop(0)
            self.mark_dirty("runtime")
            added_kwh = self.percentage_to_kwh(current_session["added_percentage"], include_charging_loss=False)
            timestamp = self.now()
            charger_meter = float(self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True))
            self.charging_history_db[timestamp] = {
                "percentage": round(current_session["added_percentage"], 1),
                "kWh": round(added_kwh, 3),
                "kWh_from_public": round(added_kwh, 3),
                "kWh_from_local_energy": 0.0,
                "solar_kwh_of_local_energy": 0.0,
                "powerwall_kwh_of_local_energy": 0.0,
                "cost": round(cost, 3),
                "public_cost": round(cost, 3),
                "unit": round(cost / added_kwh if added_kwh > 0.0 else 0.0, 3),
                "emoji": emoji_parse({"public_charging": True}),
                "start_charger_meter": charger_meter,
                "end_charger_meter": charger_meter,
                "ended": f"{timestamp.strftime('%H:%M')}",
                "charging_type": "public_charging",
            }
            odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
            if odometer_entity:
                odometer = self.get_state(odometer_entity, float_type=True, error_state=None)
                if odometer:
                    self.charging_history_db[timestamp]["odometer"] = int(odometer)
            if not sessions:
                self._set_public_session_done("off")
            else:
                self.set_setting("public_charging_session_cost", -2.0)
                self.set_attr("public_charging_session_done", "public_charging_session", [dict(s) for s in sessions])
            _LOGGER.info("Public charging session added to history: %s%% %.2fkWh %.2fkr", current_session["added_percentage"], added_kwh, cost)
            try:
                self.charging_history_combine_and_set()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in public_charging_session: %s %s", err, type(err))
                self.persistent_notification(f"Error in public_charging_session: {err} {type(err)}", f"{self.title} error", f"{self.name}_public_charging_session_error")
        else:
            _LOGGER.warning(
                "Public charging session: Invalid entity_state transition from %s with %s, get_public_charging_session_done()=%s",
                entity_state, sessions, self.get_public_charging_session_done(),
            )
        return None

    def public_charging_session_done_changed(self, value) -> None:
        """state_trigger_public_charging_session_done"""
        if value in ENTITY_UNAVAILABLE_STATES:
            return
        if value == "off":
            self.public_charging_sessions = []
            self.mark_dirty("runtime")
            self.set_setting("public_charging_session_cost", 0.0)
            self.del_attr("public_charging_session_done", "public_charging_session")

    def manual_add_public_charging_session(self, charging_session_start, battery_level_begin=None, battery_level_end=None, battery_level_added=None,
                                           battery_kwh_added=None, total_charging_cost=None, include_charging_loss=False, add_to_history=False) -> dict:
        try:
            if charging_session_start is None:
                raise ValueError('"Charging Session Start" is required.')
            charging_session_start = to_datetime(charging_session_start)
            if isinstance(charging_session_start, datetime.datetime):
                charging_session_start = charging_session_start.replace(tzinfo=None)
            else:
                raise ValueError('"Charging Session Start" must be a datetime object.')
            if battery_level_begin is None and battery_level_end is None and battery_level_added is None and battery_kwh_added is None:
                raise ValueError('Use at least "Battery Level Begin" and "Battery Level End" or "Battery Level Added" or "Battery kWh Added" to calculate the added energy.')
            if battery_kwh_added is not None:
                if not isinstance(battery_kwh_added, (int, float)):
                    raise ValueError('"Battery kWh Added" must be a number.')
                if battery_kwh_added <= 0:
                    raise ValueError('"Battery kWh Added" must be greater than 0.')
                consumption_used = "Battery kWh Added"
                added_percentage = self.kwh_to_percentage(float(battery_kwh_added), include_charging_loss=include_charging_loss)
                added_kwh = float(battery_kwh_added)
            elif battery_level_added is not None:
                if not isinstance(battery_level_added, (int, float)):
                    raise ValueError('"Battery Level Added" must be a number.')
                if battery_level_added <= 0:
                    raise ValueError('"Battery Level Added" must be greater than 0.')
                consumption_used = "Battery Level Added"
                added_percentage = float(battery_level_added)
                added_kwh = self.percentage_to_kwh(added_percentage, include_charging_loss=include_charging_loss)
            else:
                if battery_level_begin is not None and battery_level_end is None:
                    raise ValueError('"Battery Level End" is required when "Battery Level Begin" is provided.')
                if battery_level_begin is None and battery_level_end is not None:
                    raise ValueError('"Battery Level Begin" is required when "Battery Level End" is provided.')
                if not isinstance(battery_level_begin, (int, float)) or not isinstance(battery_level_end, (int, float)):
                    raise ValueError('"Battery Level Begin" and "Battery Level End" must be numbers.')
                if battery_level_end <= battery_level_begin:
                    raise ValueError('"Battery Level End" must be greater than "Battery Level Begin".')
                consumption_used = "Battery Level Begin/End"
                added_percentage = battery_level_end - battery_level_begin
                added_kwh = self.percentage_to_kwh(added_percentage, include_charging_loss=include_charging_loss)
            if total_charging_cost is None:
                raise ValueError('"Total Charging Cost" is required.')
            if not isinstance(total_charging_cost, (int, float)):
                raise ValueError('"Total Charging Cost" must be a number.')
            if total_charging_cost <= 0:
                raise ValueError('"Total Charging Cost" must be greater than 0.')
            total_charging_cost = float(total_charging_cost)
            kwh_cost = total_charging_cost / added_kwh if added_kwh > 0 else 0.0
            data = {
                "Charging Session Start": charging_session_start.isoformat(),
                "Consumption Used": consumption_used,
                "Added Percentage": round(added_percentage, 1),
                "Added kWh": round(added_kwh, 3),
                "kWh Cost": round(kwh_cost, 3),
                "Total Charging Cost": round(total_charging_cost, 3),
            }
            if not add_to_history:
                return {"status": "success", "message": "Public charging session results. Nothing was added to history.", "data": data}
            charger_meter = float(self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True))
            self.charging_history_db[charging_session_start] = {
                "percentage": round(added_percentage, 1),
                "kWh": round(added_kwh, 3),
                "kWh_from_public": round(added_kwh, 3),
                "kWh_from_local_energy": 0.0,
                "solar_kwh_of_local_energy": 0.0,
                "powerwall_kwh_of_local_energy": 0.0,
                "cost": round(total_charging_cost, 3),
                "public_cost": round(total_charging_cost, 3),
                "unit": round(kwh_cost, 3),
                "emoji": emoji_parse({"public_charging": True}),
                "start_charger_meter": charger_meter,
                "end_charger_meter": charger_meter,
                "ended": f"{charging_session_start.strftime('%H:%M')}",
                "charging_type": "public_charging",
            }
            self.charging_history_combine_and_set()
            return {"status": "success", "message": "Public charging session added successfully to history.", "data": data}
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in manual_add_public_charging_session: %s", err)
            return {"status": "error", "message": f"{err}"}
