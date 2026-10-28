"""Recalculation of the prices in the charging history."""
from __future__ import annotations

import logging
from pprint import pformat

from ...utils.formatting import emoji_sorting, emoji_update_local_energy, join_unique_emojis
from ...utils.helpers import hours_between

_LOGGER = logging.getLogger(__name__)


class HistoryRecalculate:
    """Recalculation of the prices in the charging history."""

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
