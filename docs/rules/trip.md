# 🧳 Trip

**Numbers:** *Trip charging target* (%), *Trip km needed* · **Date/time:** *EV trip departure*, *EV trip arrival* ·
**Switch:** *Trip preheat* · **Button:** *Reset trip charging*

For a longer drive outside the normal pattern.

## Planning a trip

1. Set **EV trip departure** and **EV trip arrival** in the future.
2. Set either **Trip charging target** (the battery level you want at departure) or **Trip km needed**
   (the km the trip needs – converted with the car's real consumption).
3. Optionally turn on **Trip preheat**.

The trip is planned automatically once these are set. What the trip needs is charged in the cheapest hours
before departure. Charging between the max. recommended charge limit and 100 % happens **just before
departure**, so the battery doesn't stand full for long.

The **trip arrival battery level** (default 25 %) is the level the car must still have at homecoming from the
trip.

## After the trip

The trip is reset automatically 2 hours after departure. **Reset trip charging** clears it at once.

## Together with other rules

A trip and a workday on the same day are added up. With [load balancing](../load-balancing.md), a car with
a trip soon (default within 12 hours) goes first.
