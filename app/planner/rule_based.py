"""규칙 기반 후보 생성기 (LLM 미사용 시 기본값, LLM 실패 시 폴백).

후보: FULL(완전 정리) / MINIMAL(현장 고정 위주)  (+ 커널이 IMMEDIATE 추가)
승무원 배정은 중앙값 기준 그리디(가장 빨리 도착 가능한 승무원)로 한다.
"""
from __future__ import annotations

from app.engine.cst import nearest_galley
from app.library import ACTIONS, CART_TRAVEL_PER_ZONE_S, TRAVEL_PER_ZONE
from app.models import ZONE_INDEX, CabinState, CartStatus, Plan, PlanStep, Zone


class _Greedy:
    def __init__(self, cabin: CabinState):
        self.cabin = cabin
        self.t = {c.id: 0.0 for c in cabin.crew}
        self.z = {c.id: c.zone for c in cabin.crew}
        self.steps: list[PlanStep] = []

    def _eta(self, crew_id: str, zone: Zone) -> float:
        return self.t[crew_id] + TRAVEL_PER_ZONE.median_s * abs(ZONE_INDEX[zone] - ZONE_INDEX[self.z[crew_id]])

    def assign(self, action_id: str, zone: Zone, target_id: str | None = None,
               crew_id: str | None = None, end_zone: Zone | None = None, extra: float = 0.0) -> str:
        cid = crew_id or min(self.t, key=lambda c: (self._eta(c, zone), c))
        self.t[cid] = self._eta(cid, zone) + ACTIONS[action_id].median_s + extra
        self.z[cid] = end_zone or zone
        self.steps.append(PlanStep(action_id=action_id, crew_id=cid, target_id=target_id))
        return cid

    def finish(self) -> list[PlanStep]:
        for c in self.cabin.crew:
            self.steps.append(PlanStep(action_id="CREW_SELF_PROTECT", crew_id=c.id))
        return self.steps


def _aisle_carts(cabin: CabinState):
    return [c for c in cabin.carts if c.status != CartStatus.STOWED]


def _stop_service(g: _Greedy, cabin: CabinState):
    if cabin.service_active:
        carts = _aisle_carts(cabin)
        zone = carts[0].zone if carts else cabin.crew[0].zone
        g.assign("STOP_SERVICE", zone)


def full_plan(cabin: CabinState) -> Plan:
    g = _Greedy(cabin)
    _stop_service(g, cabin)
    returned_to: set[Zone] = set()
    for c in _aisle_carts(cabin):
        cid = g.assign("STOW_HOT_LIQUID", c.zone, c.id) if c.hot_liquid else None
        gz = nearest_galley(c.zone)
        extra = CART_TRAVEL_PER_ZONE_S * abs(ZONE_INDEX[gz] - ZONE_INDEX[c.zone])
        g.assign("RETURN_CART_TO_GALLEY", c.zone, c.id, crew_id=cid, end_zone=gz, extra=extra)
        returned_to.add(gz)
    for l in cabin.lavatories:
        if l.occupied:
            g.assign("LAV_PAX_GUIDANCE", l.zone, l.id)
    for gl in cabin.galleys:
        if not gl.secured:
            g.assign("SECURE_GALLEY", gl.zone, gl.zone.value)
    return Plan(plan_id="RULE-FULL", source="RULE", label="완전 정리 후 착석",
                steps=g.finish(),
                rationale="카트를 갤리로 복귀·잠금하고 갤리까지 정리. 잔여위험 최소, 소요시간 최대.")


def minimal_plan(cabin: CabinState) -> Plan:
    g = _Greedy(cabin)
    _stop_service(g, cabin)
    for c in _aisle_carts(cabin):
        cid = g.assign("STOW_HOT_LIQUID", c.zone, c.id) if c.hot_liquid else None
        if c.status == CartStatus.AISLE_UNSECURED:
            g.assign("SECURE_CART_IN_PLACE", c.zone, c.id, crew_id=cid)
    for l in cabin.lavatories:
        if l.occupied:
            g.assign("LAV_PAX_GUIDANCE", l.zone, l.id)
    return Plan(plan_id="RULE-MINIMAL", source="RULE", label="핵심 위험만 정리 후 착석",
                steps=g.finish(),
                rationale="뜨거운 음료 수납 + 카트 현장 고정 + 화장실 안내만 수행. 갤리 정리는 포기.")


def immediate_plan(cabin: CabinState) -> Plan:
    g = _Greedy(cabin)
    return Plan(plan_id="RULE-IMMEDIATE", source="RULE", label="즉시 자기보호",
                steps=g.finish(),
                rationale="정리 없이 전원 즉시 착석. 잔여위험은 남지만 승무원 부상 위험 최소화.")


class RuleBasedPlanner:
    name = "rule"

    async def propose(self, cabin: CabinState, tta_low_s: float | None = None) -> list[Plan]:
        # 즉시 자기보호 후보는 Safety Kernel이 항상 직접 추가한다(KERNEL-IMMEDIATE).
        return [full_plan(cabin), minimal_plan(cabin)]
