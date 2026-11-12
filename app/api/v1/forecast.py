"""Turbulence forecast per route (ops ingest → versioned → uplinked to aircraft on the route)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import manager, require_ops
from app.api.v1._util import cached_json
from app.engine.geo import get_route
from app.models import ForecastOut, HazardIn
from app.orchestrator import SessionManager

router = APIRouter(prefix="/forecast", tags=["forecast"])


def _route(route_id: str):
    try:
        return get_route(route_id)
    except (KeyError, ValueError):
        raise HTTPException(404, f"unknown route {route_id}")


@router.get("/{route_id}", response_model=ForecastOut, summary="Latest forecast; returns 204 if the tablet already holds it")
def latest(route_id: str, request: Request, m: SessionManager = Depends(manager), since: int | None = Query(None, description="Version the client holds")):
    _route(route_id)
    fc = m.forecast(route_id)
    if since is not None and since >= fc["version"]:
        from fastapi import Response
        return Response(status_code=204)
    return cached_json(request, fc, etag=f'"{route_id}-v{fc["version"]}"', max_age=30)


@router.put("/{route_id}", response_model=ForecastOut, dependencies=[Depends(require_ops)], summary="Replace the whole forecast")
def replace(route_id: str, hazards: list[HazardIn], m: SessionManager = Depends(manager), source: str = "ops"):
    _route(route_id)
    items = []
    for i, h in enumerate(hazards):
        d = h.model_dump(exclude_none=True)
        d["id"] = d.get("id") or f"HZ{i + 1}"
        d["fl"] = list(d["fl"])
        items.append(d)
    return m.publish_forecast(route_id, items, source)


@router.post("/{route_id}/hazards", response_model=ForecastOut, status_code=201, dependencies=[Depends(require_ops)],
             summary="Add or update one hazard (by id) → new version, pushed to flights on the route")
def upsert_hazard(route_id: str, h: HazardIn, m: SessionManager = Depends(manager), source: str = "ops"):
    _route(route_id)
    cur = m.forecast(route_id)["hazards"]
    d = h.model_dump(exclude_none=True)
    d["fl"] = list(d["fl"])
    d["id"] = d.get("id") or f"HZ{len(cur) + 1}"
    items = [x for x in cur if x["id"] != d["id"]] + [d]
    return m.publish_forecast(route_id, items, source)


@router.delete("/{route_id}/hazards/{hazard_id}", response_model=ForecastOut, dependencies=[Depends(require_ops)])
def delete_hazard(route_id: str, hazard_id: str, m: SessionManager = Depends(manager)):
    _route(route_id)
    cur = m.forecast(route_id)["hazards"]
    if not any(x["id"] == hazard_id for x in cur):
        raise HTTPException(404, f"hazard {hazard_id} not in the current forecast")
    return m.publish_forecast(route_id, [x for x in cur if x["id"] != hazard_id], "ops")
