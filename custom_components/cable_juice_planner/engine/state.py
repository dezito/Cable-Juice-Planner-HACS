"""The data handed to and returned from the engine worker."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Snapshot:
    """Plain data collected on the event loop for one worker job."""

    now: datetime.datetime
    tz: Any
    settings: dict[str, Any] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, dict] = field(default_factory=dict)
    platforms: dict[str, str | None] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    buffers: dict[str, list] = field(default_factory=dict)
    statistics: dict[str, list] = field(default_factory=dict)
    price_data: dict | None = None
    forecast_dict: dict | None = None
    solcast: dict[str, list] = field(default_factory=dict)
    astral: Any = None  # astral.Observer
    site: dict | None = None  # load balancing, see engine/site


@dataclass
class JobResult:
    """Everything a worker job wants the event loop to publish."""

    states: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    setting_limits: dict[str, dict[str, float]] = field(default_factory=dict)
    events: list[tuple[str, dict]] = field(default_factory=list)
    notifications: list[tuple[str, str, str]] = field(default_factory=list)
    mobile_notifications: list[dict] = field(default_factory=list)
    db_saves: dict[str, Any] = field(default_factory=dict)
    config_updates: dict[str, Any] = field(default_factory=dict)
    value: Any = None
    error: BaseException | None = None
    request_reload: bool = False
    site: dict | None = None  # reservation / request / urgent for the site
    worker_thread: str = ""
    duration: float = 0.0
