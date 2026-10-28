"""Skipping unchanged plans and the default entity states."""
from __future__ import annotations

import datetime
import hashlib
import json
import logging

from ..const import CHARGING_TYPES, EMOJI_DESCRIPTION_GROUPS
from ..utils.helpers import limit_dict_size

_LOGGER = logging.getLogger(__name__)


class Planning:
    """Skipping unchanged plans and the default entity states."""

    def get_hash_dict(self) -> dict:
        return {
            "CURRENT_HOUR": self.current_hour(),
            "LAST_DRIVE_EFFICIENCY_DATA": self.last_drive_efficiency_data,
            "LAST_SUCCESSFUL_GRID_PRICES": self.grid_price_cache,
            "BATTERY_LEVEL": self.battery_level(),
            "FULL_CHARGE_99_WAITING": self.full_charge_99_since is not None,
            "BATTERY_LEVEL_EXPENSES": self.battery_level_expenses,
            "SETTINGS": (
                self.is_ev_home(),
                self.is_calculating_charging_loss(),
                self.get_min_daily_battery_level(),
                self.get_min_trip_battery_level(),
                self.get_min_charge_limit_battery_level(),
                self.get_max_recommended_charge_limit_battery_level(),
                self.get_very_cheap_grid_charging_max_battery_level(),
                self.get_ultra_cheap_grid_charging_max_battery_level(),
                self.get_powerwall_discharge_above_needed(),
                self.get_powerwall_battery_level(),
                self.get_ev_charge_after_powerwall_battery_level(),
                self.get_completed_battery_level(),
                self.get_estimated_total_range(),
                self.get_trip_date_time(),
                self.get_trip_homecoming_date_time(),
                self.get_trip_range(),
                self.get_trip_target_level(),
                self.get_public_charging_session_done(),
                self.number_state("full_charge_recommended", error_state=0.0),
                self.is_trip_planned(),
                self.fill_up_charging_enabled(),
                self.workplan_charging_enabled(),
                self.solar_charging_enabled(),
                self.manual_charging_enabled(),
                self.manual_charging_solar_enabled(),
            ),
        }

    @staticmethod
    def make_hashable_snapshot(data) -> str:
        def key_of(key):
            return key.isoformat() if isinstance(key, (datetime.datetime, datetime.date, datetime.time)) else str(key)

        def normalize(obj):
            if isinstance(obj, dict):
                return {key_of(key): normalize(value) for key, value in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [normalize(value) for value in obj]
            if isinstance(obj, set):
                return sorted(normalize(value) for value in obj)
            if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
                return obj.isoformat()
            return obj

        json_data = json.dumps(normalize(data), sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.blake2b(json_data.encode(), digest_size=16).hexdigest()

    def save_hashable_snapshot(self) -> None:
        for key, item in self.get_hash_dict().items():
            self.last_hash_results[key] = self.make_hashable_snapshot(item)

    def should_skip_calculation(self) -> bool:
        skip = True
        for key, item in self.get_hash_dict().items():
            if key not in self.last_hash_results:
                skip = False
                continue
            item_hash = self.make_hashable_snapshot(item)
            if item_hash != self.last_hash_results[key]:
                skip = False
                _LOGGER.debug("Hash snapshot changed for %s: %s != %s", key, self.last_hash_results[key], item_hash)
        return skip

    def append_overview_output(self, title=None, timestamp=None) -> None:
        if timestamp is None:
            timestamp = self.now().strftime("%Y-%m-%d %H:%M")
        key = f"{timestamp} {title}"
        if key in self.overview_history:
            return
        self.overview_history[key] = [self.own_attr("overview", "overview", error_state="")]
        self.overview_history = limit_dict_size(self.overview_history, 10)

    def set_entity_friendlynames(self) -> None:
        """The 'name' attribute of the sensors that belong to a charging type."""
        for key, entry in CHARGING_TYPES.items():
            if "entity_name" in entry:
                self.set_attr(entry["entity_name"], "name", f"{entry['emoji']} {self.t(f'charging_type.{key}.description', default='No description')}")

    def emoji_description(self) -> None:
        lines = [f"## {self.t('ui.emoji_description.header')} ##"]
        for group in EMOJI_DESCRIPTION_GROUPS:
            entries = sorted(((key, entry) for key, entry in CHARGING_TYPES.items() if entry.get("group") == group), key=lambda kv: float(kv[1].get("priority", 0)))
            items = [f"**{entry.get('emoji', '❓')} {self.t(f'charging_type.{key}.description', default='No description')}**" for key, entry in entries]
            if items:
                lines.append(f"### {self.t(f'ui.emoji_description.groups.{group}')}")
                lines.extend(f"* {item}" for item in items)
        self.set_state("emoji_description", self.t("ui.emoji_description.markdown_hint", name=self.name))
        self.set_attr("emoji_description", "description", "\n".join(lines))

    def set_default_entity_states(self) -> None:
        self.set_state("overview", self.t("ui.set_default_entity_states.markdown_guide", name=self.name))
        self.set_attr("overview", "overview", f"<center>\n\n**{self.t('ui.set_default_entity_states.no_overview')}**\n\n</center>")
