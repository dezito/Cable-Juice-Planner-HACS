"""Cable Juice Planner buttons (replace the input_button helpers)."""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .backup import BACKUP_DIR, async_export
from .const import BUTTON_KEYS, EV_BUTTON_KEYS, is_ev_configured
from .coordinator import CJPCoordinator
from .dashboard import async_reset_dashboard
from .entity import CJPEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    keys = [*BUTTON_KEYS, *(EV_BUTTON_KEYS if is_ev_configured(coordinator.config) else ())]
    entities = [*(CJPButton(coordinator, "button", key) for key in keys), CJPExportButton(coordinator, "button", "export_data")]
    if coordinator.config.get("dashboard"):
        entities.append(CJPResetDashboardButton(coordinator, "button", "reset_dashboard"))
    async_add_entities(entities)


class CJPButton(CJPEntity, ButtonEntity):
    """enforce_planning (plan now, ignoring the unchanged-input shortcut), trip_reset and
    recalculate_charging_loss (forget the measurements and read the history again)."""

    async def async_press(self) -> None:
        self.coordinator.async_press(self._key)


class CJPExportButton(CJPEntity, ButtonEntity):
    """Write a backup of the databases and settings to cable_juice_planner_backups."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        name = await async_export(self.hass, self.coordinator.entry)
        persistent_notification.async_create(
            self.hass,
            self.coordinator.t("ui.backup.exported", file=f"{BACKUP_DIR}/{name}"),
            title=f"📟{self.coordinator.title}",
            notification_id=f"{self.coordinator.object_prefix}_backup",
        )


class CJPResetDashboardButton(CJPEntity, ButtonEntity):
    """Throw away the edits made to the sidebar dashboard in the UI."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await async_reset_dashboard(self.hass, self.coordinator.prefix)
