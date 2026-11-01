# Recipes

Settings for typical setups.

## Commuter with fixed workdays

- [Work plan](rules/workplan.md) on, with km, departure and homecoming per workday.
- [Learn driving distance](learn-distance.md) on after a few weeks, so the km follow reality.
- [Forced charging below daily target](rules/manual-forced.md#-forced-charging-below-the-daily-battery-level)
  on as a safety net.
- [Charge on cheap power](rules/cheap-power.md) on, if you want the car filled when it is really cheap.

## No fixed pattern, or on holiday

- Work plan off, [optimal weekly charging](rules/weekly.md) on, with a typical daily distance a little above
  a normal day.
- The car is always ready for a normal day, and filled up in the week's cheapest hours.

## Without a car integration (virtual EV)

- Skip the EV page in the setup.
- When you plug in, set *Virtual EV battery level* to the car's level (the dashboard shows the field).
- *Completed charging battery level* is where the car stops, and *Virtual EV max range* its realistic range.
- The charger must be controllable – the blueprint's **charger** section.

Without a car integration there is no charge limit for the car, no public charging and no continuous
charging loss measurement.

## Solar panels with the hourly sell price

- [Solar surplus charging](rules/solar.md) on, *Fixed solar sell price* −1.
- *Enable selling during expensive hours* on (default): in the day's 4 most expensive hours the surplus is
  sold instead.
- The car's charger must be able to set the current (charger or car), otherwise the surplus can't be followed.

## Solar panels with a fixed sell price

Your production agreement pays a fixed price and settles the difference to the spot price, so a sold kWh is
worth `fixed price − spot price` – negative when the spot price is above the fixed price.

- Setup: *Fixed solar sell price* = the fixed price. See [Fixed solar sell price](rules/solar.md#fixed-solar-sell-price).
- Selling during expensive hours is off automatically – charging the car with the surplus saves money there.

## Together with BattMind (home battery)

The home battery shouldn't be drained into the car – unless there is energy to spare.

1. CJP setup → **House and home battery:** the home battery's level and power.
2. CJP settings: *Charge EV after home battery minimum level* (e.g. 80 %) and, if the car may use the
   home battery's surplus, *Home battery surplus to EV*.
3. BattMind setup → **Consumption → Ignored consumers:** the charger's power sensor.
4. [BattMind's blueprint](https://github.com/dezito/BattMind-Integration/blob/main/docs/actions.md#ev-optional) →
   **EV (optional)**: the charger's power sensor, *While the car is charging* (discharge power 0), *Allow
   discharging into the car when* `switch.cjp_<car>_powerwall_discharge_above_needed`, and *Only above this
   battery level* `number.cjp_<car>_ev_charge_after_powerwall_battery_level`.

## Two cars on one main fuse

Add a [shared grid connection](load-balancing.md), give each car a priority and the right phase order, and
add the house's phase sensors (HAN/P1) if you have them.

## A charger with a slow kWh meter (e.g. Easee)

Make a *Riemann sum integral* helper (method *Left*) of the charger's power sensor, and use it as the kWh
meter in the setup. The charging history and the [charging loss](charging-loss.md) then follow the charge
minute by minute.
