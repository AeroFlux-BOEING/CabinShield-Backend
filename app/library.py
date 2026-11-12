"""Reference data shipped to the tablet before departure (works offline afterwards).

Seed JSON lives in `app/data/seed/` and mirrors the crew app's content:
schedule, response protocol (P1/P2/P3 to-dos and announcements), aircraft cabin layout,
service catalogue, map places/airports, past turbulence reports and the default forecast.
All strings are bilingual `{"ko": ..., "en": ...}`. Demo values only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from functools import lru_cache
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "seed"
KST = dt.timezone(dt.timedelta(hours=9))


def _read(name: str):
    return json.loads((SEED / name).read_text(encoding="utf-8"))


@lru_cache
def schedule() -> dict[str, dict]:
    return {f["flight_no"]: f for f in _read("schedule.json")}


@lru_cache
def protocol() -> dict:
    return _read("protocol.json")


def point_leads() -> list[float]:
    return [float(p["lead_s"]) for p in protocol()["points"]]


@lru_cache
def aircraft() -> dict:
    return _read("aircraft.json")


@lru_cache
def services() -> list[dict]:
    return _read("services.json")


@lru_cache
def places() -> dict:
    return _read("places.json")


def history(route_id: str) -> list[dict]:
    p = SEED / f"history_{route_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def default_hazards(route_id: str) -> list[dict]:
    p = SEED / f"hazards_{route_id}.json"
    return json.loads(p.read_text(encoding="utf-8"))["hazards"] if p.exists() else []


@lru_cache
def strength_labels() -> list[dict]:
    return json.loads((SEED / "hazards_ICN-SFO.json").read_text(encoding="utf-8"))["strength"]


@lru_cache
def reference_bundle() -> dict:
    body = {"protocol": protocol(), "aircraft": aircraft(), "services": services(), "places": places()["places"],
            "airports": places()["airports"], "strength": strength_labels()}
    version = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    return {"version": version, **body}


def normalise_flight_no(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())[:8]


def operates_on(f: dict, day: dt.date) -> bool:
    return (day.isoweekday() % 7) in f["days"]  # 0 = Sunday, like the app


def next_operating_date(f: dict, day: dt.date, horizon: int = 14) -> dt.date | None:
    for i in range(horizon):
        d = day + dt.timedelta(days=i)
        if operates_on(f, d):
            return d
    return None


def departure_utc(day: dt.date, dep_local: str) -> dt.datetime:
    """Scheduled departure (Korea time HH:MM on `day`) as an aware UTC datetime."""
    hh, mm = map(int, dep_local.split(":"))
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=KST).astimezone(dt.timezone.utc)
