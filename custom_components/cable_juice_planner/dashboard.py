"""Generated sidebar dashboard: the bundled template mapped onto one instance.

The template (dashboard/cable_juice_planner.yaml) is written for the entities of the
integration with the prefix ``cjp`` and the author's car, charger, price and solar
entities. At setup it is rewritten for this config entry:

* own entities ``<platform>.cjp_*`` -> ``<platform>.cjp_<prefix>_*``
* external entities (charger, car, Energi Data Service, Strømligning, Solcast) -> the
  configured ones; extra car entities (frunk, sentry mode, ...) are derived from the
  name of the car's battery level entity
* cards bound to an entity that does not exist are removed, and a heading left
  without settings below it
* ``@@key@@`` placeholders -> ``dashboard.key`` of texts/<language>.json

The result is written to .storage and served as a Lovelace dashboard that can be edited in
the UI: once the user saves it, the saved copy is shown instead of the generated one until
it is reset (reset_dashboard button, or deleting the config in the raw configuration editor).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from homeassistant.components import frontend
from homeassistant.components.lovelace import dashboard as lovelace_dashboard
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import cached_json_fragment, json_fragment
from homeassistant.helpers.storage import Store

from .const import CONF_CAR_NAME, DOMAIN, ENTITY_ID_PREFIX, WEEKDAYS, is_ev_configured, normalize_language, normalize_prefix, object_prefix, sidebar_title
from .formatting import load_texts

_LOGGER = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "dashboard"
ICON = "mdi:ev-station"

_OWN_ENTITY = re.compile(r"\b(sensor|binary_sensor|switch|number|select|text|button|time|datetime|event)\.cjp_")
_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_PLACEHOLDER = re.compile(r"@@(\w+)@@")
_SOLCAST_DAYS = ("tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7")
_CAR = "mcqueen"
_CAR_EXTRAS = (
    "binary_sensor.mcqueen_doors_windows",
    "binary_sensor.mcqueen_status",
    "cover.mcqueen_frunk",
    "cover.mcqueen_trunk",
    "sensor.mcqueen_inside_temperature",
    "sensor.mcqueen_outside_temperature",
    "switch.mcqueen_sentry_mode",
)
_DROP = object()
PREHEAT_ENTITIES = (("switch", "trip_preheat"), ("number", "preheat_minutes_before"), *(("switch", f"preheat_{day}") for day in WEEKDAYS))


def dashboard_url(prefix: str) -> str:
    """Frontend url of the dashboard (Lovelace requires a '-' in it)."""
    prefix = normalize_prefix(prefix)
    return f"cjp-{prefix.replace('_', '-')}" if prefix else "cjp-planner"


def dashboard_file(hass: HomeAssistant, entry_id: str) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}.{entry_id}.dashboard.yaml"))


def custom_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The user's edited copy of the dashboard."""
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.dashboard_custom")


class CJPDashboard(lovelace_dashboard.LovelaceYAML):
    """The generated YAML file, but editable: the frontend only offers editing in storage mode."""

    def __init__(self, hass: HomeAssistant, url_path: str, config: dict[str, Any], store: Store[dict[str, Any]], custom: dict[str, Any] | None) -> None:
        super().__init__(hass, url_path, config)
        self._store = store
        self._custom = custom
        self._custom_json: json_fragment | None = None

    @property
    def mode(self) -> str:
        return MODE_STORAGE

    @property
    def customized(self) -> bool:
        return self._custom is not None

    async def async_get_info(self) -> dict[str, Any]:
        info = await super().async_get_info()
        return {**info, "mode": self.mode}

    async def async_load(self, force: bool) -> dict[str, Any]:
        if self._custom is not None:
            return self._custom
        return await super().async_load(force)

    async def async_json(self, force: bool) -> json_fragment:
        if self._custom is not None:
            if self._custom_json is None:
                self._custom_json = cached_json_fragment(self._custom)
            return self._custom_json
        return await super().async_json(force)

    async def async_save(self, config: dict[str, Any]) -> None:
        self._custom = config
        self._custom_json = None
        await self._store.async_save({"config": config})
        self._config_updated()

    async def async_delete(self) -> None:
        """Back to the generated dashboard."""
        self._custom = None
        self._custom_json = None
        await self._store.async_remove()
        self._config_updated()


def template_file(config: dict[str, Any]) -> Path:
    """One template for every configuration: the cards of a missing car/solar/... are removed."""
    return TEMPLATE_DIR / "cable_juice_planner.yaml"


def _derive(entity_id: str, old: str, new: str) -> str:
    return entity_id.replace(old, new) if old in entity_id else ""


def _car_base(config: dict[str, Any]) -> str:
    """'sensor.tesla_battery_level' -> 'tesla' (object id without the battery suffix)."""
    battery = config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
    object_id = battery.split(".", 1)[1] if "." in battery else ""
    for suffix in ("_usable_battery_level", "_battery_level", "_battery"):
        if object_id.endswith(suffix):
            return object_id[: -len(suffix)]
    return ""


def external_entities(config: dict[str, Any]) -> dict[str, str]:
    """Template entity id -> configured entity id ("" = not configured)."""
    prices = config["prices"]["entity_ids"]
    charger = config["charger"]["entity_ids"]
    ev = config["ev_car"]["entity_ids"]
    sl_buy = prices["stromligning_buy_current_price"]
    sl_sell = prices["stromligning_sell_current_price"]
    mapping = {
        "sensor.bil_lader_status": charger["status_entity_id"],
        "sensor.bil_lader_power": charger["power_consumtion_entity_id"],
        "sensor.bil_lader_2_status": charger["status_entity_id"],
        "sensor.bil_lader_2_power": charger["power_consumtion_entity_id"],
        "sensor.mcqueen_battery_level": ev["usable_battery_level_entity_id"],
        "sensor.mcqueen_odometer": ev["odometer_entity_id"],
        "binary_sensor.mcqueen_charge_cable": ev["charge_cable_entity_id"],
        "cover.mcqueen_charge_port_door": ev["charge_port_door_entity_id"],
        "device_tracker.mcqueen_location": ev["location_entity_id"],
        "climate.mcqueen_climate": ev["climate_entity_id"],
        "number.mcqueen_charge_limit": ev["charging_limit_entity_id"],
        "sensor.energi_data_service": prices["energidataservice_buy_entity_id"],
        "sensor.energi_data_service_salg": prices["energidataservice_sell_entity_id"],
        "sensor.stromligning_current_price_vat": sl_buy,
        "sensor.stromligning_forecasts_vat": prices["stromligning_buy_forecasts"],
        "binary_sensor.stromligning_tomorrow_available_vat": prices["stromligning_buy_tomorrow_available"],
        "sensor.stromligning_nettariff_vat": _derive(sl_buy, "current_price", "nettariff"),
        "sensor.stromligning_systemtariff_vat": _derive(sl_buy, "current_price", "systemtariff"),
        "sensor.stromligning_15min_spotprice_ex_vat": _derive(sl_sell, "current_price", "spotprice"),
        "sensor.stromligning_15min_forecasts_ex_vat": prices["stromligning_sell_forecasts"],
        "binary_sensor.stromligning_15min_tomorrow_available_ex_vat": prices["stromligning_sell_tomorrow_available"],
    }
    if sl_buy:
        # one price chart: Strømligning is preferred, the Energi Data Service chart is removed
        mapping["sensor.energi_data_service"] = ""
        mapping["sensor.energi_data_service_salg"] = ""
    base = _car_base(config)
    for entity_id in _CAR_EXTRAS:
        mapping[entity_id] = entity_id.replace(_CAR, base, 1) if base else ""
    # Solcast: forecast_today plus the per-day sensors next to it
    forecast = config["solar"]["entity_ids"]["forecast_entity_id"]
    solcast_base = forecast[: -len("today")] if forecast.endswith("_today") else ""
    mapping["sensor.solcast_pv_forecast_forecast_today"] = forecast
    for day in _SOLCAST_DAYS:
        mapping[f"sensor.solcast_pv_forecast_forecast_{day}"] = f"{solcast_base}{day}" if solcast_base else ""
    return mapping


def _own_missing(missing: set[str]) -> set[str]:
    """The missing entities of the integration (markdown texts are checked for these)."""
    return {entity_id for entity_id in missing if ".cjp_" in entity_id}


def _prune(node: Any, missing: set[str]) -> Any:
    """Remove cards/series/list items bound to a missing entity, and containers left empty."""
    if isinstance(node, list):
        items = []
        for item in node:
            if isinstance(item, str) and item in missing:
                continue
            item = _prune(item, missing)
            if item is not _DROP:
                items.append(item)
        return items
    if isinstance(node, dict):
        if node.get("entity") in missing:
            return _DROP
        # a text that shows an entity that does not exist (e.g. the car's analysis without a car)
        # (read more texts look for the help of several entities and show nothing when they are missing)
        content = str(node.get("content", ""))
        if node.get("type") == "markdown" and "@@read_more@@" not in content and any(entity_id in content for entity_id in _own_missing(missing)):
            return _DROP
        # (also visibility: without its entity the card must not end up always visible)
        if any(isinstance(cond, dict) and cond.get("entity") in missing for key in ("conditions", "visibility") for cond in node.get(key) or ()):
            return _DROP
        result = {}
        for key, value in node.items():
            pruned = _prune(value, missing)
            if pruned is _DROP:  # e.g. the single `card:` of a conditional
                return _DROP
            # "conditions": an and/or left without conditions would always be true
            if isinstance(value, list) and value and not pruned and key in ("cards", "series", "entities", "sections", "badges", "chips", "conditions"):
                return _DROP
            result[key] = pruned
        return result
    return node


def _drop_empty_headings(node: Any) -> Any:
    """In a section: a heading with nothing but read more texts or fixed explanations left below it is removed with them."""
    if isinstance(node, list):
        return [_drop_empty_headings(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _drop_empty_headings(value) for key, value in node.items()}
    cards = node.get("cards")
    if node.get("type") == "grid" and isinstance(cards, list) and any(card.get("type") == "heading" for card in cards):
        kept: list = []
        block: list = []
        for card in [*cards, {"type": "heading"}]:
            if card.get("type") == "heading":
                # only read more or fixed explanations left: nothing to show under the heading
                read_more_only = all(
                    c.get("type") == "markdown" and ("@@read_more@@" in str(c.get("content", "")) or "{" not in str(c.get("content", "")))
                    for c in block[1:]
                )
                if not block or block[0].get("type") != "heading" or not read_more_only:
                    kept.extend(block)
                block = [card]
            else:
                block.append(card)
        node["cards"] = kept
    return node


def _translate(node: Any, texts: dict[str, str]) -> Any:
    """Replace @@key@@ in every string with the dashboard text of the language (the key if missing)."""
    if isinstance(node, list):
        return [_translate(item, texts) for item in node]
    if isinstance(node, dict):
        return {key: _translate(value, texts) for key, value in node.items()}
    if isinstance(node, str) and "@@" in node:
        return _PLACEHOLDER.sub(lambda match: texts.get(f"dashboard.{match.group(1)}", match.group(1)), node)
    return node


def _referenced_entities(node: Any, found: set[str]) -> set[str]:
    """Entity ids used as `entity:` (cards, conditions) or as list items."""
    if isinstance(node, list):
        for item in node:
            if isinstance(item, str) and _ENTITY_ID.match(item):
                found.add(item)
            else:
                _referenced_entities(item, found)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == "entity" and isinstance(value, str) and _ENTITY_ID.match(value):
                found.add(value)
            else:
                _referenced_entities(value, found)
    return found


class _Dumper(yaml.SafeDumper):
    """Keep the JavaScript/Jinja templates readable: multi-line strings as literal blocks."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def _replace(text: str, replace: dict[str, str]) -> str:
    if not replace:
        return text
    pattern = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, sorted(replace, key=len, reverse=True))) + r")(?!\w)")
    return pattern.sub(lambda match: replace[match.group(1)], text)


def render_dashboard(config: dict[str, Any], prefix: str, entry_id: str, existing: set[str]) -> str:
    """Build the dashboard YAML for one instance (blocking file I/O).

    prefix: the user's prefix (entity ids cjp_<prefix>_*)
    existing: entity ids that exist in Home Assistant (incl. the own entities)
    """
    own = object_prefix(prefix)
    mapping = external_entities(config)
    text = template_file(config).read_text(encoding="utf-8").replace("__ENTRY_ID__", entry_id)
    replace = {source: target for source, target in mapping.items() if target and source != target}
    text = _replace(text, replace)
    if own != ENTITY_ID_PREFIX:
        text = _OWN_ENTITY.sub(lambda match: f"{match.group(1)}.{own}_", text)
    template = yaml.safe_load(text)
    unconfigured = {source for source, target in mapping.items() if not target}
    missing = {entity_id for entity_id in _referenced_entities(template, set()) if entity_id not in existing} | unconfigured
    if not (is_ev_configured(config) and config["ev_car"]["entity_ids"]["climate_entity_id"]):
        # preheating needs the car integration and its climate entity
        missing |= {f"{platform}.{own}_{key}" for platform, key in PREHEAT_ENTITIES}
    texts = load_texts(normalize_language(config.get("language")))
    template = _translate(_drop_empty_headings(_prune(template, missing)), texts)
    return yaml.dump(template, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=1000)


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


async def async_setup_dashboard(hass: HomeAssistant, entry: ConfigEntry, config: dict[str, Any], prefix: str) -> None:
    """Render the dashboard and add it to the sidebar until the entry unloads."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        _LOGGER.warning("Lovelace is not loaded, the Cable Juice Planner dashboard is not available")
        return
    url_path = dashboard_url(prefix)
    existing_dashboard = lovelace.dashboards.get(url_path)
    if existing_dashboard is not None and not isinstance(existing_dashboard, CJPDashboard):
        _LOGGER.warning("A dashboard already uses /%s, the Cable Juice Planner dashboard is not added", url_path)
        return

    path = dashboard_file(hass, entry.entry_id)
    existing = set(hass.states.async_entity_ids())
    existing.update(entity.entity_id for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))

    def _render() -> bool:
        return _write_if_changed(path, render_dashboard(config, prefix, entry.entry_id, existing))

    try:
        changed = await hass.async_add_executor_job(_render)
    except (OSError, yaml.YAMLError) as err:
        _LOGGER.error("Could not create the Cable Juice Planner dashboard: %s", err)
        return

    store = custom_store(hass, entry.entry_id)
    stored = await store.async_load()
    title = sidebar_title(prefix, entry.data.get(CONF_CAR_NAME))
    cjp_dashboard = CJPDashboard(
        hass,
        url_path,
        {"mode": MODE_STORAGE, "filename": str(path), "title": title, "icon": ICON, "show_in_sidebar": True, "require_admin": False},
        store,
        stored["config"] if stored else None,
    )
    lovelace.dashboards[url_path] = cjp_dashboard
    frontend.async_register_built_in_panel(
        hass,
        "lovelace",
        frontend_url_path=url_path,
        sidebar_title=title,
        sidebar_icon=ICON,
        require_admin=False,
        config={"mode": MODE_STORAGE},
        update=existing_dashboard is not None,
    )
    _LOGGER.info("Dashboard available at /%s%s", url_path, " (edited in the UI)" if cjp_dashboard.customized else " (updated)" if changed else "")

    @callback
    def _remove() -> None:
        if lovelace.dashboards.get(url_path) is cjp_dashboard:
            lovelace.dashboards.pop(url_path)
            frontend.async_remove_panel(hass, url_path)

    entry.async_on_unload(_remove)


async def async_reset_dashboard(hass: HomeAssistant, prefix: str) -> None:
    """Drop the user's edits: the generated dashboard is shown again."""
    lovelace = hass.data.get(LOVELACE_DATA)
    dashboard = lovelace.dashboards.get(dashboard_url(prefix)) if lovelace else None
    if isinstance(dashboard, CJPDashboard):
        await dashboard.async_delete()


async def async_remove_dashboard_file(hass: HomeAssistant, entry_id: str) -> None:
    await hass.async_add_executor_job(dashboard_file(hass, entry_id).unlink, True)
    await custom_store(hass, entry_id).async_remove()
