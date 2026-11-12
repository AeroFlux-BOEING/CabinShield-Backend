"""Reference data: schedule, protocol content, routes, past turbulence reports."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import library
from app.api.deps import manager
from app.api.v1._util import cached_json
from app.engine.flight import PLAN_GS, START_NM, flight_level
from app.engine.geo import get_route
from app.models import ScheduledFlight, ScheduleLookup
from app.orchestrator import SessionManager

router = APIRouter(tags=["reference"])


@router.get("/reference", summary="Offline reference bundle (protocol, cabin layout, services, map labels)")
def reference(request: Request):
    b = library.reference_bundle()
    return cached_json(request, b, etag=f'"{b["version"]}"', max_age=3600)


@router.get("/schedule", response_model=list[ScheduledFlight])
def schedule():
    return list(library.schedule().values())


@router.get("/schedule/{flight_no}", response_model=ScheduleLookup, summary="Auto-fill route from a flight number")
def schedule_lookup(flight_no: str, date: dt.date | None = Query(None, description="Departure date (KST), default today")):
    f = library.schedule().get(library.normalise_flight_no(flight_no))
    if not f:
        raise HTTPException(404, {"code": "UNKNOWN_FLIGHT", "msg": {"ko": "스케줄에 없는 편명입니다", "en": "Flight number not in the schedule"}})
    day = date or dt.datetime.now(library.KST).date()
    ops = library.operates_on(f, day)
    return {"flight": f, "date": day, "operates": ops, "next_operating_date": library.next_operating_date(f, day),
            "dep_utc": library.departure_utc(day, f["dep_local"]) if ops else None}


@router.get("/routes/{route_id}", summary="Route geometry and vertical profile")
def route(route_id: str, request: Request, step_nm: float = Query(50, ge=5, le=500)):
    try:
        r = get_route(route_id)
    except (KeyError, ValueError):
        raise HTTPException(404, f"unknown route {route_id}")
    prof = [{"d_nm": round(d, 1), "fl": round(flight_level(r, d))} for d in [r.length_nm * i / 40 for i in range(41)]]
    body = {"route_id": r.id, "origin": r.a.as_dict(), "destination": r.b.as_dict(), "length_nm": round(r.length_nm, 3),
            "plan_gs_kt": PLAN_GS, "demo_start_nm": START_NM, "polyline": r.polyline(step_nm), "profile": prof}
    return cached_json(request, body, max_age=86400)


SEV_TXT = [{"ko": "", "en": ""}, {"ko": "약함", "en": "light"}, {"ko": "보통", "en": "moderate"}, {"ko": "심함", "en": "severe"}]


def merged_history(m: SessionManager, route_id: str, days: int = 30) -> list[dict]:
    """Seed history (extracted from NWS / PIREP / AIREP) + live reports kept by the wx ingest + crew reports.

    Points within 50 nm of a seed segment add to its case count; the rest are grouped into 'recent' segments.
    """
    hist = [dict(h, recent_reports=0, sources={"seed": h["cases"]}) for h in library.history(route_id)]
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    points = [{"d_nm": o["d_nm"], "fl": o.get("fl"), "ring": o["ring"], "src": o["kind"], "at": o["obs_utc"]}
              for o in m.store.wx_observations(route_id, since, min_ring=1) if o.get("d_nm") is not None and o["kind"] != "SIGMET"]
    points += [{"d_nm": r["d_nm"], "fl": r["fl"], "ring": r["severity"], "src": "CREW", "at": r["created_at"]}
               for r in m.store.reports(route_id) if r["d_nm"] is not None]
    loose = []
    for p in sorted(points, key=lambda x: x["d_nm"]):
        seg = next((h for h in hist if h["d0_nm"] - 50 <= p["d_nm"] <= h["d1_nm"] + 50), None)
        if seg:
            seg["cases"] += 1
            seg["recent_reports"] += 1
            seg["sources"][p["src"]] = seg["sources"].get(p["src"], 0) + 1
        else:
            loose.append(p)
    groups: list[list[dict]] = []
    for p in loose:
        if groups and p["d_nm"] - groups[-1][-1]["d_nm"] <= 80:
            groups[-1].append(p)
        else:
            groups.append([p])
    for g in groups:
        fls = [p["fl"] for p in g if p["fl"]]
        ring = max(p["ring"] for p in g)
        srcs: dict = {}
        for p in g:
            srcs[p["src"]] = srcs.get(p["src"], 0) + 1
        hist.append({"id": f"R{int(g[0]['d_nm'])}", "d0_nm": max(0.0, g[0]["d_nm"] - 40), "d1_nm": g[-1]["d_nm"] + 40,
                     "cases": len(g), "recent_reports": len(g), "sources": srcs,
                     "fl_band": [min(fls) - 20, max(fls) + 20] if fls else None,
                     "name": {"ko": "최근 보고 (PIREP·AIREP·승무원)", "en": "Recent reports (PIREP · AIREP · crew)"},
                     "pattern": {"ko": "최근 30일 보고", "en": "Reports in the last 30 days"},
                     "severity": {"ko": f"최대 {SEV_TXT[ring]['ko']}", "en": f"up to {SEV_TXT[ring]['en']}"},
                     "season": {"ko": max(p["at"] for p in g)[:10], "en": max(p["at"] for p in g)[:10]}})
    return sorted(hist, key=lambda h: h["d0_nm"])


@router.get("/routes/{route_id}/history", summary="Past similar turbulence reports along the route")
def route_history(route_id: str, request: Request, m: SessionManager = Depends(manager)):
    return cached_json(request, {"route_id": route_id, "segments": merged_history(m, route_id)})


@router.get("/routes/{route_id}/reports", summary="Raw crew turbulence reports")
def route_reports(route_id: str, m: SessionManager = Depends(manager)):
    return m.store.reports(route_id)
