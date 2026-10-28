"""The config flow (first setup, restore of a backup, import of legacy data)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from ..management.backup import BACKUP_DIR, async_load_backup, backup_dir, list_backups
from ..const import CONF_ENTITY_PREFIX, CONF_SITE, SITE_DEFAULTS, SITE_TITLE, DEFAULT_ENTITY_PREFIX, DOMAIN, SENSOR_DEFS, default_options, CONF_CAR_NAME, instance_title, normalize_prefix, object_prefix, normalize_language
from ..management.storage import find_legacy_sources, load_legacy_config
from .pages import CJPFlowPages
from .options import CJPOptionsFlow, CJPSiteOptionsFlow
from .schema import CONFIG_FLOW_STEPS, IMPORT_NONE, PREFIX_RE, RESERVED_PREFIXES, SELECTORS, options_from_legacy, providers_from_options, site_options, site_schema


class CJPConfigFlow(CJPFlowPages, ConfigFlow, domain=DOMAIN):
    """Handle the Cable Juice Planner config flow."""

    VERSION = 2
    MINOR_VERSION = 3

    def __init__(self) -> None:
        self._prefix = DEFAULT_ENTITY_PREFIX
        self._car_name = ""
        self._options: dict[str, Any] = default_options()
        self._initial_settings: dict[str, Any] = {}
        self._legacy_sources: dict[str, list[str]] = {}
        self._legacy_prefix: str | None = None
        self._backups: dict[str, str] = {}
        self._restore_backup: str | None = None
        self._queue: list[str] = []
        self._providers: list[str] = ["energidataservice"]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        if config_entry.data.get(CONF_SITE):
            return CJPSiteOptionsFlow()
        return CJPOptionsFlow()

    def _placeholders(self) -> dict[str, str]:
        return {"prefix": self._prefix}

    def _foreign_entities(self, prefix: str) -> list[str]:
        """Entity ids this prefix would create that already belong to something else (e.g. PyScript)."""
        ent_reg = er.async_get(self.hass)
        found = []
        for key in SENSOR_DEFS:
            entity_id = f"sensor.{object_prefix(prefix)}_{key}"
            entry = ent_reg.async_get(entity_id)
            if (entry is not None and entry.platform != DOMAIN) or (entry is None and self.hass.states.get(entity_id)):
                found.append(entity_id)
        return found

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A car/charger, or the shared grid connection (once) for load balancing."""
        if self._site_exists():
            return await self.async_step_instance()
        return self.async_show_menu(step_id="user", menu_options=["instance", "site"])

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._site_exists():
            return self.async_abort(reason="site_exists")
        if user_input is not None:
            return self.async_create_entry(title=SITE_TITLE, data={CONF_SITE: True}, options=site_options(user_input))
        return self.async_show_form(step_id="site", data_schema=site_schema(dict(SITE_DEFAULTS)))

    async def async_step_instance(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            prefix = normalize_prefix(user_input.get(CONF_ENTITY_PREFIX))
            used = {normalize_prefix(entry.data.get(CONF_ENTITY_PREFIX)) for entry in self._async_current_entries() if not entry.data.get(CONF_SITE)}
            if not PREFIX_RE.match(prefix) or prefix in RESERVED_PREFIXES:
                errors[CONF_ENTITY_PREFIX] = "invalid_prefix"
            elif prefix in used:
                errors[CONF_ENTITY_PREFIX] = "prefix_in_use"
            elif self._foreign_entities(prefix):
                errors[CONF_ENTITY_PREFIX] = "prefix_entities_exist"
            else:
                self._prefix = prefix
                self._car_name = (user_input.get(CONF_CAR_NAME) or "").strip()
                self._options["language"] = user_input.get("language", self._options["language"])
                self._queue = list(CONFIG_FLOW_STEPS)
                self._backups = await self.hass.async_add_executor_job(list_backups, backup_dir(self.hass))
                if self._backups:
                    return await self.async_step_restore_backup()
                return await self._async_legacy_or_next()

        schema = vol.Schema(
            {
                vol.Optional(CONF_CAR_NAME, description={"suggested_value": self._car_name or None}): selector.TextSelector(),
                vol.Optional(CONF_ENTITY_PREFIX, description={"suggested_value": self._prefix or None}): selector.TextSelector(),
                vol.Required("language", default=normalize_language(self.hass.config.language)): SELECTORS["language"],
            }
        )
        return self.async_show_form(step_id="instance", data_schema=schema, errors=errors)

    async def _async_legacy_or_next(self) -> ConfigFlowResult:
        self._legacy_sources = await self.hass.async_add_executor_job(find_legacy_sources, self.hass.config.config_dir)
        if self._legacy_sources:
            return await self.async_step_import_legacy()
        return await self._async_next()

    async def async_step_restore_backup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which exported or deleted instance (if any) should be restored."""
        if user_input is not None:
            name = user_input.get("backup", IMPORT_NONE)
            if name not in self._backups:
                return await self._async_legacy_or_next()
            backup = await async_load_backup(self.hass, name)
            self._restore_backup = name
            self._car_name = self._car_name or (backup.get(CONF_CAR_NAME) or "").strip()
            language = self._options["language"]
            self._options = {**default_options(), **(backup.get("options") or {}), "language": language}
            self._providers = providers_from_options(self._options)
            return await self._async_next()

        options = [selector.SelectOptionDict(value=name, label=label) for name, label in self._backups.items()]
        schema = vol.Schema(
            {
                vol.Required("backup", default=next(iter(self._backups))): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[*options, selector.SelectOptionDict(value=IMPORT_NONE, label=IMPORT_NONE)],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="restore_backup",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="restore_backup", data_schema=schema, description_placeholders={"prefix": object_prefix(self._prefix), "folder": BACKUP_DIR}
        )

    async def async_step_import_legacy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which PyScript installation (if any) should be imported."""
        if user_input is not None:
            source = user_input.get("import_source", IMPORT_NONE)
            if source in self._legacy_sources:
                self._legacy_prefix = source
                language = self._options["language"]
                legacy = await self.hass.async_add_executor_job(load_legacy_config, self.hass.config.config_dir, source)
                if legacy:
                    self._options, self._initial_settings = options_from_legacy(legacy)
                    self._options["language"] = language
                    self._providers = providers_from_options(self._options)
            return await self._async_next()

        sources = list(self._legacy_sources)
        default = self._prefix if self._prefix in sources else sources[0]
        schema = vol.Schema(
            {
                vol.Required("import_source", default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[*sources, IMPORT_NONE], mode=selector.SelectSelectorMode.LIST, translation_key="import_source")
                )
            }
        )
        files = "\n".join(f"- **{source}**: {', '.join(names)}" for source, names in self._legacy_sources.items())
        return self.async_show_form(step_id="import_legacy", data_schema=schema, description_placeholders={"prefix": object_prefix(self._prefix), "files": files})

    async def _async_finish(self) -> ConfigFlowResult:
        title = instance_title(self._prefix, self._car_name)
        return self.async_create_entry(
            title=title,
            data={
                CONF_ENTITY_PREFIX: self._prefix,
                CONF_CAR_NAME: self._car_name,
                "legacy_prefix": self._legacy_prefix,
                "initial_settings": self._initial_settings,
                "restore_backup": self._restore_backup,
            },
            options=self._options,
        )
