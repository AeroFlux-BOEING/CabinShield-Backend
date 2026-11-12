"""Operations console views."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import manager
from app.orchestrator import SessionManager

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/overview", summary="All active flights: point stage, TTA, links, last tablet sync, AI agreement")
def overview(m: SessionManager = Depends(manager)):
    return {"flights": m.overview()}
