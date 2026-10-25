# Learn driving distance

**Switch:** *Learn driving distance* (off by default) · needs an EV integration with an odometer

Instead of guessing the km per workday, CJP can learn them from the odometer.

## How it works

- The odometer is read every evening (23:59). A day's distance is the difference to the evening before.
- From the last **8 weeks** CJP suggests:

  | Number | Suggestion | Why |
  |---|---|---|
  | *km needed* per workday (work plan) | the **median** of that weekday | one long trip doesn't move it |
  | *Typical daily distance* (optimal weekly charging) | the **75th percentile** of all days | a buffer above a normal day |

- The suggestions are rounded up to 5 km and are always shown as the `learned_distance` attribute of the
  number entities – also with the switch off.
- With **Learn driving distance** on, the numbers are set to the suggestions once there are enough days
  (at least 14 days, and 3 of each weekday).

**Example:** the last 8 Tuesdays were 42, 44, 43, 120, 41, 45, 44 and 43 km. The median is 43.5 km, so
Tuesday's *km needed* becomes 45 km – the 120 km trip doesn't count.

Extra driving in a week is covered by the daily arrival battery level.
