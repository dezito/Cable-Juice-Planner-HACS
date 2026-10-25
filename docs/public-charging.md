# Public charging

Needs an EV integration. Charges away from home are added to the charging history and the economy, so the
cost per km includes them.

## How it works

1. A charge away from home (the charge port or cable used while the car isn't home) is detected
   automatically: the time, the battery level at start and end, and the percentage added.
2. When it ends, *Public charging completed* (`binary_sensor.cjp_<prefix>_public_charging_session_done`)
   turns on, and you get a notification – with the mobile app you can enter the price from it.
3. Enter the **total price** of the charge (not the price per kWh) in *Cost of public charging*
   (`number.cjp_<prefix>_public_charging_session_cost`). Enter `-1` to ignore the charge.
4. About 1 minute later the charge is added to the history, and the field is reset.

With several charges, enter them in the order they happened. A charge waiting for its price is remembered
across restarts. On the dashboard, the public charging card is only shown while a charge waits.

## Adding a charge yourself

`cable_juice_planner.manual_add_public_charging_session` calculates – and optionally saves – a charge you
enter yourself, e.g. one that wasn't detected.
