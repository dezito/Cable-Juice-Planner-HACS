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
├── config_flow.py / flow/               Setup/options flow, pages, and schemas
├── const/                               Configuration, settings, and site constants
├── engine/                              Charging plans, calculations, and current actions
│   ├── engine.py / planning.py          Calculation orchestration
│   ├── planner/                         Charge plan and price-level decisions
│   │   └── future/                      Departure, scheduled, solar, and unscheduled plans
│   ├── control/                         Current charger actions, schedule, triggers, preheat, and charging-loss measurement
│   ├── energy/                          Prices, solar, forecasts, and power inputs
│   ├── history/                         Charging sessions, consumption, cost, and efficiency
│   ├── analysis/                        Economy/forecast analysis and published results
│   ├── economy.py                       Monthly charging economy comparison
│   ├── pricing.py / state.py            Price helpers (incl. `sell_value()`) and engine state
│   ├── debug.py                         Engine debug information
│   ├── site.py / battery.py / loss.py   Site, battery, and charging-loss models
│   └── public_charging.py / distance.py Public sessions and driving-distance calculations
├── prices/                              Price-source adapters and factory
├── management/                          Storage, backups, dashboard, and blueprint setup
├── utils/                               Shared utilities and datetime/time platform implementations
├── sensor.py / number.py / switch.py    Main HA entity platforms
├── select.py / button.py / binary_sensor.py  Other HA entity platforms
├── datetime.py / time.py / event.py / entity.py  Platform entry points, event platform, and shared entity base
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
├── fixtures/                            Legacy PyScript config for the import tests
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
- Learned driving distance: `docs/learn-distance.md`
- Backups and migration from PyScript: `docs/migration.md` and the backup
  section in `README.md`
- Setup and dashboard analysis: `docs/getting-started.md` and
  `docs/dashboard-analysis.md`
- Examples, troubleshooting, and emoji meanings: `docs/cookbook.md`,
  `docs/faq-troubleshooting.md`, and `docs/emojis.md`

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
- After every new, changed, or removed feature, review and update the
  README, the relevant `docs/` pages, the automated tests, and the HA-dev
  seed, simulator, and other test data before the task is done. Remove
  outdated references and test data, and check that documentation and test
  data describe the actual behavior.
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

## Calculations and economic estimates

These rules apply to code, tests, documentation, and the dashboard.

- Follow data and calculations from input to the displayed result; check
  units, timestamps, missing data, fees, losses, and boundary conditions.
- Do not let missing history or unknown prices turn into favourable default
  values. Reject the calculation or use a documented, conservative value.
- Clearly distinguish historical hindsight from a realistic forecast. Do not
  present an optimistic backtest result as expected savings, earnings, or
  payback time.
- Add regression tests for bugs found and for scenarios with missing or
  incomplete data.
- Use Home Assistant data for validation when an environment is available and
  access is approved. Keep credentials and sensitive data out of code, logs,
  and reports.

## Project notes

Agents record lasting decisions and findings for this repository here (not in
private agent memory). Update or remove a note when it stops being true.

- Built like BattMind-Integration; keep the worker-thread design so calculations
  never block HA: single-thread executor, snapshot -> job result.
- `input_boolean.dezito_show_debug_button` in the generated dashboard is the
  user's own toggle for the debug-info badge. It is intentional; do not flag it.
- README/docs audience: first-time installs, possibly migrating from PyScript. The HACS integration is new, so no release notes or "changed since" text
  about removed/renamed entities between development versions. Detailed docs
  live in `docs/` (English); the README is a short landing page that links into
  it. Anchor links to emoji headings need a leading hyphen.

### User decisions

- Default prefix `cjp`. Settings changes -> 5 s debounce + forced
  recalculation (like BattMind). Notification dedupe clears after 1 hour.
- No commands to the car/charger: publish action entities (charger_action
  charge/stop, phase amps, charging_limit, event entity for wake_up/preheat).
- `power_values_to_db` / `load_power_values_db` only need a configured,
  available house consumption entity, not solar+Solcast (deliberate).
- A fixed solar sell price (`production_price` != -1) is a settlement: a sold
  kWh is worth fixed price − the hour's raw sell price, can be negative. Use
  `sell_value()` (`engine/pricing.py`); keep price data and DBs raw.
- "No charging mode active" charges at max by design (fallback).

### Load balancing (site)

- A site config entry (`data site=True`, `site.py` `CJPSite` in
  `hass.data[DOMAIN]["site"]`); engine side in `engine/site.py`
  (`site_clamp` in the phase-limit step, hour kWh capacity hooks in the planner).
- User choices: fixed budget or fuse minus phase meters; fixed priority, an
  urgent car (under daily min / trip soon) overtakes; lower priority gets fewer
  amps, else other hours; phase order per charger.
- `site.update_from_result` recalculates all cars incl. the one that turned
  urgent; a changed live request recalculates the cars behind only when
  something changed (no ping-pong). Covered by `tests/test_site.py`.
- Shared circuit (Easee master): user choices: chargers are picked as HA
  devices on each car's load balancing page; the circuit is stored once in the
  site options (`circuits`), so a change from one car applies to all of it; a
  car joins through the devices of its charger entities. Circuit limit = sum
  per grid phase of the circuit cars' requests (not the max), patched onto
  every circuit car's `charger_action` by the site before publishing; the first
  charging car ahead decides 1/3 phases (`circuit_phases` in `site_clamp`).
  Only the master's blueprint automation fills in "Set circuit current".
- User choice: the blueprint runs once per decision change, doing everything
  that changed, so each trace is one whole decision. It triggers only on the
  `decision` attribute (built in `CJPActionSensor`, `charger_decision` in
  `sensor.py`, from the attributes actually written, incl. the site's circuit
  patch) and compares from/to state into the `changed` variable. Not
  `mode: restart` + delay: a restart can cancel a start and lose what changed.

### PyScript import

Many users still run the PyScript version, so the import must keep working and
stay easy (`find_legacy_sources`, `load_legacy_config` in
`management/storage.py`, `options_from_legacy` in `flow/schema.py`; covered by
`test_import_legacy_pyscript_config` in `tests/test_config_flow.py`).

- Legacy sources in the HA config folder: `ev`, `ev2` (instance without EV),
  `ev_test`, `ev_test2` (old config keys -> apply `CONFIG_KEYS_RENAMING`).
  `cjp_config.yaml` belongs to an abandoned rewrite and is not an import
  source; `powerwall_*_db.yaml` are BattMind, not CJP.
- Migration must happen while PyScript still runs, otherwise the
  input_*/template values are gone.

### Debug export

- `management/debug_export.py`, service `cable_juice_planner.export_debug`,
  diagnostic button `export_debug`, and a discreet "Problem or error?" section
  at the bottom of the dashboard's Settings view (not an overview badge).
  Writes `<config>/cable_juice_planner_debug/<prefix>_debug_<ts>.json.gz`
  (newest 5 kept) plus a persistent notification with a signed 24 h link and a
  prefilled `.github/ISSUE_TEMPLATE/bug_report.yml` link. CJP snapshots carry
  prices only once an hour, so the export adds `price_data`.
- Keep this file in sync with the BattMind copy.
- To analyse a file handed in by the user, run from the repo root:
  `PYTHONPATH=../ha-dev/windows %LOCALAPPDATA%/ha-dev-tests/venv/Scripts/python.exe tests/debug_replay.py <file> [failed|planning|<index>]`.

## Tests and validation

### Devcontainer

`../.devcontainer/devcontainer.json` opens the workspace folder (both
integration repos and `ha-dev`) in a container with Home Assistant installed.
Inside it, run `python -m pytest` from the repository root; it is Linux, so no
shims are needed. The dev HA runs on `http://localhost:8123`; after a code
change run `bash ha-dev/devcontainer/restart.sh`, and
`bash ha-dev/devcontainer/restart.sh reset` wipes the state and seeds again.
Seeding creates four car profiles, each with its own prefix ("Car with
everything"/`car_full` with every car entity, "Car without EV"/`car_no_ev`
without car entities, "Car without climate entity"/`car_no_climate` without the
climate entity, "Car without Energi Data Service"/`car_no_eds` with only
Strømligning as price provider) plus the shared site entry. Use them to test
missing-entity and single-provider cases. The dev HA, the dev user's profile
and the seeded entries are in English.

The seed data and simulator entities are in `../ha-dev/devcontainer/seed.py`
(`SIM_ENTITIES`/`SEEDS`), the simulator in
`../ha-dev/config/packages/simulator.yaml`, synthetic history and backups in
`../ha-dev/devcontainer/history.py`, and HA config in
`../ha-dev/devcontainer/configuration.yaml`. Test the PyScript import with
`../ha-dev/dev.ps1 import-legacy` before adding the integration.

### Outside the devcontainer, on Windows

Run the suite with `../ha-dev/run_tests_windows.ps1 cjp
[pytest args]`. The first run installs Python 3.14 and the test requirements
in `%LOCALAPPDATA%\ha-dev-tests\venv`; the shims in `../ha-dev/windows`
(fcntl, resource, a pytest plugin that disables the socket guard) must be on
`PYTHONPATH` when calling that venv directly. The system Python 3.11 lacks
HA/astral. Do not create the venv on a network drive; it installs extremely
slowly. The Docker run `../ha-dev/dev.ps1 test cjp [pytest args]` is the
reference. The dev HA runs locally in Docker Desktop, not on a remote server;
see `../ha-dev/README.md` (`ha-dev/dev.ps1 up|restart|reset|test`).

Test pitfalls: never use the `freezer` fixture (FakeDatetime breaks the
strict-type `in_between`); advance timers with
`async_fire_time_changed(utcnow() + n)` or `async_advance()` from
`tests/conftest.py`. Cron triggers reschedule from the fired time, so advance
cron k by k × 300 s. Engine snapshots need `astral`.

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
