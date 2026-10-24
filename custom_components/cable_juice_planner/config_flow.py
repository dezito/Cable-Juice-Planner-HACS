"""Config flow for Cable Juice Planner.

Everything from <prefix>_config.yaml is configured here, split into small pages with
help texts. The settings that the dashboard changes at runtime (charging modes,
workdays, battery levels, trip, ...) are entities.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .backup import BACKUP_DIR, async_export, async_import, async_load_backup, backup_dir, list_backups
from .const import (
    CONF_ENTITY_PREFIX,
    CONF_SITE,
    PHASE_ORDERS,
    SITE_DEFAULTS,
    SITE_FIELDS,
    SITE_TITLE,
    CONFIG_KEYS_RENAMING,
    DEFAULT_ENTITY_PREFIX,
    DOMAIN,
    ENTITY_OPTION_KEYS,
    FLOW_STEPS,
    LANGUAGES,
    LIST_OPTION_KEYS,
    NUMBER_SETTINGS,
    OPTION_PATHS,
    OPTIONS_MENU,
    PRICE_PROVIDER_KEYS,
    SENSOR_DEFS,
    default_options,
    CONF_CAR_NAME,
    instance_title,
    normalize_prefix,
    object_prefix,
    normalize_language,
)
from .helpers import delete_dict_key_with_path, get_dict_value_with_path, set_dict_value_with_path
from .storage import find_legacy_sources, load_legacy_config

PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9_]{0,40})?$")  # optional, entity ids are cjp_<prefix>_*
RESERVED_PREFIXES = {"site"}  # sensor.cjp_site_* belong to the shared grid connection
IMPORT_NONE = "none"
BACKUP_EXPORT = "export"
CRON_INTERVALS = ["1", "2", "3", "4", "5", "6", "10", "12", "15", "20", "30"]
PRICE_PROVIDERS = ["energidataservice", "stromligning"]
ENERGIDATASERVICE_URL = "https://github.com/MTrab/energidataservice"  # {eds_url} in the step descriptions
EV_REQUIRED = ("usable_battery_level_entity_id", "estimated_battery_range_entity_id", "odometer_entity_id")

CONFIG_FLOW_STEPS = [
    "charger",
    "charger_specs",
    "load_balancing",
    "ev",
    "ev_specs",
    "home",
    "powerwall",
    "price_provider",
    "prices_energidataservice",
    "prices_stromligning",
    "prices_stromligning_tariffs",
    "price_fees",
    "solar",
    "solar_charging",
    "weather",
    "notifications",
    "advanced",
    "database",
]


def _entity(domain: str | list[str], *, device_class: str | list[str] | None = None, integration: str | None = None, multiple: bool = False) -> selector.EntitySelector:
    """Entity picker limited to the relevant entities (domain + device class or integration)."""
    entity_filter: dict[str, Any] = {"domain": domain}
    if device_class:
        entity_filter["device_class"] = device_class
    if integration:
        entity_filter["integration"] = integration
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=[entity_filter], multiple=multiple))


def _number(minimum: float, maximum: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config: dict[str, Any] = {"min": minimum, "max": maximum, "step": step, "mode": selector.NumberSelectorMode.BOX}
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))


BOOLEAN = selector.BooleanSelector()

SELECTORS: dict[str, Any] = {
    "language": selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=code, label=label) for code, label in LANGUAGES.items()],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    ),
    # charger
    "status_entity_id": _entity("sensor"),
    "power_consumtion_entity_id": _entity("sensor", device_class="power"),
    "kwh_meter_entity_id": _entity("sensor", device_class="energy"),
    "lifetime_kwh_meter_entity_id": _entity("sensor", device_class="energy"),
    "enabled_entity_id": _entity(["switch", "binary_sensor", "input_boolean"]),
    "cable_connected_entity_id": _entity(["binary_sensor", "sensor"]),
    "co2_entity_id": _entity("sensor", integration="energidataservice"),
    "power_voltage": _number(100, 480, 1, "V"),
    "charging_phases": _number(1, 3, 1),
    "charging_max_amp": _number(6, 80, 1, "A"),
    # shown in % (0 or a loss), stored as a fraction (-5 % -> -0.05) like ev.py
    "charging_loss": _number(-50, 0, 0.1, "%"),
    # load balancing (only shown when a site entry exists)
    "site_priority": _number(1, 10, 1),
    "site_phase_order": selector.SelectSelector(selector.SelectSelectorConfig(options=list(PHASE_ORDERS), mode=selector.SelectSelectorMode.DROPDOWN)),
    # car
    "usable_battery_level_entity_id": _entity("sensor", device_class="battery"),
    "estimated_battery_range_entity_id": _entity("sensor", device_class="distance"),
    "odometer_entity_id": _entity("sensor", device_class="distance"),
    "charge_port_door_entity_id": _entity(["cover", "binary_sensor", "sensor", "lock"]),
    "charge_cable_entity_id": _entity(["binary_sensor", "sensor"]),
    "charging_limit_entity_id": _entity(["number", "sensor"]),
    "location_entity_id": _entity(["device_tracker", "person"]),
    "last_update_entity_id": _entity("sensor", device_class="timestamp"),
    "climate_entity_id": _entity(["climate", "switch", "button", "input_boolean", "input_button"]),
    "battery_size": _number(1, 250, 0.1, "kWh"),
    "last_charging_hour_before_departure_buffer": _number(0, 12, 1, "h"),
    # home / home battery
    "power_consumption_entity_id": _entity("sensor", device_class="power"),
    "power_consumption_entity_id_include_powerwall_charging": BOOLEAN,
    "power_consumption_entity_id_include_powerwall_discharging": BOOLEAN,
    "ignore_consumption_from_entity_ids": _entity("sensor", device_class="power", multiple=True),
    "powerwall_watt_flow_entity_id": _entity("sensor", device_class="power"),
    "invert_powerwall_watt_flow_entity_id": BOOLEAN,
    "powerwall_battery_level_entity_id": _entity("sensor", device_class="battery"),
    "powerwall_battery_size": _number(0.1, 1000, 0.1, "kWh"),
    "powerwall_charging_power_limit": _number(100, 100000, 100, "W"),
    "powerwall_discharging_power": _number(100, 100000, 100, "W"),
    # prices (only entities from the price integrations)
    "energidataservice_buy_entity_id": _entity("sensor", integration="energidataservice"),
    "energidataservice_sell_entity_id": _entity("sensor", integration="energidataservice"),
    "stromligning_buy_current_price": _entity("sensor", integration="stromligning"),
    "stromligning_buy_tomorrow_available": _entity("binary_sensor", integration="stromligning"),
    "stromligning_buy_forecasts": _entity("sensor", integration="stromligning"),
    "stromligning_sell_current_price": _entity("sensor", integration="stromligning"),
    "stromligning_sell_tomorrow_available": _entity("binary_sensor", integration="stromligning"),
    "stromligning_sell_forecasts": _entity("sensor", integration="stromligning"),
    "stromligning_sell_distribution": _entity("sensor", integration="stromligning"),
    "stromligning_sell_electricity_tax": _entity("sensor", integration="stromligning"),
    "stromligning_sell_nettariff": _entity("sensor", integration="stromligning"),
    "stromligning_sell_systemtariff": _entity("sensor", integration="stromligning"),
    "stromligning_sell_surcharge": _entity("sensor", integration="stromligning"),
    "refund": _number(0, 10, 0.001, "kr/kWh"),
    "sell_fee": _number(0, 10, 0.001, "kr/kWh"),
    # solar / weather
    "production_entity_id": _entity("sensor", device_class="power"),
    "forecast_entity_id": _entity("sensor", integration="solcast_solar"),
    "inverter_discharging_power_limit": _number(100, 100000, 100, "W"),
    "enable_selling_during_expensive_hours": BOOLEAN,
    # -1 = use the hourly sell price
    "solar_sell_fixed_price": _number(-1, 10, 0.01, "kr/kWh"),
    "solarpower_use_before_minutes": _number(0, 180, 1, "min"),
    "max_to_current_hour": BOOLEAN,
    "allow_grid_charging_above_solar_available": _number(-10000, 10000, 10, "W"),
    "charging_single_phase_min_amp": _number(0, 32, 1, "A"),
    "charging_single_phase_max_amp": _number(0, 32, 1, "A"),
    "charging_three_phase_min_amp": _number(0, 32, 1, "A"),
    "hourly_service_entity_id": _entity("weather"),
    "daily_service_entity_id": _entity("weather"),
    "outdoor_temp_entity_id": _entity("sensor", device_class="temperature"),
    # notifications
    "efficiency_on_cable_plug_in": BOOLEAN,
    "preheating": BOOLEAN,
    # advanced / database
    "cron_interval": selector.SelectSelector(selector.SelectSelectorConfig(options=CRON_INTERVALS, mode=selector.SelectSelectorMode.DROPDOWN)),
    "dashboard": BOOLEAN,
    "enable_diagnostic_file_log": BOOLEAN,
    "testing_mode": BOOLEAN,
    "power_values_db_data_to_save": _number(1, 100, 1, "d"),
    "solar_available_db_data_to_save": _number(1, 100, 1),
    "kwh_avg_prices_db_data_to_save": _number(1, 100, 1, "d"),
    "drive_efficiency_db_data_to_save": _number(1, 100, 1),
    "km_kwh_efficiency_db_data_to_save": _number(1, 100, 1),
    "charging_history_db_data_to_save": _number(1, 60, 1),
}

REQUIRED_KEYS = {
    "language",
    "site_priority",
    "site_phase_order",
    "status_entity_id",
    "power_consumtion_entity_id",
    "kwh_meter_entity_id",
    "lifetime_kwh_meter_entity_id",
    "power_voltage",
    "charging_phases",
    "charging_max_amp",
    "charging_loss",
    "battery_size",
    "last_charging_hour_before_departure_buffer",
    "powerwall_battery_size",
    "powerwall_charging_power_limit",
    "powerwall_discharging_power",
    "energidataservice_buy_entity_id",
    "stromligning_buy_current_price",
    "refund",
    "sell_fee",
    "inverter_discharging_power_limit",
    "solar_sell_fixed_price",
    "solarpower_use_before_minutes",
    "allow_grid_charging_above_solar_available",
    "charging_single_phase_min_amp",
    "charging_single_phase_max_amp",
    "charging_three_phase_min_amp",
    "cron_interval",
    "power_values_db_data_to_save",
    "solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save",
    "drive_efficiency_db_data_to_save",
    "km_kwh_efficiency_db_data_to_save",
    "charging_history_db_data_to_save",
}
INT_KEYS = {
    "cron_interval",
    "site_priority",
    "last_charging_hour_before_departure_buffer",
    "solarpower_use_before_minutes",
    "power_values_db_data_to_save",
    "solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save",
    "drive_efficiency_db_data_to_save",
    "km_kwh_efficiency_db_data_to_save",
    "charging_history_db_data_to_save",
}


def _form_value(key: str, value: Any) -> Any:
    if key == "charging_loss":
        return round(min(float(value or 0.0), 0.0) * 100, 1)
    if key == "cron_interval":
        return str(int(value)) if value not in (None, "") else "5"
    if key in LIST_OPTION_KEYS:
        return list(value or [])
    return value


def build_schema(keys: list[str], values: dict[str, Any], notify_services: list[str] | None = None) -> vol.Schema:
    schema: dict[Any, Any] = {}
    for key in keys:
        value = _form_value(key, values.get(key))
        if key == "notify_list":
            options = sorted({*(notify_services or []), *value})
            schema[vol.Optional(key, description={"suggested_value": value})] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN)
            )
        elif key in ENTITY_OPTION_KEYS:
            marker = vol.Required if key in REQUIRED_KEYS else vol.Optional
            schema[marker(key, description={"suggested_value": value or None})] = SELECTORS[key]
        elif key in LIST_OPTION_KEYS:
            schema[vol.Optional(key, description={"suggested_value": value})] = SELECTORS[key]
        elif key in REQUIRED_KEYS:
            schema[vol.Required(key, default=value)] = SELECTORS[key]
        else:
            schema[vol.Optional(key, default=bool(value))] = SELECTORS[key]
    return vol.Schema(schema)


def normalize_input(keys: list[str], user_input: dict[str, Any]) -> dict[str, Any]:
    """Convert form values to option values (cleared optional entities become '')."""
    result: dict[str, Any] = {}
    for key in keys:
        value = user_input.get(key)
        if key in ENTITY_OPTION_KEYS:
            result[key] = value or ""
        elif key in LIST_OPTION_KEYS:
            result[key] = [item for item in (value or []) if item]
        elif key == "charging_loss":
            result[key] = round(min(float(value or 0.0), 0.0) / 100, 4)
        elif key in INT_KEYS:
            result[key] = int(float(value))
        elif SELECTORS.get(key) is BOOLEAN:
            result[key] = bool(value)
        elif isinstance(value, (int, float)):
            result[key] = float(value)
        else:
            result[key] = value
    return result


def validate_step(step: str, values: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if step == "ev":
        configured = [key for key in EV_REQUIRED if values.get(key)]
        if configured and len(configured) != len(EV_REQUIRED):
            errors["base"] = "ev_incomplete"
    if step == "solar" and values.get("forecast_entity_id") and not values.get("production_entity_id"):
        errors["production_entity_id"] = "production_required"
    if step == "solar_charging" and values["charging_single_phase_min_amp"] > values["charging_single_phase_max_amp"]:
        errors["charging_single_phase_min_amp"] = "min_above_max"
    return errors


def options_from_legacy(legacy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flatten a PyScript <prefix>_config.yaml into config entry options + initial entity values."""
    legacy = deepcopy(legacy)
    for old_path, new_path in CONFIG_KEYS_RENAMING.items():
        value = get_dict_value_with_path(legacy, old_path)
        if value is not None:
            set_dict_value_with_path(legacy, new_path, value)
            delete_dict_key_with_path(legacy, old_path)
    options = default_options()
    for key, path in OPTION_PATHS.items():
        value = get_dict_value_with_path(legacy, path)
        if value is None:
            continue
        if key in ENTITY_OPTION_KEYS:
            options[key] = value or ""
        elif key in LIST_OPTION_KEYS:
            options[key] = [e for e in value if e] if isinstance(value, list) else []
        elif key in INT_KEYS:
            options[key] = int(value)
        else:
            options[key] = value
    options["language"] = normalize_language(options.get("language"), "da")
    if str(options.get("cron_interval")) not in CRON_INTERVALS:
        options["cron_interval"] = 5
    settings = {}
    for key, info in NUMBER_SETTINGS.items():
        if info.get("config") and isinstance(value := get_dict_value_with_path(legacy, info["config"]), (int, float)):
            settings[key] = value
    return options, settings


def providers_from_options(options: dict[str, Any]) -> list[str]:
    providers = []
    if options.get("energidataservice_buy_entity_id"):
        providers.append("energidataservice")
    if options.get("stromligning_buy_current_price"):
        providers.append("stromligning")
    return providers or ["energidataservice"]


class CJPFlowPages:
    """Pages shared by the config flow and the options flow.

    The subclass sets ``self._options`` (flat option dict), ``self._queue`` (remaining
    page ids) and implements ``_async_finish``.
    """

    _options: dict[str, Any]
    _queue: list[str]
    _providers: list[str]

    def _site_exists(self) -> bool:
        return any(entry.data.get(CONF_SITE) for entry in self.hass.config_entries.async_entries(DOMAIN))  # type: ignore[attr-defined]

    def _page_enabled(self, step: str) -> bool:
        if step == "load_balancing":
            return self._site_exists()
        if step == "prices_energidataservice":
            return "energidataservice" in self._providers
        if step == "prices_stromligning":
            return "stromligning" in self._providers
        if step == "prices_stromligning_tariffs":
            return "stromligning" in self._providers and bool(self._options.get("stromligning_sell_current_price"))
        if step in ("solar_charging", "weather"):
            return bool(self._options.get("production_entity_id"))
        return True

    async def _async_next(self) -> ConfigFlowResult:
        while self._queue:
            step = self._queue.pop(0)
            if self._page_enabled(step):
                return await getattr(self, f"async_step_{step}")()
        return await self._async_finish()

    async def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    def _placeholders(self) -> dict[str, str]:
        return {}

    def _notify_services(self) -> list[str]:
        services = self.hass.services.async_services().get("notify", {})  # type: ignore[attr-defined]
        return [f"notify.{name}" for name in services if name not in ("send_message", "persistent_notification")]

    async def _async_page(self, step: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        keys = FLOW_STEPS[step]
        errors: dict[str, str] = {}
        values_for_form = dict(self._options)
        if user_input is not None:
            values = normalize_input(keys, user_input)
            errors = validate_step(step, values)
            if not errors:
                self._options.update(values)
                return await self._async_next()
            values_for_form.update(values)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step,
            data_schema=build_schema(keys, values_for_form, self._notify_services() if step == "notifications" else None),
            errors=errors,
            description_placeholders={**self._placeholders(), "eds_url": ENERGIDATASERVICE_URL},
        )

    async def async_step_price_provider(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            providers = [p for p in user_input.get("price_providers", []) if p in PRICE_PROVIDERS]
            if not providers:
                errors["base"] = "no_price_provider"
            else:
                self._providers = providers
                for provider, keys in PRICE_PROVIDER_KEYS.items():
                    if provider not in providers:
                        for key in keys:
                            self._options[key] = ""
                return await self._async_next()
        schema = vol.Schema(
            {
                vol.Required("price_providers", default=self._providers): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=PRICE_PROVIDERS, multiple=True, mode=selector.SelectSelectorMode.LIST, translation_key="price_providers")
                )
            }
        )
        return self.async_show_form(step_id="price_provider", data_schema=schema, errors=errors, description_placeholders=self._placeholders())  # type: ignore[attr-defined]

    async def async_step_general(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("general", user_input)

    async def async_step_charger(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("charger", user_input)

    async def async_step_charger_specs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("charger_specs", user_input)

    async def async_step_load_balancing(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("load_balancing", user_input)

    async def async_step_ev(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("ev", user_input)

    async def async_step_ev_specs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("ev_specs", user_input)

    async def async_step_home(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("home", user_input)

    async def async_step_powerwall(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("powerwall", user_input)

    async def async_step_prices_energidataservice(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_energidataservice", user_input)

    async def async_step_prices_stromligning(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning", user_input)

    async def async_step_prices_stromligning_tariffs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning_tariffs", user_input)

    async def async_step_price_fees(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("price_fees", user_input)

    async def async_step_solar(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    async def async_step_solar_charging(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("solar_charging", user_input)

    async def async_step_weather(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("weather", user_input)

    async def async_step_notifications(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("notifications", user_input)

    async def async_step_advanced(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("advanced", user_input)

    async def async_step_database(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("database", user_input)


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


SITE_SELECTORS: dict[str, Any] = {
    "fuse_l1": _number(6, 400, 1, "A"),
    "fuse_l2": _number(6, 400, 1, "A"),
    "fuse_l3": _number(6, 400, 1, "A"),
    "margin": _number(0, 20, 0.5, "A"),
    "voltage": _number(100, 480, 1, "V"),
    "phase_l1_entity_id": _entity("sensor", device_class=["current", "power"]),
    "phase_l2_entity_id": _entity("sensor", device_class=["current", "power"]),
    "phase_l3_entity_id": _entity("sensor", device_class=["current", "power"]),
    "urgent_trip_hours": _number(0, 72, 1, "h"),
}


def site_schema(values: dict[str, Any]) -> vol.Schema:
    schema: dict[Any, Any] = {}
    for key in SITE_FIELDS:
        if key.endswith("_entity_id"):
            schema[vol.Optional(key, description={"suggested_value": values.get(key) or None})] = SITE_SELECTORS[key]
        else:
            schema[vol.Required(key, default=values.get(key, SITE_DEFAULTS[key]))] = SITE_SELECTORS[key]
    return vol.Schema(schema)


def site_options(user_input: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key in SITE_FIELDS:
        value = user_input.get(key)
        if key.endswith("_entity_id"):
            options[key] = value or ""
        elif key == "urgent_trip_hours":
            options[key] = int(float(value if value is not None else SITE_DEFAULTS[key]))
        else:
            options[key] = float(value if value is not None else SITE_DEFAULTS[key])
    return options


class CJPSiteOptionsFlow(OptionsFlow):
    """Change the shared grid connection."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_site()

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=site_options(user_input))
        return self.async_show_form(step_id="site", data_schema=site_schema({**SITE_DEFAULTS, **self.config_entry.options}))
