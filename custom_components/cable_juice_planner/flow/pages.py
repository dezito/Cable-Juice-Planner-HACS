"""Pages shared by the config flow and the options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from ..const import CIRCUIT_ENTITY_KEYS, CONF_CIRCUIT, CONF_CIRCUITS, CONF_ENTITY_PREFIX, CONF_SITE, DOMAIN, FLOW_STEPS, PRICE_PROVIDER_KEYS, object_prefix
from ..site import charger_devices, circuit_of, update_circuits
from .schema import ENERGIDATASERVICE_URL, PRICE_PROVIDERS, build_schema, normalize_input, validate_step


class CJPFlowPages:
    """Pages shared by the config flow and the options flow.

    The subclass sets ``self._options`` (flat option dict), ``self._queue`` (remaining
    page ids) and implements ``_async_finish``.
    """

    _options: dict[str, Any]
    _queue: list[str]
    _providers: list[str]
    # chargers on the car's circuit from the load balancing page, saved in the site entry on finish
    _circuit: list[str] | None = None

    def _site_exists(self) -> bool:
        return self._site_entry() is not None

    def _site_entry(self) -> ConfigEntry | None:
        return next((entry for entry in self.hass.config_entries.async_entries(DOMAIN) if entry.data.get(CONF_SITE)), None)  # type: ignore[attr-defined]

    def _this_entry_id(self) -> str | None:
        """The entry being changed (None while a new car is set up)."""
        return None

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
            data_schema=build_schema(
                keys,
                values_for_form,
                self._notify_services() if step == "notifications" else None,
                self.hass.config.currency,  # type: ignore[attr-defined]
            ),
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
        """Priority, phase order and the chargers sharing the car's circuit."""
        keys = FLOW_STEPS["load_balancing"]
        own = self._own_charger_devices()
        site = self._site_entry()
        current = self._circuit if self._circuit is not None else circuit_of(site.options.get(CONF_CIRCUITS) if site else [], own)
        errors: dict[str, str] = {}
        values_for_form = dict(self._options)
        if user_input is not None:
            values = normalize_input(keys, user_input)
            selected = [device for device in user_input.get(CONF_CIRCUIT) or [] if device]
            if selected and not own:
                errors[CONF_CIRCUIT] = "circuit_no_charger_device"
            elif selected and not own & set(selected):
                errors[CONF_CIRCUIT] = "circuit_own_charger_missing"
            if not errors:
                self._options.update(values)
                self._circuit = selected
                return await self._async_next()
            values_for_form.update(values)
            current = selected
        schema = build_schema(keys, values_for_form).extend(
            {vol.Optional(CONF_CIRCUIT, description={"suggested_value": current}): self._circuit_selector()}
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="load_balancing",
            data_schema=schema,
            errors=errors,
            description_placeholders={**self._placeholders(), "circuit": self._circuit_text(current, own)},
        )

    def _own_charger_devices(self) -> set[str]:
        return charger_devices(self.hass, [self._options.get(key) for key in CIRCUIT_ENTITY_KEYS])  # type: ignore[attr-defined]

    def _circuit_selector(self) -> selector.DeviceSelector:
        """Devices of the integrations the car's charger entities come from (all without)."""
        ent_reg = er.async_get(self.hass)  # type: ignore[attr-defined]
        integrations = []
        for key in CIRCUIT_ENTITY_KEYS:
            entity_id = self._options.get(key)
            entry = ent_reg.async_get(entity_id) if entity_id else None
            if entry is not None and entry.device_id and entry.platform not in integrations:
                integrations.append(entry.platform)
        config: dict[str, Any] = {"multiple": True}
        if integrations:
            config["filter"] = [{"integration": integration} for integration in integrations]
        return selector.DeviceSelector(selector.DeviceSelectorConfig(**config))

    def _circuit_text(self, devices: list[str], own: set[str]) -> str:
        """Each charger of the circuit and the cars using it (shown in the page description)."""
        if not devices:
            return "–"
        users: dict[str, list[str]] = {device: [] for device in devices}
        this_entry = self._this_entry_id()
        for device in own:
            if device in users:
                users[device].append(object_prefix(self._placeholders().get("prefix")))
        for entry in self.hass.config_entries.async_entries(DOMAIN):  # type: ignore[attr-defined]
            if entry.data.get(CONF_SITE) or entry.entry_id == this_entry:
                continue
            for device in charger_devices(self.hass, [entry.options.get(key) for key in CIRCUIT_ENTITY_KEYS]):  # type: ignore[attr-defined]
                if device in users:
                    users[device].append(object_prefix(entry.data.get(CONF_ENTITY_PREFIX)))
        dev_reg = dr.async_get(self.hass)  # type: ignore[attr-defined]
        lines = []
        for device, prefixes in users.items():
            info = dev_reg.async_get(device)
            name = (info.name_by_user or info.name) if info is not None else device
            lines.append(f"- {name}: {', '.join(prefixes) or '–'}")
        return "\n".join(lines)

    def _save_circuit(self) -> None:
        """The circuit is kept once in the site entry, so a change from one car applies to all of it."""
        site = self._site_entry()
        if self._circuit is None or site is None:
            return
        old = site.options.get(CONF_CIRCUITS) or []
        circuits = update_circuits(old, self._own_charger_devices(), self._circuit)
        if circuits != old:
            self.hass.config_entries.async_update_entry(site, options={**site.options, CONF_CIRCUITS: circuits})  # type: ignore[attr-defined]

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
