"""Shared FastAPI dependencies: auth and session lookup."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.orchestrator import FlightSession, SessionManager


def manager(request: Request) -> SessionManager:
    return request.app.state.manager


def require_key(x_api_key: str | None = Header(None), s: Settings = Depends(get_settings)) -> None:
    """Tablet / console writes. Disabled when CS_API_KEY is empty (local dev)."""
    if s.api_key and x_api_key not in {s.api_key, s.ops_api_key or s.api_key}:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-API-Key")


def require_ops(x_api_key: str | None = Header(None), s: Settings = Depends(get_settings)) -> None:
    """Forecast ingest & ops controls. Uses CS_OPS_API_KEY when set, otherwise CS_API_KEY."""
    want = s.ops_api_key or s.api_key
    if want and x_api_key != want:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ops key required")


def session(flight_id: str, m: SessionManager = Depends(manager)) -> FlightSession:
    sess = m.get(flight_id)
    if sess is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"flight {flight_id} not found")
    return sess
