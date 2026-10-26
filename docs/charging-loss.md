# Charging loss

The charging loss is what is lost between the charger and the car's battery. It is not constant: the car's
electronics use a fixed amount while it charges, so a low power (solar on 1 phase at 6 A) loses much more
than 3 × 16 A.

```
loss = fixed use (the car's electronics) ÷ power + the charger's loss
```

## Measured continuously

Needs an EV integration (the car's battery level). From the battery level and the charger's kWh meter in
Home Assistant's history (the last days raw, before that the hourly statistics), CJP finds parts of a
charge from one change of the battery level to a later one (at least 5 %). The power may vary during a
part, as it does with solar charging, but the charging phase count must remain the same.

- Per power group (< 2.5 kW, 2.5–4 kW, 4–7 kW, > 7 kW) the newest 10 parts are kept, reading only as far
  back as a group needs.
- With measurements in two or more groups, the model separates the fixed use from the charger's own loss.
- When a model can be fitted across multiple power groups and there are at least six measurements, a small
  number of clear outliers (more than 5 percentage points from the fitted curve) are excluded from the model,
  group averages, and power chart. They remain available in the sensor's `rejected_segments` attribute.

| Entity | Content |
|---|---|
| `sensor.cjp_<prefix>_charging_loss_model` | the loss at the max. charging power. The attributes hold the measurements, the model and **kWh from the charger per 1 %** |
| *Use measured charging loss* (switch, off by default) | the plan uses the measured loss instead of the configured one – per hour at that hour's power when there is a model, else the measured loss at the max. power |
| *Recalculate charging loss* (button) | forgets the measurements and reads the history again |

**kWh from the charger per 1 %** is what the plan really needs, and it doesn't depend on the battery size:
if the battery size is set too high or too low, only the loss percentage moves – the plan still fits.

**Example:** with 300 W fixed use and 3 % charger loss, charging at 11 kW loses 0.3 ÷ 11 + 3 % ≈ 6 %, while
1-phase solar charging at 1.4 kW loses 0.3 ÷ 1.4 + 3 % ≈ 24 %.

The [analysis](dashboard-analysis.md#charging-loss-per-power) shows the measurements and the model per power.

## One-time measurement

**Switch:** *Calculate charging loss*

Measures one charge from low to full and saves the result in the configuration. Best result:

1. Discharge the car as much as possible (max. 20 %).
2. Set the wanted charge limit in the car and the max. recommended charge limit (at least 90 %).
3. Turn on *Calculate charging loss* and let the car report when it is full.

The charge has to add at least 30 percentage points. After the charge, CJP waits up to 30 minutes for a
slow kWh meter, saves the result and sends a notification.
