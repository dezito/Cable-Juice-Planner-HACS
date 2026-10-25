# Dashboard and analysis

With **Dashboard in the sidebar** on (default), each car gets its own dashboard, e.g. `/cjp-planner`
(without prefix) or `/cjp-tesla`. It is built on every startup from your setup: cards for things you
haven't set up are removed.

It needs the HACS frontend cards `mushroom`, `apexcharts-card`, `config-template-card` and `card-mod`.

## Overview

| Card | Shows |
|---|---|
| **Battery level** | the forecast battery level with the planned charging hours |
| **Electricity prices** | the prices with the solar forecast and the charging plan |
| **Status** | the current charging rule and the charger's state |
| **Manual / forced charging** | the switches – forced charging only while it is relevant |
| **Trip** | departure, arrival, target and preheating |
| **Public charging** | only while a charge waits for its price |
| **Charging history** | each charge with time, reason, %, kWh, price and price/kWh, and per month km, kWh, share from solar and price |
| **Overview** | the plan with a reason for each hour, the departure plan, the cost of the current battery level, the last drive's efficiency and the solar surplus forecast |

At the top: **Force planning** recalculates the plan now.

## Settings

All rules with a **Read more** from the entities' help texts: the charging rules, the work plan per weekday
(incl. [learn driving distance](learn-distance.md)), optimal weekly charging, the battery levels, cheap power,
full charge, solar/home battery, charging loss and the virtual EV. Preheating is only shown with a car
integration that has a climate entity.

The plan is recalculated 5 seconds after a change.

## Analysis

Everything is calculated from Home Assistant's history and CJP's own data, and refreshed every night. The
comparisons need plug-in times and hourly prices, which the recorder has for its `purge_keep_days` – the
longer, the more there is from the first day (e.g. 30 days). After that CJP saves the prices every hour itself.

### Economy

Per month: kWh per source (grid, solar, home battery, public), what it cost and kr/km. And what the same kWh
would have cost at:

| Comparison | Meaning |
|---|---|
| Average price | the month's average price |
| **Daily charging** | charging in the cheapest hours until the next departure, like most smart chargers |
| **Lowest possible** | the cheapest hours while the car was plugged in |

- **Saved** is the saving compared with daily charging.
- **Score** is how close the price came to the lowest possible, measured from the average: 100 % = the
  cheapest hours, 0 % = the average price.

Older months (before the recorder's history) only show kWh per source, cost and kr/km. *Daily charging* uses
the work plan as it is set now. `sensor.cjp_<prefix>_economy` holds this month's saving, the months in the
attributes.

### Driving and temperature

km/kWh against the outdoor temperature, from the drives (unplugging to plugging in) and the outdoor
temperature's statistics – and how much more energy per km the car uses at 0 °C than at 20 °C.

### Ready at departure

Whether the car had the battery level it needed at each work plan departure, saved at every departure from
the first start. `sensor.cjp_<prefix>_analysis` is the share of the last 30 departures that were ready.

### Planned against actually charged

The kWh planned the evening before against what was charged, and how much was charged outside the plan
(manual, forced charging …). Saved every evening from the first start.

### Battery capacity

kWh from the charger per 1 % per month and power group, from the [charging loss](charging-loss.md)
measurements. Rising over the years = less capacity. Compare the same month year by year – winter is higher.

### Charging loss per power

- **Loss by charging power:** the measured loss per charge part (dots) and the model per phase count (lines).
- **What the loss is made of:** the model's loss split into the **charger's loss** and the car's **fixed
  use** per power – the fixed use is what makes a low power (solar on 1 phase) expensive.

See [Charging loss](charging-loss.md).

### History

The battery level, the charge limit, the driving efficiency and the solar charging over time.

## Editing the dashboard

You can edit the dashboard like any other (**Edit**, or **⋮ → Raw configuration editor** to copy it). Once
you save your changes, your version is kept, and dashboard changes in later updates of the integration no
longer show up. Press **Reset dashboard** (on the integration's device) to throw your changes away and go
back to the generated dashboard.
