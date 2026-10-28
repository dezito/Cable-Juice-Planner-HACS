# Cable Juice Planner agent guide

Instructions for coding agents working in this repository. Follow the existing
implementation and project documentation when this guide does not answer a
question. Do not guess behavior that affects charging schedules, stored data,
or Home Assistant configuration.

## Project scope

Cable Juice Planner (CJP) is a Home Assistant custom integration installed
through HACS. It plans EV charging and exposes action entities; it does not
directly control a car or charger. Users connect the actions to their own
automations or the included blueprint.

- Integration code: `custom_components/cable_juice_planner/`
- Tests and shared fixtures: `tests/`
- User and behavior documentation: `docs/` and `README.md`
- Integration metadata: `custom_components/cable_juice_planner/manifest.json`

## Code map

```text
custom_components/cable_juice_planner/  The HA integration (HACS payload)
├── __init__.py                          Integration setup and platform forwarding
├── coordinator/                         Shared data, refresh, events, and lifecycle
├── config_flow/                         Setup/options flow and schemas
├── const/                               Configuration, settings, and site constants
├── engine/                              Charging plans, calculations, and current actions
│   ├── engine.py / planning.py          Calculation orchestration
│   ├── planner/                         Charge plan and price-level decisions
│   │   └── future/                      Departure, scheduled, solar, and unscheduled plans
│   ├── control/                         Current charger actions, schedule, and preheat
│   ├── energy/                          Prices, solar, forecasts, and power inputs
│   ├── history/                         Charging sessions, consumption, cost, and efficiency
│   ├── analysis/                        Economy/forecast analysis and published results
│   ├── site.py / battery.py / loss.py   Site, battery, and charging-loss models
│   └── public_charging.py / distance.py Public sessions and driving-distance calculations
├── prices/                              Price-source adapters and factory
├── management/                          Storage, backups, dashboard, and blueprint setup
├── utils/                               Shared codecs, formatting, and helpers
├── sensor.py / number.py / switch.py    Main HA entity platforms
├── select.py / button.py / binary_sensor.py  Other HA entity platforms
├── datetime.py / time.py / event.py / entity.py  Time/event platforms and shared entity base
├── repairs.py / site.py                 Repairs and site-related entities
├── translations/ texts/                 HA strings and user-facing descriptions
├── dashboard/ blueprints/               Bundled dashboard and automation blueprint
└── manifest.json                        HA integration metadata

tests/                                   Pytest suite
├── conftest.py                          Shared fixtures and simulated HA entities
├── test_engine.py / test_economy.py     Engine and cost/economy behavior
├── test_site.py / test_charging_loss_model.py  Site and charging-loss calculations
├── test_analysis.py / test_config_flow.py      Analysis and setup/options
├── test_init.py / test_backup.py / test_debug_export.py  Lifecycle and persistence
├── test_dashboard.py                    Dashboard generation and content
├── test_blueprint_control.py / test_blueprint_install.py  Charger actions and installation
└── debug_replay.py                      Debug/replay helper

docs/                                    User documentation and behavior details
.github/workflows/validate.yml           CI: hassfest and HACS validation
pytest.ini / requirements_test.txt       Pytest configuration and pinned test deps
```

Use this as a map, not an exhaustive inventory; check the current files before
editing. Prefer the narrowest relevant module and test file over changing a
broad layer for a local behavior change.

Read the relevant documentation before changing behavior. In particular:

- Planning inputs, schedule, and fail-safe behavior: `docs/how-it-works.md`
- Individual charging rules: `docs/rules/`
- Charger action entities and blueprint: `docs/charger-control.md`
- Shared charging and load constraints: `docs/load-balancing.md`
- Charging-loss model: `docs/charging-loss.md`
- Public charging sessions: `docs/public-charging.md`
- Backups and migration from PyScript: `docs/migration.md` and the backup
  section in `README.md`
- Setup and dashboard analysis: `docs/getting-started.md` and
  `docs/dashboard-analysis.md`

## Important behavior and compatibility

- Treat planner and economy changes as financially significant. Preserve the
  semantics of work plans, daily reserve, weekly cheap-hour charging, trips,
  solar surplus, home-battery interaction, price forecasts, charging losses,
  and driving efficiency. Keep published plans and reasons consistent with
  the actual actions.
- Preserve the distinction between planning and charger control. Do not add
  direct commands to vehicles or chargers, or assume a particular vehicle,
  charger, tariff, entity, or automation.
- Load balancing is safety-sensitive: preserve shared connection limits,
  priorities, and per-car charging constraints. Test interactions between
  cars and missing or unavailable inputs when changing this logic.
- Respect Home Assistant timestamps and configured time zones. Avoid naive
  datetime assumptions and test departure boundaries, price intervals,
  day/week transitions, and daylight-saving transitions when relevant.
- Preserve compatibility of config-entry options, entity IDs/unique IDs,
  stored history, backups, and import from the PyScript version. Changes to
  these formats or identifiers require a migration/compatibility path and
  documentation. Do not modify the PyScript source files as part of this
  integration unless explicitly requested.
- Data from Home Assistant entities, price providers, forecasts, and vehicle
  integrations can be missing, stale, unavailable, malformed, or non-numeric.
  Handle these cases explicitly and preserve the documented fail-safe behavior;
  do not silently invent prices, distances, battery levels, or forecasts.
- Keep network and blocking work out of the event loop. Follow the existing
  coordinator/executor lifecycle and ensure workers and resources are cleaned
  up on unload.
- Do not add telemetry, undisclosed external services, credentials, or
  undocumented network endpoints.

## Implementation conventions

- Follow the existing Home Assistant patterns in this integration for config
  entries, coordinators, entities, services, repairs, and lifecycle management.
- Keep changes focused. Prefer extending the existing module responsible for
  the behavior over adding duplicate logic or broad refactors.
- Add or update tests for behavior changes. Prefer deterministic tests using
  shared fixtures in `tests/conftest.py`; mock Home Assistant entities and
  external integrations rather than relying on a live installation, car, or
  charger.
- Test boundary conditions and failure paths as well as the expected path.
  For asynchronous lifecycle changes, verify setup, unload, and background
  task cleanup.
- Keep user-facing behavior and documentation aligned. Update README/docs when
  configuration, entities, planning behavior, charger actions, backup formats,
  or migration steps change.
- Whenever changing dashboard content or behavior, review and update each
  affected "Read more" description so it accurately covers the entities,
  controls, and functions shown beneath it. Check that the descriptions do not
  omit new dashboard content or retain claims about removed content.
- When a HA-dev environment is available, apply and verify dashboard changes
  there as well. For dashboard analysis changes, check that every test
  device/config entry has sufficient representative recorder/history data for
  the analysis entities and time ranges being tested; current entity states
  alone are not enough. If required history is missing, create or update
  dedicated test history and states in HA-dev so the analysis page can render
  and calculate its results. Do not alter live user history. Report any
  missing data that could not safely be added, or if HA-dev is unavailable.
- If changing any file under
  `custom_components/cable_juice_planner/texts/` or
  `custom_components/cable_juice_planner/translations/`, update every
  supported language (`da`, `de`, `en`, `en-GB`, `pl`, and `uk`)
  consistently. Check that keys and placeholders match across languages; do
  not update only Danish or English.
- Do not edit generated/cache files such as `__pycache__`, `.pytest_cache`, or
  `.ruff_cache`.

## Tests and validation

The test configuration is in `pytest.ini`; test dependencies are pinned in
`requirements_test.txt`. Those dependencies target the Home Assistant
2026.10.x / Python 3.14 environment noted in that file.

From the repository root, run the focused test file(s) first, then the full
suite when practical:

```bash
python -m pytest tests/test_<area>.py
python -m pytest
```

If test dependencies are not installed, install the repository's declared
test requirements in the active environment:

```bash
python -m pip install -r requirements_test.txt
```

The GitHub validation workflow runs Home Assistant `hassfest` and the HACS
integration validation action. Run the relevant checks when available for
changes to integration metadata, translations, or HACS structure. Do not claim
a check passed unless it was actually run. No repository Ruff configuration
or Ruff CI check is defined; follow the existing style rather than introducing
an unconfigured formatter or automatic rewrite.

## Before finishing a change

1. Review the diff and confirm it contains only intended changes.
2. Run the focused tests; run the full pytest suite for shared/core behavior
   changes when practical.
3. Run relevant Home Assistant/HACS validation for metadata or packaging
   changes, if available.
4. Check translations, documentation, and compatibility requirements above.
5. Report which checks ran and their results, and disclose any validation that
   could not be run.
