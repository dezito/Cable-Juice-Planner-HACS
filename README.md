<img src="custom_components/cable_juice_planner/brand/icon.png" alt="Cable Juice Planner" width="128" align="right">

# 🚗Cable Juice Planner🔋🌞📅 for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Release](https://img.shields.io/github/v/release/dezito/Cable-Juice-Planner-HACS)](https://github.com/dezito/Cable-Juice-Planner-HACS/releases)
[![License](https://img.shields.io/github/license/dezito/Cable-Juice-Planner-HACS)](LICENSE)

Plans EV charging based on a work plan or the cheapest times over a week.
The plan uses electricity prices, solar surplus, home battery and the car's actual consumption.

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
- **[💯 Full charge every X days](docs/rules/full-charge.md)** for the battery's sake (above the max recommended limit). It counts as done
  from 99 %, as a cold battery may stop there.

> [!NOTE]
> If all charging rules are turned off, Cable Juice Planner charges like a regular charger at max. current.

## Features

- **EV integration or virtual EV:** without a car in Home Assistant you enter the
  battery level yourself when the cable is plugged in
- **Solar and Solcast (optional):** surplus now and forecast ahead, from
  [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) and/or own history
- **Home battery (optional):** charge the home battery to a certain level first, and charge the car from
  the home battery above a minimum
- **Multiple cars on the same main fuse:** [load balancing](docs/load-balancing.md) with priority and phase order
- **House consumption** is estimated from own history (excluding car, home battery and ignored consumers)
- **Price providers:** [Energi Data Service](https://github.com/MTrab/energidataservice) and/or
  [Strømligning](https://github.com/MTrab/stromligning), incl. sell price, tariffs and electricity tax refund
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
  - the suggestions are always shown, and with **Learn driving distance** on, the numbers are set automatically
- **[Analysis](docs/dashboard-analysis.md#analysis)**
  - economy per month: kWh per source (grid, solar, home battery, public), cost and price/km
  - saved compared with daily charging, and a score for how close the price came to the lowest possible
  - driving efficiency against the outdoor temperature (how much more the car uses at 0 °C)
  - whether the car was ready at each work plan departure
  - planned the evening before against actually charged
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
- For the dashboard (HACS frontend): `mushroom`, `apexcharts-card`, `config-template-card` and `card-mod`

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
3. **Load balancing:** priority and phase order (only with a [shared grid connection](docs/load-balancing.md))
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

The blueprint file is never overwritten on its own. If a new version arrives with a HACS update (or you
have edited the file yourself), a repair shows up after restart under *Settings → System → Repairs*.

---

## Multiple cars (load balancing)

If several cars charge on the same grid connection, add one **Shared grid connection**
(**Add integration → Cable Juice Planner → Shared grid connection**) with the main fuse per phase and
optionally the house's phase sensors. Each car gets a priority and a phase order, and together they never
use more current per phase than the fuse. See [Multiple cars](docs/load-balancing.md).

---

## Entities

The settings from the dashboard are entities that are kept across restarts:

| Type | Examples |
|---|---|
| `switch` | charging rules, workdays and preheating per day, manual charging, forced charging, charge on cheap power, calculate charging loss, use measured charging loss, learn driving distance |
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
- **Analysis:** economy, driving and temperature, ready at departure, planned against charged, battery
  capacity and charging loss

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
- Call `cable_juice_planner.debug_info` under **Developer tools → Actions**, and attach the response when you open an issue.

---

## License

GPL-3.0, see [LICENSE](LICENSE).
