# Dashboard and analysis

With **Dashboard in the sidebar** on (default), each car gets its own dashboard, e.g. `/cjp-planner`
(without prefix) or `/cjp-tesla`. It is built on every startup from your setup: cards for things you
haven't set up are removed.

It needs these HACS frontend cards: [Mushroom](https://github.com/piitaya/lovelace-mushroom),
[ApexCharts Card](https://github.com/RomRider/apexcharts-card),
[Config Template Card](https://github.com/iantrich/config-template-card) and
[card-mod](https://github.com/thomasloven/lovelace-card-mod).

## Overview

| Card | Shows |
|---|---|
| **Battery level** | the forecast battery level with the planned charging hours |
| **Electricity prices** | the prices with the solar forecast and the charging plan |
| **Current rule** | the current charging rule |
| **Manual / forced charging** | the switches – forced charging only while it is relevant |
| **Status** | above the trip: battery level, charging limit, the charger's power and state, estimated range and kWh cost price |
| **Trip** | departure, arrival, target and preheating |
| **Public charging** | only while a charge waits for its price |
| **Charging history** | each charge with time, reason, %, kWh, price and price/kWh, and per month km, kWh, share from solar and price |
| **Overview** | the plan with a reason for each hour, the departure plan, the cost of the current battery level, the last drive's efficiency and the solar surplus forecast |

At the top: **Force planning** recalculates the plan now. The overview also has the emoji explanation – see [Emojis](emojis.md).

## Settings

All rules with a **Read more** from the entities' help texts: the charging rules, the work plan per weekday
(incl. [learn driving distance](learn-distance.md)), optimal weekly charging, the battery levels, cheap power,
full charge, solar/home battery, charging loss, the virtual EV and, under *Advanced*, the forecast type and
[correct the price forecast](how-it-works.md#prices). Preheating is only shown with a car
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

### Price forecast

How well the price integration's forecast (e.g. Carnot in Energi Data Service, or Strømligning) hit the
real buy price. Every hour CJP saves the forecast for the coming days, without its own small adder per day
ahead. When the real prices of a day are published (around 13:00 the day before), they are compared with
what the forecast said 1, 2, 3 … days before.

| Column | Meaning |
|---|---|
| Days ahead | how many days before the forecast was made. 1 = the day before, before the real prices came |
| Days | days compared the last 30 days |
| Error 7 / 30 days | the average error per hour in kr/kWh |
| Bias | > 0: the forecast was too high on average |
| Worst hour | the biggest error in a single hour |
| Cheapest found | how many of the day's 4 cheapest hours by the forecast really were among the 4 cheapest – the hours the plan prefers to charge in. In brackets the extra price per kWh of picking them by the forecast |
| Correction | what the plan adds to the forecast prices for that day ahead: minus the bias, after 7 days of measurements and at most ±0.50 kr. – while there are too few days, or when *Correct the price forecast* (*Settings → Advanced*) is off |

The level of the forecast matters less than the order of the hours: a forecast that is 0.30 kr too high but
finds the right cheap hours plans almost as well as the real prices. The charts show the real price against
the forecast 1 to 7 days before per hour (from 2 days ago to 4 days ahead), the corrected forecast the
plan uses for the coming hours, and the error per day for the last 31 days.

Only the buy price is measured, and only hours with a forecast from the price integration – hours CJP fills
from its own price database are not included. The measurements always use the forecast as the price
integration published it, never the corrected one. The first numbers come at 13:00 the day after the first
start, "3 days ahead" after 3 days.

### Forecast accuracy

How well each forecast type (EMA, trend, average) predicts the two things CJP forecasts from its own
history:

- **House consumption per hour** – used for the solar surplus the car can charge from
- **The car's km/kWh per drive** – used for how much trips and workdays need

Just before a new measurement is added to the history (the house consumption at the end of every hour, the
km/kWh when a drive is saved), CJP calculates what each type would have predicted from the same history
and compares it with the measurement. ✅ marks the selected type, ⭐ the most accurate.

| Column | Meaning |
|---|---|
| Avg. kWh/hour, Avg. km/kWh | what each type expected on average. The 📏 **Actual** row at the top is the measured value |
| Error | the average error per hour (house: 7 and 30 days) or per drive (car: the last 10 and 30 drives) |
| Relative | the error as a share of the measured value |
| Bias | > 0: the forecast was too high on average |

The error is counted hour by hour and drive by drive, so a type can hit the average and still miss a lot
each time. When another type is more accurate, a warning shows the numbers behind the suggestion: both
types' error, how much smaller it is, and what each expected against the actual value.

One forecast type (*Settings → Advanced → Forecast type*) is used for the house consumption, km/kWh and the
fallback prices alike, so the two tables can point at different types. Pick the one that is most accurate
where it matters most to you: without solar, the house consumption is not used. The numbers are collected
from the first start: the house consumption after the first whole day, km/kWh after the first drive.

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
