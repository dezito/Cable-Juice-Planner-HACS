# 💸 Cheap power

**Switch:** *Charge on cheap power* · **Numbers:** *Charge level for very cheap electricity* (default 70 %),
*Charge level for ultra cheap electricity* (default 90 %)

When the power is cheap, it pays to charge more than the plan needs – the kWh will cost more later.

## When is it cheap?

CJP takes the **15 lowest prices** (the number of prices kept in the price database, setup → *Databases*) of the known hours of the week (the published prices, the forecast and the
hours filled from the [price database](../how-it-works.md#prices)) and calculates their average:

| Level | The hour's price | Charged to |
|---|---|---|
| Very cheap | at most that average | *charge level for very cheap electricity* (70 %) |
| Ultra cheap | at most 75 % of that average | *charge level for ultra cheap electricity* (90 %) |

**Example:** the 15 cheapest hours of the week average 0.80 kr/kWh. An hour at 0.75 kr is very cheap (charge to
70 %), an hour at 0.55 kr is ultra cheap (charge to 90 %).

The sensors `sensor.cjp_<prefix>_charge_very_cheap_battery_level` and `_charge_ultra_cheap_battery_level`
show the levels in use. They follow the expected solar surplus of the coming week: if the sun will charge
the car anyway, less is bought.

## When it applies

- **planned charge hours** at a very or ultra cheap price charge to that level instead of only what the rule needs –
  e.g. with [optimal weekly charging](weekly.md) or the [work plan](workplan.md)
- with **Charge on cheap power** on, the cheap hours are planned for the levels on their own – also without any other rule

The charge level never exceeds the max. recommended charge limit, unless a [trip](trip.md) or a
[full charge](full-charge.md) needs more.
