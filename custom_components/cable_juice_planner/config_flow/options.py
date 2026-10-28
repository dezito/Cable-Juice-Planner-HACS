"""The options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from ..management.backup import BACKUP_DIR, async_export, async_import, backup_dir, list_backups
from ..const import CONF_ENTITY_PREFIX, SITE_DEFAULTS, OPTIONS_MENU, default_options, object_prefix, normalize_language
from .schema import BACKUP_EXPORT, providers_from_options, site_options, site_schema
from .pages import CJPFlowPages


class CJPOptionsFlow(CJPFlowPages, OptionsFlow):
    """Change the Cable Juice Planner configuration (the entry is reloaded when saved)."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._queue: list[str] = []
        self._providers: list[str] = []

    def _placeholders(self) -> dict[str, str]:
        return {"prefix": self.config_entry.data.get(CONF_ENTITY_PREFIX, "")}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._options = {**default_options(), **self.config_entry.options}
        self._options["language"] = normalize_language(self._options.get("language"))
        self._providers = providers_from_options(self._options)
        return self.async_show_menu(step_id="init", menu_options=[*(f"menu_{group}" for group in OPTIONS_MENU), "backup"])

    async def async_step_backup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Export the data now, or replace it with a backup (the entry is reloaded)."""
        if user_input is not None:
            choice = user_input.get("backup", BACKUP_EXPORT)
            if choice == BACKUP_EXPORT:
                name = await async_export(self.hass, self.config_entry)
                return self.async_abort(reason="backup_exported", description_placeholders={"file": f"{BACKUP_DIR}/{name}"})
            try:
                await async_import(self.hass, self.config_entry, choice, bool(user_input.get("restore_configuration")))
            except HomeAssistantError as err:
                return self.async_abort(reason="backup_failed", description_placeholders={"error": str(err)})
            return self.async_abort(reason="backup_imported", description_placeholders={"file": f"{BACKUP_DIR}/{choice}"})

        backups = await self.hass.async_add_executor_job(list_backups, backup_dir(self.hass))
        options = [selector.SelectOptionDict(value=name, label=label) for name, label in backups.items()]
        schema = vol.Schema(
            {
                vol.Required("backup", default=BACKUP_EXPORT): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[selector.SelectOptionDict(value=BACKUP_EXPORT, label=BACKUP_EXPORT), *options],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="backup_action",
                    )
                ),
                vol.Required("restore_configuration", default=False): selector.BooleanSelector(),
            }
        )
        prefix = object_prefix(self.config_entry.data.get(CONF_ENTITY_PREFIX))
        return self.async_show_form(step_id="backup", data_schema=schema, description_placeholders={"prefix": prefix, "folder": BACKUP_DIR})

    async def _async_menu(self, group: str) -> ConfigFlowResult:
        self._queue = list(OPTIONS_MENU[group])
        return await self._async_next()

    async def async_step_menu_general(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("general")

    async def async_step_menu_charger(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("charger")

    async def async_step_menu_ev(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("ev")

    async def async_step_menu_home(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("home")

    async def async_step_menu_prices(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("prices")

    async def async_step_menu_solar(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("solar")

    async def async_step_menu_notifications(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("notifications")

    async def async_step_menu_advanced(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("advanced")

    async def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)


class CJPSiteOptionsFlow(OptionsFlow):
    """Change the shared grid connection."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_site()

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=site_options(user_input))
        return self.async_show_form(step_id="site", data_schema=site_schema({**SITE_DEFAULTS, **self.config_entry.options}))
