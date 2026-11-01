<img src="custom_components/cable_juice_planner/brand/icon.png" alt="Cable Juice Planner" width="128" align="right">

# 🚗Cable Juice Planner🔋🌞📅 for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Release](https://img.shields.io/github/v/release/dezito/Cable-Juice-Planner-HACS)](https://github.com/dezito/Cable-Juice-Planner-HACS/releases)
[![License](https://img.shields.io/github/license/dezito/Cable-Juice-Planner-HACS)](LICENSE)

Plans EV charging based on a work plan or the cheapest times over a week.
The plan uses electricity prices, solar surplus, home battery and the car's actual consumption.

## How Cable Juice Planner compares with other smart chargers

Smart charging can mean different things: a Home Assistant integration, a charger app that schedules a session, or a bundled electricity tariff that automatically controls a compatible car or charger.

CJP plans charging in Home Assistant. An automation, such as the included blueprint, applies the plan to your charger.

The examples below compare published approaches, not prices or performance in a controlled test. Availability, supported hardware and tariffs vary by country and can change.

Two key advantages of CJP are its potential to save more by planning across the whole week and its support for a lower average battery state of charge during everyday driving.

For a typical driver on variable electricity prices, CJP offers substantial savings potential compared with charging to the maximum recommended level every day.<br><br>CJP can wait for cheaper hours across the week, account for forecast solar surplus and avoid buying energy before it is needed.

This is not a guarantee.<br><br>Actual savings depend on prices, solar production and driving.

Its configurable daily reserve can also avoid routinely keeping the battery at a high state of charge, which may be gentler on battery health. Battery-life effects depend on battery chemistry and the vehicle.<br><br>See the scenarios below and [Daily reserve, cheaper power and battery health](#daily-reserve-cheaper-power-and-battery-health).

Compare the projects and provider offerings by scenario. The table describes features from CJP's documentation and the linked projects' or providers' published descriptions.

**Charging-window assumption for the comparison:** For EV Smart Charging, ha-evcc and public/provider solutions, unless their documentation explicitly states otherwise, assume the available cheap-charging window is only from arriving home today until departure the next day. They are not assumed to shift charging into cheaper hours on earlier days.

| Scenario | Cable Juice Planner | [EV Smart Charging](https://github.com/jonasbkarlsson/ev_smart_charging) | [ha-evcc](https://github.com/marq24/ha-evcc) | Public/provider options |
| --- | --- | --- | --- | --- |
| Potential to save on charging | Plans across the week and can use forecast solar surplus.<br>Actual savings depend on prices and usage. | Selects low-price intervals within the assumed window, ending at departure the next day.<br>Actual savings depend on prices and usage. | Solar modes can prioritize solar charging.<br>No direct price-savings comparison is implied. | Octopus schedules for cheaper energy within the assumed window, ending at departure the next day.<br>Other offers combine tariffs, apps or scheduled charging.<br>Actual savings depend on plan and usage. |
| Maximizing battery longevity | Daily reserve supports a lower average SOC, which research links to slower battery aging. | Defaults to a 100% target if no target-SOC entity is set.<br><br>Prolonged high SOC can accelerate aging. | evcc plans can be set to a high target SOC.<br><br>Prolonged high SOC can accelerate aging. | If the service charges to a high target SOC.<br><br>Prolonged high SOC can accelerate aging. |
| Cheapest hours across the week | Selects the week's cheapest eligible hours, not just the hours before the next departure. | No multi-day look-ahead documented. | No multi-day look-ahead documented. | No multi-day price look-ahead documented. |
| Routine charging | Daily reserve, weekly cheap-hour top-up. | Cheap 15/60-minute slots before the next-day departure. | evcc modes: Solar, Min+Solar, Fast. | [Octopus](https://octopus.energy/smart/intelligent-octopus-go/): schedules compatible EV/charger.<br>[OVO](https://www.ovoenergy.com/electric-cars/charge-anytime): schedules within set times.<br>Comparison assumes departure the next day. |
| Planned trip | Add departure date and time, departure SOC or required distance. | No trip-specific inputs documented.<br>Supports a charge-completion time and minimum SOC.<br>Comparison assumes departure the next day. | Can create or adjust charging plans via HA services.<br>No trip-specific inputs documented. | — |
| Unexpected long trip | A low daily reserve may not cover it.<br>A higher reserve keeps more charge available. | A higher target SOC keeps more charge available.<br>Defaults to 100% if no target-SOC entity is provided. | A higher target can be set in the evcc charging plan. | A higher charge target, where configurable, keeps more charge available. |
| Solar and home battery | Uses solar surplus and forecasts it when planning ahead.<br>Can account for a home battery. | Price-based interval selection. | evcc Solar/Min+Solar modes and battery settings. | Looad advertises Smart Charging in its [app](https://looad.dk/pages/vaelg-din-ladelosning). |
| Convenience after setup | Once configured, plans routine charging and future trips automatically. | Automatic charging after price, vehicle/charger and schedule configuration. | Requires a separate evcc installation as well as the HA integration. | Provider apps/services offer managed charging, subject to device and tariff compatibility. |
| Control and offering | You decide what charging actions to take and adapt the automation to your own setup. | Direct charger or EV-integration control. | Requires a separate evcc instance. | [OK](https://www.ok.dk/privat/produkter/appen/viden/hjemmeladning-med-ok-appen): choose session start/end.<br>[Clever](https://clever.dk/ladeloesninger/): home charging, subscriptions, electricity and public network.<br>[Looad](https://looad.dk/pages/vaelg-din-ladelosning): charger, electricity and app. |

### “Daily charging estimate” section in the Home Assistant CJP dashboard overview

The **Daily charging estimate** is a comparison, not the planner that controls CJP's charging actions. It estimates a daily-charging alternative by using forecast solar from the preceding day and then the cheapest eligible grid-price hours between the car coming home and its next charging deadline. The economy comparison also reports a conventional daily-charging baseline: the same energy in the cheapest hours before departure, without shifting the charge to an earlier, cheaper window or counting solar.

### Daily reserve, cheaper power and battery health

CJP does not assume that the car must always be fully charged, or that the amount needed is the same every day. The daily reserve is configurable with `number.cjp_<prefix>_min_daily_battery_level` (for example, `number.cjp_tesla_min_daily_battery_level`). It represents the battery level you want available when you get home. The plan adds the energy needed for your typical daily distance or the distances in your work plan and trips.

This is a deliberate trade-off compared with charging to the max recommended level every day. CJP prioritizes the energy you expect to need, but can use sufficiently cheap hours to charge further toward the recommended limit. When prices are high, it can leave that extra capacity unfilled. When prices are very low, it can take the opportunity to save more.

For battery health, regular smaller top-ups can keep the battery in a narrower state-of-charge (SOC) range and reduce depth of discharge, rather than repeatedly cycling through a wider range or leaving the car at a high SOC for long periods. For example, daily use around 30–60% SOC is generally a gentler pattern than repeatedly using 60–90%, though the exact effect depends on battery chemistry, temperature, the battery-management system and the vehicle. Laboratory findings support lower depth of discharge and avoiding prolonged high SOC, but their benefit does not translate identically to every EV ([Recurrent](https://www.recurrentauto.com/research/one-simple-trick), [Acterra](https://acterra.org/electric-vehicles-battery/), [WiTricity](https://www.witricity.com/blogs/how-you-charge.php)). CJP's configurable daily reserve supports this charging pattern while still letting you charge higher when needed or when power is very cheap. Follow the vehicle maker's guidance, especially for LFP batteries and trips requiring a higher charge.

### CJP's trade-offs

| ✅ Advantages | ⚠️ Disadvantages |
| --- | --- |
| Plans workdays, trips and preheating.<br>Automates routine charging and can find cheaper hours earlier in the week. | More settings and reliable price, charger and battery data are needed. |
| Combines prices, solar, home battery, driving distance and measured charging loss. | Charger control needs your automation.<br>The included blueprint helps. |
| Daily reserve avoids charging to a high level every day.<br>Very cheap hours can fill toward the recommended limit. | An unexpected long trip may require public charging if only the daily reserve remains. |
| Lower average SOC may help battery longevity.<br>CJP also includes a dashboard, history and cost comparisons. | Built-in price providers are Energi Data Service and/or Strømligning.<br>Other sources may need extra setup. |
| Works with supported HA charger and car integrations through your own automation. | More rules and forecasts mean more setup and complexity than a simple price scheduler. |

---

> [!NOTE]
> This is the **integration version** of Cable Juice Planner, installed via HACS. It replaces the
> [PyScript version](https://github.com/dezito/Cable-Juice-Planner) and can import its
> configuration and databases. See [Switching from the PyScript version](docs/migration.md).

> [!IMPORTANT]
> The integration **sends no commands** to the charger or the car. The plan is exposed as
> [action entities](docs/charger-control.md#the-action-entities), and you control the charger yourself with an
> automation. A [blueprint](docs/charger-control.md#the-blueprint) with fail-safe is included for this.

<a href="https://sloth.nu/tesla_solar_demo.gif" target="_blank"><img src="images/tesla_solar_demo_small.gif" width="49%"></a><a href="https://sloth.nu/virtual_ev_demo.gif" target="_blank"><img src="images/virtual_ev_demo_small.gif" width="49%"></a>

![Charging period](images/charging_period.png)

📖 **[Documentation](docs/README.md)** – [getting started](docs/getting-started.md), [how CJP plans](docs/how-it-works.md),
every charging rule explained with examples, [charger control](docs/charger-control.md) and [FAQ](docs/faq-troubleshooting.md).

---

## Charging rules

- **[☀️ Solar surplus charging](docs/rules/solar.md)**
  - The car is charged with the solar panels' surplus production. It can use the whole hour's surplus, even if
    charging only starts later in the hour.
  - Can also be set to X minutes back. With e.g. 15 min the hour is split into four
    (13:00, 13:15, 13:30 and 13:45), and the surplus is used in those intervals.
- **[🏢 Work Plan charging](docs/rules/workplan.md)**
  - Set the number of kilometres, departure, arrival and preheating per workday. The integration finds the
    cheapest times to charge and preheats the car for departure, if selected.
  - When the car is away on a workday and hasn't come home yet, only the way back (half the distance) is
    left for that day.
  - The km per weekday can be [learned](docs/learn-distance.md) from the odometer.
- **[📅 Optimal weekly charging (no Work Plan)](docs/rules/weekly.md)**
  - Set how many kilometres you typically drive per day. Every day, enough is always charged for that
    distance plus the daily minimum, in the day's cheapest hours. The rest up to the **EV max
    recommended charging limit** (e.g. 90 %) is only charged in the week's cheapest hours (the cheapest 30 %), and
    only if the next 24 hours aren't clearly cheaper.
- **[🧳 Trip](docs/rules/trip.md):** charge to a percentage or to a number of km before a departure, optionally with preheating
- **[🔌 Manual charging](docs/rules/manual-forced.md)** (e.g. guests), optionally on solar surplus only
- **[🪫 Forced charging below the daily battery level](docs/rules/manual-forced.md#-forced-charging-below-the-daily-battery-level)** (up to e.g. 30 %), but not in the most expensive hours
- **[💸 Cheap and very cheap power](docs/rules/cheap-power.md):** charge higher (e.g. 70 % / 90 %) when the power is cheap or extra cheap –
  together with Optimal weekly charging, or on its own with **Charge on cheap power**
- **[💯 Full charge every X days](docs/rules/full-charge.md):** for LFP batteries that need a periodic 100% charge for battery-management-system calibration, following the car maker's guidance. CJP targets 100%. If charging stops at 99% and remains inactive for 30 minutes, CJP counts it as complete and stops retrying the final percentage.

> [!NOTE]
> If all charging rules are turned off, Cable Juice Planner charges like a regular charger at max. current.

## Features

- **EV integration or virtual EV:** without a car in Home Assistant you enter the
  battery level yourself when the cable is plugged in
- **Solar and Solcast (optional):** surplus now and forecast ahead, from
  [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) and/or own history
- **Home battery (optional):** charge the home battery to a certain level first, and charge the car from
  the home battery above a minimum
- **Multiple cars on the same main fuse:** [load balancing](docs/load-balancing.md) with priority and phase order,
  and chargers sharing one circuit limit (e.g. an Easee master)
- **House consumption** is estimated from own history (excluding car, home battery and ignored consumers)
- **Price providers:** [Energi Data Service](https://github.com/MTrab/energidataservice) and/or
  [Strømligning](https://github.com/MTrab/stromligning), incl. sell price, tariffs and electricity tax refund;
  forecast prices are corrected with their measured bias
- **Realistic consumption** based on the driving pattern: driving efficiency, km/kWh, Wh/km and estimated range
- **[Public charging](docs/public-charging.md)** (with EV integration)
  - a charge away from home is detected automatically: time, battery level at start and end, and percentage added
  - you get a notification, enter the total price, and the charge is added to the history and the economy
  - waiting sessions are remembered across restarts, and charges can also be added with a service
- **[Charging loss](docs/charging-loss.md)**
  - **measured continuously** from the charger's kWh meter and the car's battery level, also back in Home
    Assistant's history – no full charge needed
  - per charging power (< 2,5 kW, 2,5-4 kW, 4-7 kW, > 7 kW), so the model can tell the car's fixed use from the
    charger's loss – a low power (solar on 1 phase) loses the most
  - **Use measured charging loss:** the plan uses the measured loss per hour at that hour's power instead of the
    configured one
  - a one-time measurement from low to full is still possible
- **[Learn driving distance](docs/learn-distance.md)**
  - the odometer is read every evening, and from the last 8 weeks CJP suggests the **km per workday** in the work plan
    (the median per weekday) and the **typical daily distance** for optimal weekly charging (75th percentile)
  - the suggestions are always shown, and with **Learn driving distance** on, the plan uses the higher of the
    entered and the learned km – the entered numbers are not changed
  - the analysis shows entered against learned distance per weekday
- **[Analysis](docs/dashboard-analysis.md#analysis)**
  - economy per month: kWh per source (grid, solar, home battery, public), cost and price/km
  - saved compared with daily charging, and a score for how close the price came to the lowest possible
  - driving efficiency against the outdoor temperature (how much more the car uses at 0 °C)
  - whether the car was ready at each work plan departure
  - planned the evening before against actually charged
  - the price forecast against the real prices per day ahead: the error, and how often it found the cheapest hours
  - the error of each forecast type (EMA, trend, average) for the house consumption and the car's km/kWh, against the actual values, and why another type is suggested when it is more accurate
  - battery capacity over time, and the charging loss per power
- **CO₂ emissions** and **kWh charged by solar**
- **Very detailed charging history**
  - each charge: time, charging reason, percentage, kWh, price and price/kWh
  - per month: km, kWh, share from solar, price and price/kWh
  - split between day and night
  - public charging
- **Overviews:** last drive's efficiency, costs for the current battery level,
  charging overview, departure plan, estimate for daily charging and solar surplus forecast
- **Fail-safe:** CJP stores the prices in its own [price database](docs/how-it-works.md#prices) and fills hours
  without a price from it, and the blueprint charges at a fixed current if the integration stops. See
  [When something goes wrong](docs/how-it-works.md#when-something-goes-wrong)
- **[Ready-made dashboard](docs/dashboard-analysis.md)** in the sidebar, one per car, built from your setup
  - **Overview:** battery level forecast, prices with solar and the charging plan, status, trip, manual charging and history
  - **Settings:** all charging rules, work plan per weekday, battery levels and charging loss, each with "Read more"
  - **Analysis:** the analysis above plus the history of battery, charge limit, driving efficiency and solar
- **Languages:** Danish, English, German, Polish and Ukrainian
- **Does not run in Home Assistant's main thread:** all calculations run in the integration's own
  worker thread, so Home Assistant isn't blocked while the plan is calculated

| Last drive | Battery level costs | Charging overview |
| --- | --- | --- |
| ![Last drive efficiency](images/last_drive_efficiency.png) | ![Battery level costs](images/battery_level_costs.png) | ![Charging overview](images/charging_overview.png) |

| Overview | History | Solar surplus |
| --- | --- | --- |
| ![Overview](images/overview.png) | ![History](images/history.png) | ![Solar surplus](images/solar_overproduction.png) |

---

## Requirements

- Home Assistant **2025.1** or newer with recorder (default). The [analysis](docs/dashboard-analysis.md#analysis) and the charging loss read
  its history: the longer `purge_keep_days`, the more there is from the first day (e.g. 30 days).
- A charger with **status**, **power** (W) and **kWh** sensors
- Price integration: [Energi Data Service](https://github.com/MTrab/energidataservice) and/or [Strømligning](https://github.com/MTrab/stromligning)
- Optional:
  - EV integration (battery level, range and odometer, and preferably charge port, charge limit and location)
  - solar production (W) and [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar)
  - a weather integration with `weather.get_forecasts`, e.g.
    [Meteorologisk institutt](https://www.home-assistant.io/integrations/met/),
    [AccuWeather](https://www.home-assistant.io/integrations/accuweather/) or
    [OpenWeather](https://www.home-assistant.io/integrations/openweathermap/)
- For the dashboard (install these HACS frontend cards): [Mushroom](https://github.com/piitaya/lovelace-mushroom),
  [ApexCharts Card](https://github.com/RomRider/apexcharts-card),
  [Config Template Card](https://github.com/iantrich/config-template-card) and
  [card-mod](https://github.com/thomasloven/lovelace-card-mod)

### Tested with

| Charger | Car | Inverter |
|---|---|---|
| [Easee](https://github.com/nordicopen/easee_hass) | [Tesla](https://github.com/alandtse/tesla), [TeslaFi](https://github.com/jhansche/ha-teslafi), [Tessie](https://www.home-assistant.io/integrations/tessie/), [Polestar](https://github.com/pypolestar/polestar_api), [Cupra WeConnect](https://github.com/daernsinstantfortress/cupra_we_connect), [Kia UVO](https://github.com/Hyundai-Kia-Connect/kia_uvo) | [Huawei Solar](https://github.com/wlcrs/huawei_solar), [Fronius](https://www.home-assistant.io/integrations/fronius/), [Kostal](https://www.home-assistant.io/integrations/kostal_plenticore/), [Solarman](https://github.com/StephanJoubert/home_assistant_solarman/) |

The charger is controlled by your own automation, so other chargers work too, as long as they can be started,
stopped and have their amps set from Home Assistant.

---

## Installation

### HACS (recommended)

[![Open your Home Assistant and add the repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dezito&repository=Cable-Juice-Planner-HACS&category=integration)

1. **HACS** → ⋮ → **Custom repositories**: add `https://github.com/dezito/Cable-Juice-Planner-HACS` as **Integration**.
2. Search for **Cable Juice Planner**, install it, and restart Home Assistant.
3. Add the integration:

   [![Open your Home Assistant and add Cable Juice Planner.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cable_juice_planner)

   or **Settings → Devices & services → Add integration → Cable Juice Planner**.
4. Create the automation that controls the charger, e.g. with the [blueprint](docs/charger-control.md#the-blueprint).

### Manual

Copy `custom_components/cable_juice_planner` to `config/custom_components/cable_juice_planner` and restart Home Assistant.

---

## Setup

Setup is done in the UI, split into pages with help texts:

1. **Car:** name (used as the device name and in notifications), prefix and language. All entity
   ids start with `cjp_`. Without a prefix they are named `sensor.cjp_overview`, and with the prefix
   `tesla` they are named `sensor.cjp_tesla_overview`. Use one prefix per car.
2. **Charger:** status, power, kWh meters, on/off, cable and CO₂, as well as voltage, phases,
   max. amps and charging loss
3. **Load balancing:** priority, phase order and the chargers on the same circuit (only with a
   [shared grid connection](docs/load-balancing.md))
4. **EV:** battery level, range, odometer, charge port/cable, charge limit,
   location, update time and climate, as well as battery size. Skip the page if you don't have
   an EV integration (virtual EV).
5. **House and home battery:** consumption, ignored consumers, the home battery's level and power
6. **Price providers:** Energi Data Service and/or Strømligning, refund and selling fee
7. **Solar panels, solar charging and weather** (only with solar panels), incl. a
   [fixed solar sell price](docs/rules/solar.md#fixed-solar-sell-price) (−1 = the hourly price)
8. **Notifications, advanced** (interval, dashboard, diagnostic file log, testing mode) **and databases**

The entity selectors only show relevant entities: battery level only `battery` sensors, power
only `power` sensors, kWh only `energy` sensors, prices only entities from the price integrations
and the forecast only Solcast.

Everything can be changed afterwards under **Configure** on the integration, via a menu.
[Getting started](docs/getting-started.md) goes through every page and how to set the values.

---

## How to control the charger and the car

| Entity | Content |
|---|---|
| `sensor.cjp_<prefix>_charger_action` | `charge` or `stop`, with the current per phase, the charge limit and `charger_enabled` as attributes |
| `sensor.cjp_<prefix>_charger_phase_1_amps` … `_3_amps` | desired charging current per phase in A (0 = phase not used) |
| `sensor.cjp_<prefix>_charging_watts` | desired charging power |
| `sensor.cjp_<prefix>_charging_limit` | desired charge limit on the car in % (only with EV integration) |
| `event.cjp_<prefix>_ev_request` | `wake_up`, `preheat_start` (data: `departure`, `defrost`) and `preheat_stop` |

The easiest way is the **blueprint** that is installed with the integration
(*Settings → Automations & scenes → Blueprints → Cable Juice Planner - charger control*): one automation per
car, with a section for the charger and one for the car, and a fail-safe that charges at a fixed current if
the integration stops. See [Charger and car control](docs/charger-control.md) for every input, typical
setups and examples.

Find a ready-made setup for your charger and car, or share your own, under
[Blueprint setups](https://github.com/dezito/Cable-Juice-Planner-HACS/discussions/categories/blueprint-setups).

The blueprint file is never overwritten on its own. If a new version arrives with a HACS update (or you
have edited the file yourself), a repair shows up after restart under *Settings → System → Repairs*.

---

## Multiple cars (load balancing)

If several cars charge on the same grid connection, add one **Shared grid connection**
(**Add integration → Cable Juice Planner → Shared grid connection**) with the main fuse per phase and
optionally the house's phase sensors. Each car gets a priority and a phase order, and together they never
use more current per phase than the fuse. Chargers sharing one circuit limit (e.g. Easee chargers behind a
master) can be linked, so the master's automation sets the circuit for all of them. See
[Multiple cars](docs/load-balancing.md).

---

## Entities

The settings from the dashboard are entities that are kept across restarts:

| Type | Examples |
|---|---|
| `switch` | charging rules, workdays and preheating per day, manual charging, forced charging, charge on cheap power, calculate charging loss, use measured charging loss, learn driving distance, correct the price forecast |
| `number` | km per workday, battery levels, charge level on cheap power, trip, full charge every X days, price of public charging, virtual EV |
| `time` | departure and arrival per workday |
| `datetime` | the trip's departure and arrival |
| `select` | forecast type |
| `button` | force planning, reset trip, recalculate charging loss, export data, reset dashboard |
| `binary_sensor` | public charging session done (waiting for the price) |

If you click a setting, a short explanation is shown under **Attributes** (`description`
and `help`). The plan is recalculated 5 seconds after a change.

The results include `sensor.cjp_current_charging_rule`, `_overview`, `_charging_history`,
`_emoji_description`, `_kwh_cost_price`, `_drive_efficiency`, `_km_per_kwh`, `_estimated_range`,
`_co2_emitted`, `_kwh_charged_by_solar`, `_last_full_charge` (set by the integration when the battery was
full), `_charging_loss_model`, `_economy` and `_analysis`. Large attributes (markdown, plans, measurements)
are not stored in the recorder database.

---

## Dashboard

With **Dashboard in the sidebar** enabled (default), each car gets its own dashboard,
e.g. `/cjp-planner` (without prefix) or `/cjp-tesla`, with three views:

- **Overview:** battery level with forecast and planned charging hours, electricity prices with solar and the
  charging plan, current charging rule, status, manual and forced charging, trip, public charging, the
  charging history and the overview
- **Settings:** all charging rules, the work plan per weekday, battery levels, cheap power, full charge,
  solar/home battery, charging loss and virtual EV – with "Read more"
- **Analysis:** economy, driving and temperature, ready at departure, planned against charged, price
  forecast, battery capacity and charging loss

Every view is explained in [Dashboard and analysis](docs/dashboard-analysis.md). Once you save your own
changes to the dashboard, your version is kept. Press **Reset dashboard** (on the integration's device) to
go back to the generated dashboard.

---

## Services

| Service | Description |
|---|---|
| `cable_juice_planner.enforce_planning` | Recalculate the plan now |
| `cable_juice_planner.manual_add_public_charging_session` | Calculate (and optionally save) a public charging session |
| `cable_juice_planner.debug_info` | Returns internal state for troubleshooting |
| `cable_juice_planner.export_debug` | Save an anonymised debug file for a GitHub issue |
| `cable_juice_planner.export_data` | Save a backup of data and settings (see below) |
| `cable_juice_planner.import_data` | Replace data and settings with a backup |

All services optionally take `config_entry_id` so they only affect one car (`import_data` requires it).

---

## Backup: export and import of data

A backup is one JSON file in `/config/cable_juice_planner_backups/` with the databases, the settings (switches, numbers, ...) and the configuration from the setup. The folder is included in Home Assistant's own backups.

- **When you delete a car**, a backup is saved automatically (`…_deleted.json`) before the databases are removed.
- **Export** with the *Export data* button on the device, under **Configure → Backup** or with `cable_juice_planner.export_data`.
- **Import into a new car**: when you create a new one, the setup offers to restore one of the backups. The configuration is pre-filled, and databases and settings are restored on first start.
- **Import into an existing car** under **Configure → Backup** or with `cable_juice_planner.import_data` (`file: cjp_tesla_20261024_120000.json`). Databases and settings are replaced, and the integration is reloaded. The current data is first saved as `…_before_import.json`, so an import can be undone. With `restore_configuration` the configuration is also taken over. Leave it off when the backup comes from another Home Assistant.

---

## Switching from the PyScript version

> [!WARNING]
> Keep the PyScript version running while the integration is added. Otherwise its helpers and sensors
> are gone, and their values can't be copied.

If PyScript files exist (`ev_config.yaml`, `ev2_config.yaml`, …), the setup offers to import the
configuration, the databases and the values of the old helpers. The PyScript files are only read, never
changed, and the new entities start with `cjp_`, so both versions can run side by side.
See [Switching from the PyScript version](docs/migration.md) for the steps and what became what.

---

## Troubleshooting

See [FAQ and troubleshooting](docs/faq-troubleshooting.md). In short:

- Turn on **Diagnostic file log** under **Configure → Advanced** (`cable_juice_planner_<prefix>.log` in the config folder).
- Found a bug? Press **Export debug info** on the device (or run `cable_juice_planner.export_debug`) right after it happened, and follow the
  notification: it links to the file and to a prefilled GitHub issue. See [Report a problem](docs/faq-troubleshooting.md#report-a-problem).

---

## License

GPL-3.0, see [LICENSE](LICENSE).
