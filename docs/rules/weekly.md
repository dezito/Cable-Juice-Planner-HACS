# 📅 Optimal weekly charging

**Switch:** *Optimal weekly charging (no Work Plan)* (`switch.cjp_<prefix>_fill_up`) · **Number:** *Typical daily distance (Non-workday)* (default 50 km)

Without a fixed pattern – or on holiday – CJP keeps the car ready for a normal day and fills it up when the
power is cheapest during the week.

## How it plans

Two levels every day:

1. **The floor – always:** enough for the **typical daily distance** plus the **daily arrival battery level**,
   charged in the day's cheapest hours.
2. **The top – only when cheap:** the rest up to the **EV max recommended charging limit** (e.g. 90 %) is only
   charged in hours that are
   - among the **cheapest 30 %** of all the known hours of the week, and
   - not more than 10 % more expensive than the cheapest hour until the end of the next day. If tomorrow
     night is clearly cheaper, the car waits for it.

**Example:** typical daily distance 40 km ≈ 10 %, daily minimum 30 %. The car always has at least 40 %.
Wednesday night the price is among the week's cheapest, and Thursday is not cheaper: the car is charged to
90 % Wednesday night.

The known hours are the published prices, the price forecast, and the hours CJP fills from its own
[price database](../how-it-works.md#prices).

## Together with other rules

- **Work plan:** when the work plan is also on, workdays follow the work plan and the other days the typical
  daily distance. Turn the work plan off on holiday, and optimal weekly charging covers every day.
- **Cheap power:** with optimal weekly charging, the [cheap power](cheap-power.md) levels apply automatically.
- **Full charge:** a [full charge](full-charge.md) is planned the same way, in the cheap hours.
- The typical daily distance can be [learned from the odometer](../learn-distance.md).
