# Cable Juice Planner documentation

The [README](../README.md) gives the short version. These pages go into detail: how Cable Juice Planner
(CJP) plans, what every charging rule does, and how to connect it to your charger and car.

## Start here

| Page | Read it when |
|---|---|
| [Getting started](getting-started.md) | you install CJP for the first time |
| [How CJP plans](how-it-works.md) | you want to understand why the car charges when it does |
| [Charger and car control](charger-control.md) | you build the automation that controls the charger and the car |

## Charging rules

| Page | Content |
|---|---|
| [Work plan](rules/workplan.md) | km, departure, homecoming and preheating per weekday |
| [Optimal weekly charging](rules/weekly.md) | without a work plan: the daily need, the rest in the week's cheapest hours |
| [Trip](rules/trip.md) | charge to a percentage or a number of km before a departure |
| [Solar surplus charging](rules/solar.md) | charging with the surplus, 1/3 phase switching, the home battery |
| [Cheap power](rules/cheap-power.md) | charge higher when the power is cheap or very cheap |
| [Full charge](rules/full-charge.md) | a full charge every X days for the battery's sake |
| [Manual and forced charging](rules/manual-forced.md) | charge now, solar only, and charging below the daily minimum |

## Features

| Page | Content |
|---|---|
| [Multiple cars (load balancing)](load-balancing.md) | several cars on the same main fuse |
| [Charging loss](charging-loss.md) | measured continuously per charging power |
| [Learn driving distance](learn-distance.md) | km per workday and the typical daily distance from the odometer |
| [Public charging](public-charging.md) | charges away from home in the history and the economy |
| [Dashboard and analysis](dashboard-analysis.md) | every view, and how to read the analysis |

## Reference

| Page | Content |
|---|---|
| [Recipes](cookbook.md) | typical setups: no car integration, solar with a fixed sell price, together with BattMind … |
| [Switching from the PyScript version](migration.md) | import of configuration and databases |
| [FAQ and troubleshooting](faq-troubleshooting.md) | "why doesn't it charge now?", logs and debug info |
