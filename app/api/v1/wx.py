"""Live aviation weather (NWS Aviation Weather Center): status, manual refresh, route-filtered data."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import manager, require_ops
from app.engine.flight import LIVE_SOURCES
from app.orchestrator import SessionManager

router = APIRouter(prefix="/wx", tags=["weather"])


def wx(request: Request):
    return request.app.state.wx


@router.get("/status", summary="Ingest mode, last fetch per route, last error")
def status(svc=Depends(wx)):
    return svc.status


@router.post("/refresh", dependencies=[Depends(require_ops)], summary="Fetch PIREP/AIREP + SIGMETs now and publish if changed")
async def refresh(route_id: str = "ICN-SFO", svc=Depends(wx)):
    try:
        return await svc.refresh(route_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, {"code": "AWC_UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"})


@router.get("/{route_id}/reports", summary="PIREP / AIREP turbulence reports within the route corridor")
def reports(route_id: str, m: SessionManager = Depends(manager), hours: float = Query(24, gt=0, le=720),
            min_ring: int = Query(0, ge=0, le=3), kind: str | None = Query(None, pattern="^(PIREP|AIREP|UUA)$")):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
    obs = [o for o in m.store.wx_observations(route_id, since, kind, min_ring) if o["kind"] != "SIGMET"]
    return {"route_id": route_id, "count": len(obs), "reports": obs}


@router.get("/{route_id}/areas", summary="Live hazards (SIGMET / PIREP clusters) in the current forecast")
def areas(route_id: str, m: SessionManager = Depends(manager)):
    fc = m.forecast(route_id)
    return {"route_id": route_id, "version": fc["version"],
            "areas": [h for h in fc["hazards"] if h.get("source") in LIVE_SOURCES]}
