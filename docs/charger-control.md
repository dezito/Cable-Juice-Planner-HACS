# Charger and car control

CJP sends **no commands** to the charger or the car. It publishes what should happen on the action
entities, and your automation does it – easiest with the blueprint that comes with the integration.

## The action entities

| Entity | Content |
|---|---|
| `sensor.cjp_<prefix>_charger_action` | `charge` or `stop`: whether the charger should charge now |
| ↳ attribute `charger_enabled` | whether the charger should be enabled |
| ↳ attributes `phase_1_amps`, `phase_2_amps`, `phase_3_amps` | the charging current per phase in A (0 = phase not used) – so an automation only needs `charger_action` |
| ↳ attributes `circuit_phase_1_amps` … `_3_amps` | the circuit's limit per phase (e.g. Easee's circuit limit); on a [shared circuit](load-balancing.md#shared-circuit-eg-easee-master) the sum per grid phase of all its chargers |
| ↳ attribute `decision` | the action, enabled, mode, amps, circuit and charge limit in one text, e.g. `charge, enabled, mode normal, 16/16/16 A, circuit 24/24/24 A, limit 80 %` – trigger on it to run once per change |
| ↳ attribute `circuit_chargers` | only on a shared circuit: the prefixes of the cars on it |
| ↳ attribute `charging_limit` | the charge limit for the car in % |
| ↳ attribute `mode` | `normal` or `error` (the calculation failed) |
| ↳ attribute `updated` | the time of the latest calculation (heartbeat) |
| `sensor.cjp_<prefix>_charger_phase_1_amps` … `_3_amps` | the same current per phase as sensors |
| `sensor.cjp_<prefix>_charging_watts` | the wanted charging power |
| `sensor.cjp_<prefix>_charging_limit` | the wanted charge limit on the car in % (only with an EV integration) |
| `event.cjp_<prefix>_ev_request` | `wake_up`, `preheat_start` (data: `departure`, `defrost`) and `preheat_stop` |

## The blueprint

**Cable Juice Planner - charger control** is installed with the integration. It is placed in
`config/blueprints/automation/cable_juice_planner/charger_control.yaml` on startup and can be found under
*Settings → Automations & scenes → Blueprints*.

The file is never overwritten on its own. If a new version arrives with a HACS update (or you have edited
the file yourself), a repair shows up after restart under *Settings → System → Repairs*. If you choose to
update, the file is replaced with the bundled version, and automations using it are reloaded. If you ignore
it, you keep your own file. If the file is deleted, the latest version is put back on the next restart.

Create **one automation per car**. Under *Cable Juice Planner* select the car's **Charger action** – the
current per phase is read from its attributes.

### Charger and car

The blueprint has one section for the **charger** and one for the **car**. All actions are optional: fill in
each task where your setup can do it – in the charger, in the car or both (e.g. the current in the charger
and the charge limit in the car).

| Section | Inputs |
|---|---|
| Charger | Start charging, Stop charging, Set charging current, Set circuit current (master only), Enable the charger, Disable the charger |
| Car | Start charging in the car, Wait before starting the car, Stop charging in the car, Set charging current in the car, Set charge limit in the car |

On start: charge limit in the car → enable the charger → circuit limit → current in the charger → current in
the car → start the charger → start the car. On stop, both are stopped. If the car must wait until the charger
is ready, set **Wait before starting the car** (seconds). If one action fails (e.g. the charger's cloud doesn't
answer), the error is logged and the remaining actions still run.

The automation runs **once per change** of the decision (the `decision` attribute), however much changed:
a new action runs the whole start or stop, otherwise only what changed is sent (current, circuit, charge
limit, enable/disable). The trace's `changed` variable shows what it was, e.g. `['amps', 'circuit']`, so each
trace under *Traces* is one whole decision. A calculation that changes nothing doesn't run it.

In your actions you can use the variables `phase_1`, `phase_2`, `phase_3`, `max_phase`, `phases`,
`circuit_phase_1`, `circuit_phase_2`, `circuit_phase_3`, `charging_limit`, `charger_enabled`, `fail_safe` and
`message`. Use them rather than reading the sensors yourself – in fail-safe they hold the fail-safe current.

**Typical setups**

| Setup | Fill in | Example |
|---|---|---|
| The charger can be controlled, the car has no integration (or can't be controlled) | Charger | Easee, Zaptec, Monta, Wallbox or an OCPP charger with a virtual EV |
| The car can be controlled, the charger has no integration ("dumb" charger, optionally measured with an energy meter) | Car | VW/Cupra/Skoda, Kia/Hyundai, Tesla … on a charger without integration – e.g. measured with a Shelly 3EM |
| Both the charger and the car have an integration | Each task where it can be done | current and start/stop in the charger and charge limit in the car – or start/stop and charge limit in the car and current in the charger |

**Typical actions** – the names depend on the integration; find them under *Developer tools → Actions*
and *States*:

| Input | Charger integration | Car integration |
|---|---|---|
| Start / stop | a start/stop or pause/resume service (e.g. Easee `action_command`), a start/stop switch (e.g. Monta, Wallbox, OCPP *Charge Control*) or a button | a charge switch (e.g. Tesla/Tessie) or a start/stop service (e.g. Kia/Hyundai Connect `start_charge`/`stop_charge`, the VW/Cupra integrations) |
| Charging current | a dynamic limit service (e.g. Easee, Zaptec) or a number for max. current (e.g. OCPP *Maximum Current*, Wallbox *Max charging current*) – `{{ max_phase }}` or per phase `{{ phase_1 }}` … | only some cars have a number for the current (e.g. Tesla/Tessie *charge current*) – `{{ max_phase }}` |
| Charge limit | – | a number for the charge limit (e.g. *charge limit*, *AC charging limit*, *target state of charge*) – `{{ charging_limit }}` |
| Enable / disable | the charger's "enabled" switch – select the entity, not the device | – |

If neither the charger nor the car can set the current, the blueprint only starts and stops charging –
solar surplus charging and load balancing then can't regulate the current.

### Shared circuit

For chargers controlled through a circuit limit, e.g. Easee chargers behind a master. Fill in **Set circuit
current (master only)** **only in the automation of the car whose charger is the master**, and leave it empty in
the other cars' automations – otherwise several automations set the same circuit. Example for Easee:

```yaml
action: easee.set_circuit_dynamic_limit
data:
  device_id: <the master charger>
  current_p1: "{{ circuit_phase_1 }}"
  current_p2: "{{ circuit_phase_2 }}"
  current_p3: "{{ circuit_phase_3 }}"
```

Link the chargers under each car's *Load balancing* page (**Chargers on the same circuit**, see
[Shared circuit](load-balancing.md#shared-circuit-eg-easee-master)). Every car of the circuit then gets the sum
per grid phase of all the circuit's chargers, and the master's automation runs when it changes – also when
another car on the circuit starts or stops. The charging current of each charger (**Set charging current**) is
still set in each car's own automation. Without linked chargers the values are the car's own circuit limit. In
fail-safe the circuit gets the fail-safe current, which the circuit's chargers share.

### Charging state

Tells the blueprint whether the car is charging, so start isn't sent again and again:

| Input | Meaning |
|---|---|
| Status or "charging" entity | the charger's or the car's status sensor, or a switch/binary sensor that is `on` while charging |
| States that mean charging / done | e.g. `charging` and `completed`, `finished` (OCPP: `Charging`, `Finishing`) |
| Charging power + Charging above | a power sensor with a threshold (default 500 W) |

A new charging decision from CJP always sends start.

### Restart when not charging

Actions that run when the action has been `charge` for a while (**Restart after**, e.g. 10 minutes) without
charging – typically `homeassistant.reload_config_entry` targeting one of the charger's or the car's
entities, which reloads that integration. It is repeated after another 10 and 20 minutes (**Max number of
restarts**, default 3), you get a notification, and 1 minute later the current and start are sent again.
Needs *Charging state*.

### Fail-safe

If `charger_action` becomes unavailable, or its `updated` attribute is older than **Fail-safe after**
(default 20 min), the integration has stopped or hangs. The blueprint then charges with **Fail-safe charging
current** (default 16 A) on **Fail-safe phases** (default 3) and sends the **Notification**. The variable
`fail_safe` is `true` in your actions.

## Your own automations

Without the blueprint, e.g. the charge limit and the car requests:

```yaml
alias: CJP - charge limit
triggers:
  - trigger: state
    entity_id: sensor.cjp_charging_limit
conditions:
  - "{{ trigger.to_state.state | is_number }}"
actions:
  - action: number.set_value
    target:
      entity_id: number.my_car_charge_limit
    data:
      value: "{{ trigger.to_state.state | int }}"
---
alias: CJP - wake and preheat the car
triggers:
  - trigger: state
    entity_id: event.cjp_ev_request
actions:
  - choose:
      - conditions: "{{ trigger.to_state.attributes.event_type == 'wake_up' }}"
        sequence:
          - action: button.press
            target:
              entity_id: button.my_car_wake
      - conditions: "{{ trigger.to_state.attributes.event_type == 'preheat_start' }}"
        sequence:
          - action: climate.turn_on
            target:
              entity_id: climate.my_car_climate
      - conditions: "{{ trigger.to_state.attributes.event_type == 'preheat_stop' }}"
        sequence:
          - action: climate.turn_off
            target:
              entity_id: climate.my_car_climate
```
