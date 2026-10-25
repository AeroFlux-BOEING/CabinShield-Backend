from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.engine.tta import tta_estimate
from app.kernel import SafetyKernel, immediate_self_protect_plan
from app.library import library_as_dict
from app.models import AdvisoryIn, CabinState, Decision, EvaluateRequest, TtaSource
from app.orchestrator import FlightSession, SessionManager
from app.scenarios import SCENARIOS

router = APIRouter(prefix="/api/v1")
ws_router = APIRouter()


# ---------------------------------------------------------------- deps
def require_key(x_api_key: Optional[str] = Header(default=None), settings: Settings = Depends(get_settings)):
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(401, "invalid api key")


def manager(request: Request) -> SessionManager:
    return request.app.state.manager


def session_or_404(flight_id: str, mgr: SessionManager = Depends(manager)) -> FlightSession:
    s = mgr.get(flight_id)
    if not s:
        raise HTTPException(404, f"flight {flight_id} not found")
    return s


# ---------------------------------------------------------------- reference
@router.get("/library")
def get_library():
    return library_as_dict()


@router.get("/scenarios")
def get_scenarios():
    return {k: {"initial_tta_s": v["initial_tta_s"], "cabin": v["cabin"]().model_dump(mode="json")}
            for k, v in SCENARIOS.items()}


# ---------------------------------------------------------------- stateless
@router.post("/evaluate", response_model=Decision, dependencies=[Depends(require_key)])
async def evaluate(req: EvaluateRequest, request: Request, settings: Settings = Depends(get_settings)):
    """세션 없이 1회 평가 (콘솔/테스트/발표 슬라이드용)."""
    mgr: SessionManager = request.app.state.manager
    kernel = SafetyKernel(settings)
    tta = tta_estimate(req.tta_mean_s, req.tta_sigma_s, settings.tta_quantile_low,
                       source=TtaSource.OPS_FEED, advisory_id="ADHOC")
    fail = kernel.fail_safe_reasons(req.cabin, tta)
    buf = kernel.buffer_for(fail)
    plans = await mgr.planner.propose(req.cabin, tta.tta_low_s)
    plans.append(immediate_self_protect_plan(req.cabin))
    evals = [kernel.evaluate_plan(req.cabin, p, tta, buf) for p in plans]
    return kernel.decide("ADHOC", req.cabin, tta, evals, fail, buf)


# ---------------------------------------------------------------- flights
class CreateFlight(BaseModel):
    scenario: Optional[str] = "A320_HOT_BEVERAGE"
    cabin: Optional[CabinState] = None
    initial_tta_s: Optional[float] = None     # None이면 시나리오 기본값, 0이면 시뮬레이터 없음(OPS_FEED만)
    flight_id: Optional[str] = None
    autostart: bool = False


@router.post("/flights", dependencies=[Depends(require_key)], status_code=201)
async def create_flight(body: CreateFlight, mgr: SessionManager = Depends(manager)):
    preset = SCENARIOS.get(body.scenario or "")
    if body.cabin is None and preset is None:
        raise HTTPException(400, "cabin 또는 유효한 scenario가 필요합니다")
    cabin = body.cabin or preset["cabin"]()
    tta0 = body.initial_tta_s if body.initial_tta_s is not None else (preset or {}).get("initial_tta_s")
    if body.flight_id and mgr.get(body.flight_id):
        raise HTTPException(409, "flight_id already exists")
    sess = mgr.create(cabin, tta0 or None, body.flight_id)
    if body.autostart:
        sess.start()
    return sess.snapshot()


@router.get("/flights")
def list_flights(mgr: SessionManager = Depends(manager)):
    return [{"flight_id": s.id, "phase": s.phase, "sim_running": s.running} for s in mgr.sessions.values()]


@router.get("/flights/{flight_id}")
def get_flight(sess: FlightSession = Depends(session_or_404)):
    return sess.snapshot()


@router.delete("/flights/{flight_id}", dependencies=[Depends(require_key)], status_code=204)
async def delete_flight(flight_id: str, mgr: SessionManager = Depends(manager)):
    await mgr.remove(flight_id)


@router.put("/flights/{flight_id}/cabin-state", dependencies=[Depends(require_key)])
async def put_cabin_state(cabin: CabinState, sess: FlightSession = Depends(session_or_404)):
    """승무원 태블릿: 서비스 단계·카트·화장실 상태 갱신 (난기류 이전부터 평소에 갱신)."""
    await sess.update_cabin(cabin)
    return sess.snapshot()


@router.post("/flights/{flight_id}/advisories", dependencies=[Depends(require_key)])
async def post_advisory(adv: AdvisoryIn, sess: FlightSession = Depends(session_or_404)):
    """운항계층(조종실/OCC) → CabinShield 난기류 위험 이벤트 ingest. 승무원 UI용 아님."""
    await sess.ingest_advisory(adv)
    sess.start()  # TTA 감소 tick
    return sess.snapshot()


@router.get("/flights/{flight_id}/decision")
def get_decision(sess: FlightSession = Depends(session_or_404)):
    return sess.decision.model_dump(mode="json") if sess.decision else {"phase": sess.phase, "decision": None}


@router.post("/flights/{flight_id}/sim/start", dependencies=[Depends(require_key)])
def sim_start(sess: FlightSession = Depends(session_or_404)):
    if not sess.sim and not sess._ext_adv:
        raise HTTPException(400, "simulator not configured for this flight")
    sess.start()
    return {"running": True}


@router.post("/flights/{flight_id}/sim/stop", dependencies=[Depends(require_key)])
async def sim_stop(sess: FlightSession = Depends(session_or_404)):
    await sess.stop()
    return {"running": False}


@router.post("/flights/{flight_id}/sim/step", dependencies=[Depends(require_key)])
async def sim_step(seconds: float = Query(1.0, gt=0, le=60), sess: FlightSession = Depends(session_or_404)):
    """발표용 수동 진행(일시정지 상태에서 N초 전진)."""
    await sess.tick(seconds)
    return sess.snapshot()


@router.get("/flights/{flight_id}/audit")
async def get_audit(limit: int = Query(100, le=1000), sess: FlightSession = Depends(session_or_404)):
    return await sess.audit.read(sess.id, limit)


# ---------------------------------------------------------------- websocket
@ws_router.websocket("/ws/flights/{flight_id}")
async def ws_flight(ws: WebSocket, flight_id: str, token: Optional[str] = None):
    settings = get_settings()
    if settings.api_key and token != settings.api_key:
        await ws.close(code=4401)
        return
    mgr: SessionManager = ws.app.state.manager
    sess = mgr.get(flight_id)
    if not sess:
        await ws.close(code=4404)
        return
    await ws.accept()
    q = sess.subscribe()
    try:
        await ws.send_json({"type": "state", "data": sess.snapshot()})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                msg = {"type": "ping"}   # 모바일 망/프록시 idle 끊김 방지
            await ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sess.unsubscribe(q)
