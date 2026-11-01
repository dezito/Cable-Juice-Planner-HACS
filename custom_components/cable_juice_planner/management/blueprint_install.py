"""Install the bundled automation blueprints into config/blueprints/automation/<domain>/.

Like Home Assistant's own blueprints they are copied at startup when they are missing,
so they show up in the blueprint list without importing a URL. An existing file is never
overwritten on its own: when it differs from the bundled version (a HACS update or the
user's own changes) a repair issue lets the user replace it. The files are compared the
way Home Assistant writes a blueprint (validated and dumped again), so re-importing it
from source_url is not a change.

Inputs that changed from one entity to several are rewritten as lists in the automations
that use the blueprint (automations.yaml, written like the automation editor does) once the
installed file is the bundled version: the editor shows a single entity id in a multiple
entity selector as empty.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, Blueprint
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import yaml as yaml_util
from homeassistant.util.file import write_utf8_file_atomic

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SOURCE = Path(__file__).parent.parent / "blueprints"
ISSUE_PREFIX = "blueprint_update_"
# inputs that took one entity in earlier versions and take several now
LIST_INPUTS = {"charger_control.yaml": ("charger_status", "power_sensor")}


def _normalized(text: str) -> str:
    """The blueprint as Home Assistant saves it when it is re-imported."""
    try:
        data = yaml_util.parse_yaml(text)
        return Blueprint(data, expected_domain="automation", schema=BLUEPRINT_SCHEMA).yaml()
    except HomeAssistantError:
        return text


def _target(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("blueprints", "automation", DOMAIN))


def _issue_id(name: str) -> str:
    return f"{ISSUE_PREFIX}{Path(name).stem}"


def _migrate_automations(hass: HomeAssistant, names: list[str]) -> bool:
    """Turn a single entity id in a LIST_INPUTS input into a list. True when the file changed."""
    inputs_by_path = {f"{DOMAIN}/{name}": LIST_INPUTS[name] for name in names if name in LIST_INPUTS}
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    if not inputs_by_path or not os.path.isfile(path):
        return False
    try:
        automations = yaml_util.load_yaml(path)
    except HomeAssistantError as err:
        # the blueprint itself is still installed; the editor shows the old inputs as empty
        _LOGGER.warning("Could not read %s to update the blueprint inputs: %s", path, err)
        return False
    if not isinstance(automations, list):
        return False
    changed = False
    for automation in automations:
        blueprint = automation.get("use_blueprint") if isinstance(automation, dict) else None
        if not isinstance(blueprint, dict) or not isinstance(blueprint.get("input"), dict):
            continue
        inputs = blueprint["input"]
        for key in inputs_by_path.get(blueprint.get("path"), ()):
            if isinstance(inputs.get(key), str):
                inputs[key] = [inputs[key]] if inputs[key] else []
                changed = True
    if changed:
        write_utf8_file_atomic(path, yaml_util.dump(automations))
        _LOGGER.info("Updated the inputs of the automations using %s in %s", ", ".join(inputs_by_path), path)
    return changed


async def _async_reload(hass: HomeAssistant) -> None:
    # the blueprint integration caches the blueprints it has read, and automations that are
    # already set up still use the old version - reload them like Home Assistant does after
    # a re-import
    domain_blueprints = hass.data.get("blueprint", {}).get("automation")
    if domain_blueprints is not None:
        await domain_blueprints.async_reset_cache()
        if hass.services.has_service("automation", "reload"):
            await hass.services.async_call("automation", "reload", blocking=False)


async def async_install_blueprints(hass: HomeAssistant) -> None:
    target = _target(hass)

    def _install() -> tuple[list[str], list[str], dict[str, str], bool]:
        sources = sorted(SOURCE.glob("*.yaml"))
        installed: list[str] = []
        outdated: dict[str, str] = {}
        for source in sources:
            content = source.read_text(encoding="utf-8")
            destination = target / source.name
            if not destination.exists():
                target.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                installed.append(source.name)
                _LOGGER.info("Installed blueprint %s", destination)
            elif _normalized(destination.read_text(encoding="utf-8")) != _normalized(content):
                outdated[source.name] = yaml_util.parse_yaml(content)["blueprint"]["name"]
        names = [source.name for source in sources]
        # the installed files are the bundled version (also after a re-import from source_url)
        migrated = _migrate_automations(hass, [name for name in names if name not in outdated])
        return names, installed, outdated, migrated

    # earlier versions kept the digests of the installed files here
    await Store(hass, 1, f"{DOMAIN}.blueprints").async_remove()
    try:
        # the folder is listed in the executor too: scandir blocks the event loop
        names, installed, outdated, migrated = await hass.async_add_executor_job(_install)
    except (OSError, HomeAssistantError) as err:
        _LOGGER.error("Could not install the Cable Juice Planner blueprints: %s", err)
        return
    for name in names:
        if name not in outdated:
            ir.async_delete_issue(hass, DOMAIN, _issue_id(name))
    for name, title in outdated.items():
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(name),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="blueprint_update",
            translation_placeholders={"name": title, "path": f"blueprints/automation/{DOMAIN}/{name}"},
            data={"file": name},
        )
    if installed or migrated:
        await _async_reload(hass)


async def async_update_blueprint(hass: HomeAssistant, name: str) -> None:
    """Replace the installed blueprint with the bundled version (repair fix flow)."""
    source = SOURCE / name
    destination = _target(hass) / name

    def _update() -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        _migrate_automations(hass, [name])

    await hass.async_add_executor_job(_update)
    _LOGGER.info("Updated blueprint %s", destination)
    await _async_reload(hass)
