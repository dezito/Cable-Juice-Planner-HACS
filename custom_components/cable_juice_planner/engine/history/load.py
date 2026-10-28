"""Loading and saving of the charging history and the efficiency databases."""
from __future__ import annotations

import datetime
import logging
from pprint import pformat

from ...utils.helpers import hours_between, months_between

_LOGGER = logging.getLogger(__name__)


class HistoryLoad:
    """Loading and saving of the charging history and the efficiency databases."""

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
