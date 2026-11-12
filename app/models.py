"""API contract (Pydantic). The tablet can generate TypeScript types from GET /openapi.json.

Conventions
* `t_s`   — simulation/flight time in seconds since departure (same clock as the app's FlightSim)
* `*_nm`  — nautical miles along the route; `lon` is 0–360 °E like the app's map
* `Txt`   — bilingual string {"ko": ..., "en": ...}
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field


class Txt(BaseModel):
    ko: str
    en: str


# ------------------------------------------------------------------ schedule / reference

class ScheduledFlight(BaseModel):
    flight_no: str
    origin: str
    destination: str
    destination_name: Txt
    dep_local: str = Field(description="Scheduled departure, Korea time HH:MM")
    block_min: int
    aircraft: str
    aircraft_note: Txt
    days: list[int] = Field(description="Operating weekdays, 0 = Sunday")
    supported: bool = Field(description="Route available in this demo")
    route_id: str


class ScheduleLookup(BaseModel):
    flight: ScheduledFlight
    date: dt.date
    operates: bool
    next_operating_date: dt.date | None
    dep_utc: dt.datetime | None


# ------------------------------------------------------------------ flights

FlightStatus = Literal["SCHEDULED", "IN_FLIGHT", "ARRIVED"]
FeedMode = Literal["SIMULATOR", "AIRCRAFT_FEED"]


class FlightCreate(BaseModel):
    flight_no: str = Field(examples=["AFX017"])
    date: dt.date = Field(description="Departure date (Korea time)")
    dep_local: str | None = Field(None, pattern=r"^\d{2}:\d{2}$", description="Actual departure HH:MM KST, defaults to schedule")
    feed: FeedMode = "SIMULATOR"
    autostart: bool = True


class FlightOut(BaseModel):
    id: str
    flight_no: str
    date: dt.date
    route_id: str
    aircraft: str
    dep_local: str
    dep_utc: dt.datetime
    status: FlightStatus
    feed: FeedMode
    created_at: dt.datetime


class PositionReport(BaseModel):
    """Aircraft position from the AID / ACARS feed (or a test client)."""
    t_s: float | None = Field(None, description="Seconds since departure; defaults to wall clock − dep_utc")
    lat: float
    lon: float
    fl: float | None = None
    gs_kt: float | None = None
    track_deg: float | None = None


class SimControl(BaseModel):
    speed: float | None = Field(None, ge=1, le=600)
    aid: bool | None = Field(None, description="false = simulate loss of the aircraft position signal")
    sat: bool | None = Field(None, description="false = simulate loss of the ground (satellite) link")


# ------------------------------------------------------------------ forecast

class HazardIn(BaseModel):
    """Forecast turbulence area. Shape: 8 radii multipliers (N, NE, E, …) of r_nm. Rings: 100/66/33 % = light/moderate/severe."""
    id: str | None = None
    name: Txt
    fl: tuple[float, float] = Field(description="Flight level band, hundreds of ft")
    f: float = Field(ge=0, le=1, description="Reference point as a fraction of the route")
    off_nm: float = Field(0, description="Cross-track offset of the centre (nm, + = right of track)")
    r_nm: float = Field(gt=0, le=500)
    drift_kt: float = 15
    shape: list[float] = Field(default_factory=lambda: [1.0] * 8, min_length=8, max_length=8)
    # optional — live areas (SIGMET / PIREP clusters) or ops-drawn outlines
    source: str = "ops"
    centre_ll: dict | None = Field(None, description='Absolute centre {"lat","lon"} at t_obs_utc (overrides f/off_nm)')
    t_obs_utc: str | None = None
    vertices_nm: list[list[float]] | None = Field(None, description="Exact outline [[x_east, y_north], …] nm from the centre")
    floor: int = Field(0, ge=0, le=3, description="Minimum intensity anywhere inside (SEV SIGMET → 3)")
    drift_dir_deg: float = 90.0
    valid_from_utc: str | None = None
    valid_to_utc: str | None = None


class ForecastOut(BaseModel):
    route_id: str
    version: int
    issued_at: dt.datetime
    source: str
    hazards: list[dict[str, Any]]


# ------------------------------------------------------------------ sync (tablet → ground)

SyncKind = Literal["log", "checklist", "decision", "cabin", "point", "link"]


class DecisionPayload(BaseModel):
    features: list[float] = Field(min_length=38, max_length=38)
    q_low_s: float | None
    pick: int | Literal["FULL", "MINIMAL", "IMMEDIATE"]
    cst_s: list[float] | None = None
    csm_s: list[float] | None = None
    model_version: str | None = None


class SyncEvent(BaseModel):
    id: str = Field(min_length=6, max_length=64, description="Client-generated unique id (UUID) — makes uploads idempotent")
    kind: SyncKind
    t_s: float
    at: dt.datetime | None = Field(None, description="Device wall clock when it happened")
    payload: dict[str, Any] = Field(default_factory=dict)


class SyncBatch(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    forecast_version: int | None = Field(None, description="Forecast version the tablet currently holds")
    events: list[SyncEvent] = Field(max_length=500)


class SyncResult(BaseModel):
    accepted: int
    duplicates: int
    verdicts: list[dict[str, Any]]
    forecast_version: int
    forecast_stale: bool
    server_time: dt.datetime


# ------------------------------------------------------------------ reports & model

class TurbulenceReport(BaseModel):
    """Crew report after an encounter. Feeds the 'past similar reports' layer for later flights."""
    t_s: float
    severity: int = Field(ge=1, le=3, description="1 light · 2 moderate · 3 severe")
    d_nm: float | None = None
    lat: float | None = None
    lon: float | None = None
    fl: float | None = None
    note: str | None = Field(None, max_length=500)


class EvaluateIn(BaseModel):
    features: list[float] = Field(min_length=38, max_length=38)
    q_low_s: float | None = None
    model_version: str | None = None
