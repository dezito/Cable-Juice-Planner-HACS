# 💯 Full charge

**Number:** *Recommended full charge every* (days, `number.cjp_<prefix>_full_charge_recommended`, 0 = off) ·
**Sensor:** *Last full charge*

Some batteries need a full charge now and then – an LFP battery e.g. at least every 14 days, or as the car
maker says. The rest of the time the car stays below the max. recommended charge limit.

## How it works

- `sensor.cjp_<prefix>_last_full_charge` is set by CJP when the car reaches **99 %** or more. A cold battery
  may stop at 99 %, so that counts as full.
- When more days than *recommended full charge every* have passed, CJP plans a charge to 100 % in the
  cheapest hours until the end of tomorrow – above the max. recommended charge limit.
- Set the number to 0 to switch it off.
