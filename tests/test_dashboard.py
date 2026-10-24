"""The generated dashboard: entities mapped to the instance, missing cards removed, texts translated."""
from __future__ import annotations

import yaml

from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.dashboard import render_dashboard

from .conftest import MOCK_OPTIONS, OBJECT_PREFIX, async_setup, async_unload_and_join, make_entry, set_source_states
from .test_engine import OPTIONS


def _own(prefix: str) -> set[str]:
    own = const.object_prefix(prefix)
    ids = {f"switch.{own}_{key}" for key, info in const.SWITCH_SETTINGS.items() if info.get("requires") in (None, "ev")}
    ids |= {f"number.{own}_{key}" for key, info in const.NUMBER_SETTINGS.items() if info.get("requires") in (None, "ev")}
    ids |= {f"sensor.{own}_{key}" for key, info in const.SENSOR_DEFS.items() if info[3] in (None, "ev")}
    ids |= {f"time.{own}_{key}" for key in const.TIME_SETTINGS} | {f"datetime.{own}_{key}" for key in const.DATETIME_SETTINGS}
    ids |= {f"select.{own}_{key}" for key in const.SELECT_SETTINGS} | {f"button.{own}_{key}" for key in (*const.BUTTON_KEYS, *const.EV_BUTTON_KEYS)}
    return ids


def _cards(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _cards(value)
    elif isinstance(node, list):
        for item in node:
            yield from _cards(item)


def test_dashboard_for_a_car_without_solar() -> None:
    config = const.build_config({**OPTIONS, "language": "da"})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.price"}
    text = render_dashboard(config, "tesla", "entry1", existing)
    assert "@@" not in text
    assert "cjp_tesla_fill_up" in text and "switch.cjp_fill_up" not in text
    assert "sensor.ev_battery_level" in text
    dashboard = yaml.safe_load(text)
    assert [view["title"] for view in dashboard["views"]] == ["Oversigt", "Indstillinger", "Analyse"]
    headings = [card["heading"] for card in _cards(dashboard) if card.get("type") == "heading"]
    # no solar, no virtual car: their sections are gone with their headings
    assert "Sol og husbatteri" not in headings and "Virtuel elbil" not in headings
    assert "Arbejdsplan" in headings and "Billig strøm" in headings
    # the overview markdown is shown as the integration writes it
    contents = [card.get("content") for card in _cards(dashboard) if card.get("type") == "markdown"]
    assert "{{ state_attr('sensor.cjp_tesla_overview', 'overview') }}" in contents
    assert "config_entry_id: entry1" in text


def test_dashboard_layout() -> None:
    config = const.build_config({**OPTIONS, "language": "da"})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "binary_sensor.cjp_tesla_public_charging_session_done"}
    overview, settings, _ = yaml.safe_load(render_dashboard(config, "tesla", "entry1", existing))["views"]
    headings = lambda view: [card["heading"] for card in _cards(view) if card.get("type") == "heading"]  # noqa: E731
    # the charging history above the overview, like the old dashboard
    contents = [card.get("content") for card in _cards(overview) if card.get("type") == "markdown"]
    history = contents.index("{{ state_attr('sensor.cjp_tesla_charging_history', 'history') }}")
    assert contents[history + 1] == "{{ state_attr('sensor.cjp_tesla_overview', 'overview') }}"
    # no rule twice in the settings: the modes with their own section are only there
    settings_ids = [row["entity"] for card in _cards(settings) if card.get("type") == "entities" for row in card["entities"] if "entity" in row]
    # manual and forced charging are only on the overview
    assert not {"switch.cjp_tesla_allow_manual_charging_now", "switch.cjp_tesla_forced_charging_daily_battery_level"} & set(settings_ids)
    assert settings_ids.count("switch.cjp_tesla_workplan_charging") == 1
    assert settings_ids.count("switch.cjp_tesla_fill_up") == 1
    # manual charging also on the overview, with a description
    overview_ids = [row["entity"] for card in _cards(overview) if card.get("type") == "entities" for row in card["entities"] if "entity" in row]
    assert {"switch.cjp_tesla_allow_manual_charging_now", "switch.cjp_tesla_allow_manual_charging_solar"} <= set(overview_ids)
    assert "Manuel ladning" in headings(overview)
    # manual charging below the trip, before the emoji description
    trip_section = next(section for section in overview["sections"] if "Manuel ladning" in headings(section))
    assert headings(trip_section)[0] == "Tur"
    assert "emoji_description" in trip_section["cards"][-1].get("content", "")
    # the battery chart: the planned km per day, not the battery level history
    chart = next(card for card in _cards(overview) if card.get("type") == "custom:apexcharts-card")
    assert [series.get("yaxis_id") for series in chart["series"]] == ["distance", "level", "charge"]
    # public charging only while a session waits for its cost
    conditional = next(card for card in _cards(overview) if card.get("type") == "conditional" and "Offentlig ladning" in headings(card))
    assert conditional["conditions"] == [{"condition": "state", "entity": "binary_sensor.cjp_tesla_public_charging_session_done", "state": "on"}]
    # the queue: how many are waiting and which session the cost is for
    queue = next(card["content"] for card in _cards(conditional) if card.get("type") == "markdown" and "public_charging_session'" in card.get("content", ""))
    assert "sessions | length > 1" in queue and "sessions[0].start_battery_level" in queue
    assert "@@" not in queue


def test_preheat_only_with_a_climate_entity() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "climate.tesla_climate"}
    without = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "preheat" not in without
    with_climate = render_dashboard(const.build_config({**OPTIONS, "language": "da", "climate_entity_id": "climate.tesla_climate"}), "tesla", "entry1", existing)
    for entity_id in ("switch.cjp_tesla_trip_preheat", "switch.cjp_tesla_preheat_monday", "number.cjp_tesla_preheat_minutes_before"):
        assert entity_id in with_climate


def test_forced_charging_shown_when_relevant() -> None:
    """Only while the battery level is under the daily minimum."""
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    overview = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing))["views"][0]
    card = next(c for c in _cards(overview) if c.get("type") == "conditional" and "forced_charging_daily_battery_level" in yaml.dump(c["card"]))
    branches = card["conditions"][0]["conditions"]
    # the car's battery level; the virtual car's branch is removed with its entity (an empty "and" would always be true)
    assert len(branches) == 1
    assert [c["entity"] for c in branches[0]["conditions"]] == ["sensor.ev_battery_level"] * 2
    assert branches[0]["conditions"][0]["below"] == "number.cjp_tesla_min_daily_battery_level"


def _price_charts(options: dict) -> list[str]:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.eds", "sensor.sl_price"}
    overview = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "da", **options}), "tesla", "entry1", existing))["views"][0]
    return [s["entity"] for c in _cards(overview) if c.get("type") == "custom:apexcharts-card" for s in c["series"][:1] if s.get("yaxis_id") == "price"]


def test_one_price_chart_stromligning_preferred() -> None:
    assert _price_charts({"energidataservice_buy_entity_id": "sensor.eds"}) == ["sensor.eds"]
    both = _price_charts({"energidataservice_buy_entity_id": "sensor.eds", "stromligning_buy_current_price": "sensor.sl_price"})
    assert both == ["sensor.sl_price"]


def test_charging_loss_section_on_analysis() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "@@" not in text
    analysis = yaml.safe_load(text)["views"][2]
    assert "Ladetab pr. effekt" in [c["heading"] for c in _cards(analysis) if c.get("type") == "heading"]
    charts = [c for c in _cards(analysis) if c.get("type") == "custom:apexcharts-card" and c.get("graph_span") == "12h"]
    assert len(charts) == 2 and all(s["entity"] == "sensor.cjp_tesla_charging_loss_model" for c in charts for s in c["series"])
    buttons = [row["entity"] for c in _cards(analysis) if c.get("type") == "entities" for row in c["entities"]]
    assert "button.cjp_tesla_recalculate_charging_loss" in buttons
    # without a car integration there is nothing to measure: the section is gone
    no_ev = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "da", "usable_battery_level_entity_id": ""}), "tesla", "entry1", _own("tesla") - {"sensor.cjp_tesla_charging_loss_model", "button.cjp_tesla_recalculate_charging_loss", "switch.cjp_tesla_use_measured_charging_loss"}))
    assert "Ladetab pr. effekt" not in [c.get("heading") for c in _cards(no_ev["views"][2])]


def test_economy_section_on_analysis() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "@@" not in text
    analysis = yaml.safe_load(text)["views"][2]
    assert [c["heading"] for c in _cards(analysis) if c.get("type") == "heading"][0] == "Økonomi"
    charts = [c for c in _cards(analysis) if c.get("type") == "custom:apexcharts-card" and c["series"][0]["entity"] == "sensor.cjp_tesla_economy"]
    assert len(charts) == 2


def test_analysis_sections() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "@@" not in text
    headings = [c["heading"] for c in _cards(yaml.safe_load(text)["views"][2]) if c.get("type") == "heading"]
    for heading in ("Kørsel og temperatur", "Klar til afgang", "Planlagt mod faktisk ladet", "Batterikapacitet"):
        assert heading in headings


async def test_sidebar_dashboard_can_be_edited_and_reset(hass: HomeAssistant, hass_ws_client, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "dashboard": True})
    await async_setup(hass, entry)
    client = await hass_ws_client(hass)

    async def command(message: dict) -> dict:
        await client.send_json_auto_id({"url_path": "cjp-test", **message})
        response = await client.receive_json()
        assert response["success"], response
        return response["result"]

    generated = await command({"type": "lovelace/config"})
    assert generated["views"]
    # storage mode: the frontend offers the edit UI and the raw configuration editor
    panel = hass.data["frontend_panels"]["cjp-test"]
    assert panel.config == {"mode": "storage"}

    await command({"type": "lovelace/config/save", "config": {"views": [{"title": "Mine"}]}})
    assert await command({"type": "lovelace/config"}) == {"views": [{"title": "Mine"}]}

    # the edits survive a reload, the generated dashboard is not shown over them
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await command({"type": "lovelace/config"}) == {"views": [{"title": "Mine"}]}

    await hass.services.async_call("button", "press", {"entity_id": f"button.{OBJECT_PREFIX}_reset_dashboard"}, blocking=True)
    assert await command({"type": "lovelace/config"}) == generated
    await async_unload_and_join(hass, entry)
