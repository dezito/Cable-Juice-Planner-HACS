from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Site (shared grid connection): load balancing between the cars
# --------------------------------------------------------------------------- #
CONF_SITE = "site"
SITE_TITLE = "Cable Juice Planner – el-tilslutning"
# charger L1/L2/L3 -> grid phase index
PHASE_ORDERS: dict[str, tuple[int, int, int]] = {
    "L1-L2-L3": (0, 1, 2),
    "L2-L3-L1": (1, 2, 0),
    "L3-L1-L2": (2, 0, 1),
}
SITE_DEFAULTS: dict[str, Any] = {
    "fuse_l1": 25.0,
    "fuse_l2": 25.0,
    "fuse_l3": 25.0,
    "margin": 1.0,
    "voltage": 230.0,
    "phase_l1_entity_id": "",
    "phase_l2_entity_id": "",
    "phase_l3_entity_id": "",
    "urgent_trip_hours": 12,
}
SITE_FIELDS = list(SITE_DEFAULTS)
# chargers sharing one circuit limit (e.g. an Easee master): lists of HA device ids in the
# site options, chosen on each car's load balancing page
CONF_CIRCUITS = "circuits"
CONF_CIRCUIT = "site_circuit"
# the car's charger entities; their devices tell which charger the car uses
CIRCUIT_ENTITY_KEYS = (
    "status_entity_id",
    "power_consumtion_entity_id",
    "kwh_meter_entity_id",
    "lifetime_kwh_meter_entity_id",
    "enabled_entity_id",
    "cable_connected_entity_id",
)
SITE_HOURS_AHEAD = 24 * 8
SITE_SENSORS = ("site_available_l1", "site_available_l2", "site_available_l3", "site_house_l1", "site_house_l2", "site_house_l3")
SIGNAL_SITE_UPDATE = "cable_juice_planner_site_update"
