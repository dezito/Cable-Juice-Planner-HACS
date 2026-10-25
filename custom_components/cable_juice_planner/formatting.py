"""Emoji and text formatting helpers shared by the engine modules."""
from __future__ import annotations

import json
from pathlib import Path

from .const import CHARGING_TYPES, currency_symbol

_EMOJI_PRIORITY = {entry["emoji"]: float(entry["priority"]) for entry in CHARGING_TYPES.values()}


def emoji_sorting(text: str) -> str:
    emoji_sorted = {}
    for emoji in text.split():
        if emoji in _EMOJI_PRIORITY:
            emoji_sorted[_EMOJI_PRIORITY[emoji]] = emoji
    return " ".join(emoji for _, emoji in sorted(emoji_sorted.items()))


def emoji_parse(data: dict) -> str:
    emojis = [CHARGING_TYPES[key]["emoji"] for key in data if data[key] is True and key in CHARGING_TYPES]
    return emoji_sorting(" ".join(emojis))


def join_unique_emojis(str1: str, str2: str) -> str:
    if str1 and str2:
        return " ".join(set(str1.split()).union(set(str2.split())))
    return str1 or str2


def emoji_text_format(text: str, group_size: int = 3) -> str:
    words = text.split()
    if len(words) <= group_size:
        return "".join(words)
    return "<br>".join("".join(words[i:i + group_size]) for i in range(0, len(words), group_size))


def emoji_update_local_energy(emojis="", kwh_from_local_energy=0.0, solar_kwh_of_local_energy=0.0, powerwall_kwh_of_local_energy=0.0) -> str:
    solar = emoji_parse({"solar": True})
    powerwall = emoji_parse({"powerwall": True})
    if solar_kwh_of_local_energy > 0.0:
        emojis = join_unique_emojis(emojis, solar)
    elif len(emojis.split(" ")) > 1 and solar_kwh_of_local_energy <= 0.0:
        emojis_set = set(emojis.split(" "))
        emojis_set.discard(solar)
        emojis = " ".join(emojis_set)
    if powerwall_kwh_of_local_energy > 0.0:
        emojis = join_unique_emojis(emojis, powerwall)
    elif len(emojis.split(" ")) > 1 and powerwall_kwh_of_local_energy <= 0.0:
        emojis_set = set(emojis.split(" "))
        emojis_set.discard(powerwall)
        emojis = " ".join(emojis_set)
    return emojis


def get_list_values(data) -> list:
    """Floats of a database list ([float, ...] or [[timestamp, float], ...])."""
    float_list = []
    for item in data:
        if isinstance(item, float):
            float_list.append(item)
        elif isinstance(item, (list, tuple)):
            if isinstance(item[1], float):
                float_list.append(item[1])
    return float_list


def format_charging_rule(text: str, testing: bool, extra: str = "") -> str:
    """🧪 around every line in testing mode, plus extra status lines (🚧 ...)."""
    marker = "🧪" if testing else ""
    text = "\n".join(f"{marker}{line.strip()}{marker}" for line in str(text).split("\n") if isinstance(line, str) and line.strip())
    return f"{text}{extra}"


def translate(texts: dict[str, str], key: str, default: str | None = None, **fmt) -> str:
    """Runtime text from texts/<language>.json (flattened keys, see load_texts).

    Placeholders use str.format, so a literal { or } is written as {{ or }} in the json.
    """
    value = texts.get(key, default if default is not None else key)
    try:
        return value.format(**fmt) if fmt else value
    except (KeyError, IndexError, ValueError):
        return value


def load_texts(language: str, currency: str | None = None, distance: str | None = None) -> dict[str, str]:
    """Flattened runtime texts of texts/<language>.json, English is the fallback for missing keys.

    currency: hass.config.currency; replaces the language's currency in ui.common.valuta*.
    distance: hass.config.units.length_unit ("km"/"mi", the unit the car's sensors are shown in);
    replaces the language's distance unit in ui.common.* when the language uses the other one.
    {valuta}, {valuta_kwh}, {distance} and {distance_kwh} are replaced in every text (also
    @@valuta@@, @@valuta_kwh@@, @@distance@@ and @@distance_kwh@@ in the dashboard).
    Blocking file I/O, run it in the executor.
    """
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    folder = Path(__file__).parent / "texts"
    for name in dict.fromkeys(("en", language)):
        path = folder / f"{name}.json"
        if path.is_file():
            flatten(json.loads(path.read_text(encoding="utf-8")))

    if symbol := currency_symbol(currency):
        for key in [key for key in texts if key.startswith("ui.common.valuta")]:
            unit = texts[key]
            texts[key] = symbol + unit[unit.find("/"):] if "/" in unit else symbol
    # en.json is written for miles, the other languages for km (uk: км)
    if distance in ("km", "mi") and (texts.get("ui.common.distance_type") == "mi") != (distance == "mi"):
        texts["ui.common.distance_type"] = distance
        texts["ui.common.distance_kwh"] = f"{distance}/kWh"
        texts["ui.common.distance_percentage"] = f"{distance}/%"
        texts["ui.common.wh_distance"] = f"Wh/{distance}"
        texts["ui.common.valuta_distance"] = f"{texts.get('ui.common.valuta', 'kr')}/{distance}"
    units = {
        "valuta": texts.get("ui.common.valuta", "kr"),
        "valuta_kwh": texts.get("ui.common.valuta_kwh", "kr/kWh"),
        "distance": texts.get("ui.common.distance_type", "km"),
        "distance_kwh": texts.get("ui.common.distance_kwh", "km/kWh"),
    }
    for key, value in texts.items():
        if isinstance(value, str) and ("{valuta" in value or "{distance" in value):
            for token, replacement in units.items():
                value = value.replace(f"{{{token}}}", replacement)
            texts[key] = value
    for token, replacement in units.items():
        texts[f"dashboard.{token}"] = replacement
    return texts
