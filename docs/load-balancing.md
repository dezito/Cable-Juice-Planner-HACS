# Multiple cars (load balancing)

If several cars charge on the same grid connection, together they can exceed the main fuse. A **Shared
grid connection** plans them together, so they never use more current per phase than allowed.

## Setup

**Add integration → Cable Juice Planner → Shared grid connection:**

| Field | Meaning |
|---|---|
| Main fuse L1/L2/L3 | the limit per phase (or the share the cars may use together) – default 25 A |
| Safety margin | subtracted from the fuse for short peaks (default 1 A) |
| Current/power L1/L2/L3 (optional) | the house's measured current (A) or power (W) per phase, e.g. from a HAN/P1 meter |
| Grid voltage | converts W to A |
| Trip soon | a trip within this many hours gives the car precedence (default 12) |

Then, for each car (under **Configure → Charger**, the *Load balancing* page):

| Field | Meaning |
|---|---|
| Priority | 1 = charges first |
| Phase order | which grid phases the charger's L1/L2/L3 are connected to: L1-L2-L3, L2-L3-L1 or L3-L1-L2. 1-phase charging uses the charger's L1, so the right grid phase is loaded |

Without phase sensors, the fuse is a fixed budget for the cars. With all three, the house's consumption is
subtracted, and an overloaded phase limits the last car at once.

## How it works

- **Order:** the cars are ordered by priority. A car below the daily minimum, or with a trip within *Trip
  soon*, goes first.
- **Planning:** each car gets a capacity per hour: the fuse minus the margin, minus the house's expected
  consumption (the average of the same hour over the last week, only with phase sensors), minus what the cars
  ahead have reserved. If there isn't room for full current, the car charges with fewer amps, and the rest
  is planned in other cheap hours.
- **1 or 3 phases:** the highest power that fits within the available current per phase is chosen. So a car
  can switch to 1 phase when a phase is full.
- **Live:** the amps are always limited to what is available right now. If a car ahead changes its plan or
  its current, the cars behind are recalculated after 15 seconds. With phase sensors, the car with the
  lowest priority is limited immediately if a phase exceeds the fuse.

**Example:** a 25 A fuse with 1 A margin, two cars with 3 × 16 A. Car 1 (priority 1) charges at 16 A. The
house uses 5 A on L1. Car 2 gets 25 − 1 − 5 − 16 = 3 A on L1 – too little, so it is planned in other hours
(or with fewer amps once car 1 is done).

## Sensors

| Sensor | Content |
|---|---|
| `sensor.cjp_site_available_l1` … `_l3` | available current per phase (the `cars` attribute shows the order and each car's request) |
| `sensor.cjp_site_house_l1` … `_l3` | the house's current per phase, excluding the cars |

The current per car is in its own [action entities](charger-control.md#the-action-entities) – the
blueprint needs no changes for load balancing.
