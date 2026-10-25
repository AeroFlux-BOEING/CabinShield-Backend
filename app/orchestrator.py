"""비행 세션 오케스트레이터.

TTA 수신(시뮬레이터 또는 OPS_FEED) → Planner 후보 생성 → Safety Kernel 검증 → Decision
→ WebSocket 구독자(승무원 태블릿/콘솔)에게 푸시.

성능: 객실상태가 바뀔 때만 후보 생성 + CST 몬테카를로를 다시 돌리고,
TTA만 바뀌는 매 tick에는 캐시된 CST로 시간검증(CSM)만 갱신한다.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from app.config import Settings
from app.engine.tta import FlightSimulator, tta_estimate
from app.kernel import SafetyKernel, immediate_self_protect_plan
from app.models import AdvisoryIn, CabinState, Decision, PlanEvaluation, TtaEstimate, TtaSource, utcnow
from app.store import AuditStore

log = logging.getLogger("cabinshield.orchestrator")


class FlightSession:
    def __init__(self, flight_id: str, cabin: CabinState, settings: Settings, planner, kernel: SafetyKernel,
                 audit: AuditStore, sim: Optional[FlightSimulator] = None):
        self.id = flight_id
        self.s = settings
        self.planner = planner
        self.kernel = kernel
        self.audit = audit
        self.cabin = cabin
        self.cabin_version = 0
        self.sim = sim
        self.tta: Optional[TtaEstimate] = None
        self._ext_adv: Optional[tuple[AdvisoryIn, datetime]] = None
        self._evals: Optional[list[PlanEvaluation]] = None
        self._evals_key: Optional[tuple[int, float]] = None
        self.decision: Optional[Decision] = None
        self.phase = "MONITORING"   # MONITORING | ADVISORY | IN_TURBULENCE | CLEARED
        self.subscribers: set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ pub/sub
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _publish(self, msg: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            if q.full():  # 느린 클라이언트: 오래된 메시지 버림
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    def snapshot(self) -> dict:
        return {
            "flight_id": self.id,
            "phase": self.phase,
            "sim": self.sim.snapshot() if self.sim else None,
            "sim_running": self.running,
            "cabin": self.cabin.model_dump(mode="json"),
            "decision": self.decision.model_dump(mode="json") if self.decision else None,
        }

    async def _event(self, kind: str, data: dict) -> None:
        self._publish({"type": "event", "event": kind, "data": data})
        await self.audit.log(utcnow().isoformat(), self.id, kind, data)

    # ------------------------------------------------------------ inputs
    async def update_cabin(self, cabin: CabinState) -> None:
        async with self._lock:
            cabin.updated_at = utcnow()
            self.cabin = cabin
            self.cabin_version += 1
            self._evals = None
        await self.audit.log(utcnow().isoformat(), self.id, "CABIN_UPDATE", cabin.model_dump(mode="json"))
        await self.reevaluate()

    async def ingest_advisory(self, adv: AdvisoryIn) -> None:
        """외부 운항계층(OPS_FEED) 이벤트. 시뮬레이터보다 우선."""
        self._ext_adv = (adv, utcnow())
        self.phase = "ADVISORY"
        await self._event("ADVISORY_RECEIVED", {"source": "OPS_FEED", **adv.model_dump()})
        self._refresh_ext_tta()
        await self.reevaluate()

    def _refresh_ext_tta(self) -> None:
        if not self._ext_adv:
            return
        adv, recv = self._ext_adv
        elapsed = (utcnow() - recv).total_seconds()
        est = tta_estimate(max(0.0, adv.tta_mean_s - elapsed), adv.tta_sigma_s, self.s.tta_quantile_low,
                           source=TtaSource.OPS_FEED, advisory_id=adv.advisory_id, severity=adv.severity)
        est.issued_at = recv   # 외부 정보는 새 이벤트가 와야 신선해짐 → stale 판정 대상
        self.tta = est

    # ------------------------------------------------------------ core
    async def reevaluate(self) -> Optional[Decision]:
        async with self._lock:
            if self.tta is None:
                self.decision = None
                self._publish({"type": "state", "data": self.snapshot()})
                return None
            cabin, tta = self.cabin, self.tta
            fail = self.kernel.fail_safe_reasons(cabin, tta)
            buffer_s = self.kernel.buffer_for(fail)
            key = (self.cabin_version, buffer_s)
            if self._evals is None or self._evals_key != key:
                plans = await self.planner.propose(cabin, tta.tta_low_s)
                plans.append(immediate_self_protect_plan(cabin))
                rng = np.random.default_rng(1000 + self.cabin_version)  # 같은 상태 → 같은 CST (화면 흔들림 방지)
                self._evals = [self.kernel.evaluate_plan(cabin, p, tta, buffer_s, rng) for p in plans]
                self._evals_key = key
                changed = True
            else:
                self._evals = [self.kernel.recheck_time(e, tta, buffer_s) for e in self._evals]
                changed = False
            prev_rec = self.decision.recommended.plan.plan_id if self.decision and self.decision.recommended else None
            self.decision = self.kernel.decide(self.id, cabin, tta, self._evals, fail, buffer_s)
            rec = self.decision.recommended.plan.plan_id if self.decision.recommended else None
        if changed or rec != prev_rec:
            await self.audit.log(utcnow().isoformat(), self.id, "DECISION",
                                 self.decision.model_dump(mode="json", exclude={"candidates": {"__all__": {"cst": {"timeline"}}}}))
        if rec != prev_rec and prev_rec is not None:
            await self._event("RECOMMENDATION_CHANGED", {"from": prev_rec, "to": rec,
                                                         "csm_s": self.decision.recommended.csm_s})
        self._publish({"type": "state", "data": self.snapshot()})
        return self.decision

    # ------------------------------------------------------------ simulator loop
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name=f"sim-{self.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        dt = self.s.sim_tick_s
        try:
            while True:
                await asyncio.sleep(dt)
                await self.tick(dt * self.s.sim_speed)
                if self.phase == "CLEARED":
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("sim loop crashed (%s)", self.id)

    async def tick(self, dt: float) -> None:
        if self._ext_adv:
            self._refresh_ext_tta()
        elif self.sim:
            self.sim.step(dt)
            mean, _ = self.sim.raw_tta()
            if self.sim.passed_zone:
                if self.phase != "CLEARED":
                    self.phase = "CLEARED"
                    self.tta = None
                    await self._event("TURBULENCE_CLEARED", self.sim.snapshot())
            elif self.sim.in_zone:
                if self.phase != "IN_TURBULENCE":
                    self.phase = "IN_TURBULENCE"
                    await self._event("TURBULENCE_ENCOUNTER", self.sim.snapshot())
                self.tta = self.sim.estimate(self.s.tta_quantile_low)
            elif mean <= self.s.sim_lookahead_s:
                self.tta = self.sim.estimate(self.s.tta_quantile_low)
                if self.phase == "MONITORING":
                    self.phase = "ADVISORY"
                    await self._event("ADVISORY_RECEIVED", {"source": "SIMULATOR", **self.tta.model_dump(mode="json")})
        await self.reevaluate()


class SessionManager:
    MAX_SESSIONS = 50

    def __init__(self, settings: Settings, planner, audit: AuditStore):
        self.s = settings
        self.planner = planner
        self.kernel = SafetyKernel(settings)
        self.audit = audit
        self.sessions: dict[str, FlightSession] = {}

    def create(self, cabin: CabinState, initial_tta_s: Optional[float], flight_id: Optional[str] = None) -> FlightSession:
        if len(self.sessions) >= self.MAX_SESSIONS:
            oldest = next(iter(self.sessions))
            asyncio.create_task(self.sessions.pop(oldest).stop())
        fid = flight_id or f"FLT-{uuid.uuid4().hex[:6].upper()}"
        sim = FlightSimulator.from_initial_tta(initial_tta_s) if initial_tta_s else None
        sess = FlightSession(fid, cabin, self.s, self.planner, self.kernel, self.audit, sim)
        self.sessions[fid] = sess
        return sess

    def get(self, fid: str) -> Optional[FlightSession]:
        return self.sessions.get(fid)

    async def remove(self, fid: str) -> None:
        sess = self.sessions.pop(fid, None)
        if sess:
            await sess.stop()

    async def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            await s.stop()
