# Switching from the PyScript version

The integration replaces the [PyScript version](https://github.com/dezito/Cable-Juice-Planner). It reads the
PyScript files but never changes them, and stores its own data in `.storage`.

> [!WARNING]
> Keep the PyScript version running while the integration is added. Otherwise its helpers and sensors
> are gone, and their values can't be copied.

## Steps

1. **Add the integration.** If PyScript files exist (`ev_config.yaml`, `ev2_config.yaml`, …), the setup
   asks which installation should be imported:
   - the values from `<name>_config.yaml` are filled in on the pages
   - `<name>_*_db.yaml` (charging history, power values, solar production, kWh prices, driving efficiency
     and km/kWh) are imported on first start
   - the values from `input_boolean/input_number/input_select/input_datetime.<name>_*` and the template
     sensors are copied to the new entities
2. **Create the automations** on the [action entities](charger-control.md#the-action-entities), e.g. with the
   [blueprint](charger-control.md#the-blueprint), but keep them off as long as the PyScript version controls
   the charger. The new entities start with `cjp_` and don't collide with the PyScript version's `ev_*`.
3. **Compare the plans** for a few days.
4. **Switch over:** remove `pyscript/ev.py` and `packages/ev.yaml`, restart, and turn the automations on.

## What became what

| PyScript | Integration |
|---|---|
| `ev_config.yaml` | Setup / **Configure** in the UI |
| `input_boolean.ev_*` | `switch.cjp_*` |
| `input_number.ev_*` | `number.cjp_*` |
| `input_datetime.ev_workday_*` | `time.cjp_*` |
| `input_datetime.ev_trip_*` | `datetime.cjp_*` |
| `input_datetime.ev_last_full_charge` | `sensor.cjp_last_full_charge` (set by the integration) |
| `input_button.ev_*` | `button.cjp_*` |
| Charging commands (`charge_switch`, `charging_amps`, `wake_up`, dynamic limits, …) | [Action entities](charger-control.md#the-action-entities) + automation |
| Other cars on the charger | [Shared grid connection](load-balancing.md) |
| `pyscript.ev_manual_add_public_charging_session` | `cable_juice_planner.manual_add_public_charging_session` |
| Update scripts and `shell_command` | HACS |

## Changes compared to the PyScript version

- No commands to the charger or car. See the [action entities](charger-control.md#the-action-entities).
- Load balancing between multiple cars with priority and main fuse.
- Consumption history is also stored without solar panels and Solcast.
- Public charging sessions are remembered across restarts.
- Optimal weekly charging: always enough for the day's driving, the rest only in the week's cheapest hours
  (instead of splitting the need in 3 parts).
- Hours without a price are filled from the price database, and the offline prices use the right weekday.
- A [fixed solar sell price](rules/solar.md#fixed-solar-sell-price) is treated as a settlement agreement
  (fixed price − spot price).
- The charging loss is measured continuously, and the dashboard has an [analysis](dashboard-analysis.md#analysis)
  of the economy and the driving.
- The last full charge is a sensor set by the integration (was an `input_datetime`).
- The same notification is sent at most once per hour.
- History is read from an in-memory buffer instead of 70+ recorder queries per planning run.
