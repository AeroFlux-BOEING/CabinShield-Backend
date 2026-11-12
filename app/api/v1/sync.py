"""Tablet → ground sync. The tablet queues events while offline and uploads them in batches.

Uploads are idempotent (client event ids). Every `decision` event is re-checked by the ground
Safety Kernel and the verdict is stored in the audit trail and returned to the tablet.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import manager, require_key, session
from app.config import Settings, get_settings
from app.kernel import verify_client_decision
from app.models import DecisionPayload, SyncBatch, SyncResult
from app.orchestrator import FlightSession, SessionManager
from app.store import now_utc

router = APIRouter(prefix="/flights", tags=["sync"])


@router.post("/{flight_id}/sync", response_model=SyncResult, dependencies=[Depends(require_key)])
def sync(batch: SyncBatch, sess: FlightSession = Depends(session), m: SessionManager = Depends(manager), s: Settings = Depends(get_settings)):
    accepted = dup = 0
    verdicts = []
    for e in sorted(batch.events, key=lambda x: x.t_s):
        ed = e.model_dump()
        if e.kind == "decision":
            DecisionPayload.model_validate(e.payload)  # 422 on a malformed decision
        if not m.store.add_sync_event(sess.id, batch.device_id, ed):
            dup += 1
            continue
        accepted += 1
        sess.apply_tablet_event(ed, batch.device_id)
        if e.kind == "decision":
            v = verify_client_decision(e.payload, s.safety_buffer_s)
            m.store.add_decision_audit(e.id, sess.id, e.t_s, v)
            verdicts.append({"event_id": e.id, "agree": v["agree"], "client_pick": v["client_pick"],
                             "server_pick": v["server_pick"], "flags": v["flags"]})
    if batch.forecast_version is not None:
        sess.tablet["forecast_version"] = batch.forecast_version
    if accepted:
        sess.broadcast({"type": "event", "event": "SYNC_RECEIVED",
                        "data": {"accepted": accepted, "device_id": batch.device_id, "verdicts": verdicts}})
    return {"accepted": accepted, "duplicates": dup, "verdicts": verdicts, "forecast_version": sess.forecast_version,
            "forecast_stale": batch.forecast_version is not None and batch.forecast_version < sess.forecast_version,
            "server_time": now_utc()}


@router.get("/{flight_id}/sync-events", summary="What the tablet uploaded (newest first)")
def sync_events(sess: FlightSession = Depends(session), m: SessionManager = Depends(manager),
                kind: str | None = None, limit: int = Query(200, le=1000)):
    return m.store.sync_events(sess.id, kind, limit)


@router.get("/{flight_id}/audit", summary="Ground re-check of every tablet AI decision")
def audit(sess: FlightSession = Depends(session), m: SessionManager = Depends(manager), limit: int = Query(200, le=1000)):
    return {"summary": m.store.audit_summary(sess.id), "items": m.store.decision_audit(sess.id, limit)}
