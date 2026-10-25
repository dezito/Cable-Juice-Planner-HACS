# FAQ and troubleshooting

Start with the overview on the dashboard: the plan shows a reason for every charge hour, and
`sensor.cjp_<prefix>_current_charging_rule` tells why the charger does what it does right now.

## Why doesn't it charge now?

Check in this order:

- **Is the car ready to charge?** Plugged in, at home (location, or the charger's status), and the charger
  status is one CJP knows (`awaiting_start`, `ready_to_charge`, `charging` …).
- **Is this a planned hour?** The plan charges in the cheapest hours before the departure – not necessarily now.
- **Is the car already above what the rules need?** E.g. the work plan only charges what the next workday
  needs, optimal weekly charging only tops up in the week's cheapest hours.
- **Solar:** the surplus may be too small for the min. current (6 A on 1 phase), or the home battery is below
  *Charge EV after home battery minimum level*, or it is one of the 4 expensive hours where the surplus is sold.
- **Is the action `charge`, but the car doesn't charge?** Then it is the automation: check the blueprint's
  trace (*Automations → ⋮ → Traces*) and its *Charging state* inputs.

## It planned to charge tonight, but the car wasn't charged in the morning

Usually the plan changed during the night. CJP recalculates the plan every few minutes, and it moves the
charging when something it depends on changes:

- **The prices changed.** Forecast prices (e.g. Carnot) and hours filled from the price database are
  replaced by the real prices when they are published, and a cheaper time before the departure may turn up.
  The [price forecast analysis](dashboard-analysis.md#price-forecast) shows how far off the forecast usually is.
- **The need changed.** The battery level, the departure or the km needed are different from when you looked.
- **The solar forecast changed.** More expected sun means less grid charging.

As long as the car is ready at the departure, the plan has done its job. To see what actually happened:

- open the history of `sensor.cjp_<prefix>_charger_action` (*History* or the entity's dialog): it shows
  when the action was `charge` and `stop` during the night
- `sensor.cjp_<prefix>_current_charging_rule` and its history tell why
- the overview shows the plan as it is now, with the charge hours that are left
- [Planned against actually charged](dashboard-analysis.md#planned-against-actually-charged) in the analysis
  compares the plan from the evening before with what was charged

If the action was `charge` but the car didn't charge, the automation or the charger is the problem – see
[Why doesn't it charge now?](#why-doesnt-it-charge-now).

## Why does it charge at max. current all the time?

All charging rules are off – then CJP charges like a normal charger. Turn on at least one rule.

## The car's consumption / range looks wrong

The driving efficiency is measured from the drives. A few drives with a wrong battery level or odometer
(e.g. a car integration that reports old values) can skew it. It gets better as more drives are stored.

## What happens when prices are missing?

Single hours without a price (e.g. the rest of the week without a price forecast) are filled from CJP's own
price database – the stored prices of the same hour and weekday. They are listed under *Using offline
prices for some hours* in the overview.

If the price integration is down completely, CJP keeps the last prices for 2 hours, then plans the next 7
days with the database. The database fills up over time, so right after the first installation it has
little to go on – unless it was imported from the PyScript version or a backup.

## The plan isn't recalculated

It is only recalculated when something it depends on has changed. Press **Force planning** (or call
`cable_juice_planner.enforce_planning`).

## The charging loss measurement doesn't start or finish

See [Charging loss](charging-loss.md#one-time-measurement): the charge has to add at least 30 percentage
points, and the car has to report when it is full.

## Logs and debug info

- Turn on **Diagnostic file log** under **Configure → Advanced**. The log is written to
  `cable_juice_planner_<prefix>.log` in the config folder (2 MB, 3 rotated files).
- Or enable debug logging in `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.cable_juice_planner: debug
  ```
- Call `cable_juice_planner.debug_info` under **Developer tools → Actions**, and attach the response when you
  open an [issue](https://github.com/dezito/Cable-Juice-Planner-HACS/issues).
