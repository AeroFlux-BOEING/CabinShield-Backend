"""Safety Kernel — 생성형 AI가 만든 후보를 결정론적 규칙 + 시간계산으로 검증한다.

AI는 최종판정 권한이 없다. 이 모듈을 통과한 계획만 승무원에게 표시된다.
검증 순서:
  1) 구조검증: 승인 행동만 / 존재하는 승무원·대상 / 자기보호는 각 승무원의 마지막 행동
  2) 선행조건·금지행동: 상태를 순서대로 적용하며 검사
  3) 시간검증: CSM = Q_low(TTA) − Q_high(CST) − B ≥ 0
  4) 통과 후보 중 (여유 충분 우선) → 잔여위험 최소 → CSM 최대 순으로 추천
  5) 통과 후보가 없거나 입력이 불확실하면 Fail-safe(즉시 자기보호)
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from app.config import Settings
from app.engine.csm import band_for, compute_csm
from app.engine.cst import compute_cst, nearest_galley
from app.library import ACTIONS, HAZARD_WEIGHTS
from app.models import (GALLEY_ZONES, Band, CabinState, CartStatus, Decision, Plan, PlanEvaluation,
                        PlanStep, TtaEstimate, Verdict, Zone)


# ------------------------------------------------------------------ 구조·규칙 검증
def _validate_structure(cabin: CabinState, plan: Plan) -> list[str]:
    errs: list[str] = []
    crew_ids = {c.id for c in cabin.crew}
    cart_ids = {c.id for c in cabin.carts}
    lav_ids = {l.id for l in cabin.lavatories}
    terminated: set[str] = set()

    for i, s in enumerate(plan.steps):
        a = ACTIONS.get(s.action_id)
        if a is None:
            errs.append(f"[{i}] NOT_IN_LIBRARY: '{s.action_id}'는 승인 행동이 아님")
            continue
        if s.crew_id not in crew_ids:
            errs.append(f"[{i}] UNKNOWN_CREW: {s.crew_id}")
            continue
        if s.crew_id in terminated:
            errs.append(f"[{i}] AFTER_SELF_PROTECT: {s.crew_id}는 자기보호 이후 행동 불가")
        if a.terminal:
            terminated.add(s.crew_id)
        if a.target == "CART" and s.target_id not in cart_ids:
            errs.append(f"[{i}] BAD_TARGET: 카트 '{s.target_id}' 없음")
        elif a.target == "LAV" and s.target_id not in lav_ids:
            errs.append(f"[{i}] BAD_TARGET: 화장실 '{s.target_id}' 없음")
        elif a.target == "GALLEY" and s.target_id not in {z.value for z in GALLEY_ZONES}:
            errs.append(f"[{i}] BAD_TARGET: 갤리 '{s.target_id}' 없음")
        elif a.target == "ZONE" and s.target_id not in {z.value for z in Zone}:
            errs.append(f"[{i}] BAD_TARGET: 구역 '{s.target_id}' 없음")

    missing = crew_ids - terminated
    if missing:
        errs.append(f"MISSING_SELF_PROTECT: {sorted(missing)} 자기보호(착석) 미포함")
    return errs


def _apply_and_check(cabin: CabinState, plan: Plan) -> tuple[list[str], CabinState]:
    """상태를 순서대로 적용하며 선행조건·금지행동 검사. 최종 객실상태 반환."""
    st = cabin.model_copy(deep=True)
    carts = {c.id: c for c in st.carts}
    lavs = {l.id: l for l in st.lavatories}
    galleys = {g.zone: g for g in st.galleys}
    errs: list[str] = []
    steps = plan.steps
    has_stop = any(s.action_id == "STOP_SERVICE" for s in steps)
    non_terminal = [s for s in steps if not ACTIONS[s.action_id].terminal]
    secured_in_plan: set[Zone] = set()

    if cabin.service_active and non_terminal and not has_stop:
        errs.append("SERVICE_NOT_STOPPED: 서비스 중 정리행동 수행 시 STOP_SERVICE 필요")

    for i, s in enumerate(steps):
        aid = s.action_id
        if aid == "STOP_SERVICE":
            if not st.service_active:
                errs.append(f"[{i}] PRECONDITION: 서비스가 이미 중단됨")
            st.service_active = False
        elif aid == "STOW_HOT_LIQUID":
            c = carts[s.target_id]
            if not c.hot_liquid:
                errs.append(f"[{i}] PRECONDITION: {c.id}에 뜨거운 음료 없음")
            c.hot_liquid = False
        elif aid == "SECURE_CART_IN_PLACE":
            c = carts[s.target_id]
            if c.status != CartStatus.AISLE_UNSECURED:
                errs.append(f"[{i}] PRECONDITION: {c.id}는 미고정 상태가 아님")
            c.status = CartStatus.AISLE_SECURED
        elif aid == "RETURN_CART_TO_GALLEY":
            c = carts[s.target_id]
            if c.status == CartStatus.STOWED:
                errs.append(f"[{i}] PRECONDITION: {c.id}는 이미 수납됨")
            if c.hot_liquid:
                errs.append(f"[{i}] FORBIDDEN: 뜨거운 음료 미수납 상태로 카트 이동 금지 ({c.id})")
            g = nearest_galley(c.zone)
            c.zone, c.status = g, CartStatus.STOWED
            if g in secured_in_plan:
                errs.append(f"[{i}] ORDER: 이미 잠긴 {g.value} 갤리로 카트 복귀 — 갤리 정리 순서 오류")
        elif aid == "LAV_PAX_GUIDANCE":
            l = lavs[s.target_id]
            if not l.occupied:
                errs.append(f"[{i}] PRECONDITION: {l.id} 비어 있음")
            l.occupied = False
        elif aid == "SECURE_GALLEY":
            g = galleys[Zone(s.target_id)]
            if g.secured:
                errs.append(f"[{i}] PRECONDITION: {g.zone.value} 갤리 이미 정리됨")
            g.secured = True
            secured_in_plan.add(g.zone)
    return errs, st


def residual_hazard(st: CabinState) -> tuple[float, list[str]]:
    w, items = 0.0, []
    for c in st.carts:
        if c.hot_liquid:
            w += HAZARD_WEIGHTS["HOT_LIQUID"]; items.append(f"{c.id}: 뜨거운 음료 노출")
        if c.status == CartStatus.AISLE_UNSECURED:
            w += HAZARD_WEIGHTS["CART_AISLE_UNSECURED"]; items.append(f"{c.id}: 통로 미고정")
        elif c.status == CartStatus.AISLE_SECURED:
            w += HAZARD_WEIGHTS["CART_AISLE_SECURED"]; items.append(f"{c.id}: 통로 고정(잔여)")
    for l in st.lavatories:
        if l.occupied:
            w += HAZARD_WEIGHTS["LAV_OCCUPIED"]; items.append(f"{l.id}: 승객 재실")
    for g in st.galleys:
        if not g.secured:
            w += HAZARD_WEIGHTS["GALLEY_UNSECURED"]; items.append(f"{g.zone.value} 갤리 미정리")
    if st.service_active:
        w += HAZARD_WEIGHTS["SERVICE_ACTIVE"]; items.append("서비스 진행 중")
    return round(w, 2), items


def immediate_self_protect_plan(cabin: CabinState) -> Plan:
    return Plan(
        plan_id="KERNEL-IMMEDIATE", source="KERNEL_FALLBACK", label="즉시 자기보호",
        steps=[PlanStep(action_id="CREW_SELF_PROTECT", crew_id=c.id) for c in cabin.crew],
        rationale="검증을 통과한 후보가 없거나 입력 신뢰도가 낮아 커널이 생성한 Fail-safe 계획.",
    )


# ------------------------------------------------------------------ 평가
class SafetyKernel:
    def __init__(self, settings: Settings):
        self.s = settings

    def evaluate_plan(self, cabin: CabinState, plan: Plan, tta: TtaEstimate, buffer_s: float,
                      rng: np.random.Generator | None = None) -> PlanEvaluation:
        errs = _validate_structure(cabin, plan)
        if errs:
            return PlanEvaluation(plan=plan, verdict=Verdict.REJECT, reasons=errs)
        rule_errs, final_state = _apply_and_check(cabin, plan)
        hz, items = residual_hazard(final_state)
        if rule_errs:
            return PlanEvaluation(plan=plan, verdict=Verdict.REJECT, reasons=rule_errs,
                                  residual_hazard=hz, residual_items=items)
        cst = compute_cst(cabin, plan, self.s, rng)
        verdict, csm, reason = self._time_check(tta, cst.cst_high_s, buffer_s)
        return PlanEvaluation(plan=plan, verdict=verdict, reasons=[reason], cst=cst, csm_s=csm,
                              residual_hazard=hz, residual_items=items)

    @staticmethod
    def _time_check(tta: TtaEstimate, cst_high_s: float, buffer_s: float) -> tuple[Verdict, float, str]:
        raw = compute_csm(tta.tta_low_s, cst_high_s, buffer_s)
        ok = raw >= 0
        reason = (f"TIME_OK: CSM {raw:+.1f}s" if ok else
                  f"TIME_SHORT: Q_low(TTA) {tta.tta_low_s:.1f}s < Q_high(CST) {cst_high_s:.1f}s + B {buffer_s:.0f}s")
        return (Verdict.PASS if ok else Verdict.REJECT), round(raw, 1), reason

    def recheck_time(self, ev: PlanEvaluation, tta: TtaEstimate, buffer_s: float) -> PlanEvaluation:
        """객실상태가 그대로일 때 TTA만 갱신되면 CST 재계산 없이 시간검증만 다시 수행."""
        if ev.cst is None:
            return ev
        if ev.reasons and not ev.reasons[0].startswith("TIME_"):
            return ev   # 구조/규칙 위반은 시간과 무관하게 REJECT 유지
        verdict, csm, reason = self._time_check(tta, ev.cst.cst_high_s, buffer_s)
        return ev.model_copy(update={"verdict": verdict, "csm_s": csm, "reasons": [reason]})

    def fail_safe_reasons(self, cabin: CabinState, tta: TtaEstimate, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        r = []
        if (now - tta.issued_at).total_seconds() > self.s.advisory_stale_s:
            r.append("STALE_ADVISORY: 난기류 정보 갱신 지연")
        if (now - cabin.updated_at).total_seconds() > self.s.cabin_state_stale_s:
            r.append("STALE_CABIN_STATE: 객실상태 갱신 지연")
        if tta.tta_sigma_s > max(15.0, 0.4 * tta.tta_mean_s):
            r.append("LOW_CONFIDENCE_TTA: TTA 불확실성 과대")
        return r

    def buffer_for(self, fail_reasons: list[str]) -> float:
        return self.s.safety_buffer_s + (self.s.stale_extra_buffer_s if fail_reasons else 0.0)

    def decide(self, flight_id: str, cabin: CabinState, tta: TtaEstimate,
               evaluations: list[PlanEvaluation], fail_reasons: list[str], buffer_s: float) -> Decision:
        passed = [e for e in evaluations if e.verdict == Verdict.PASS]
        # 랭킹: (1) CSM이 최소 선호여유 이상인 후보 우선 → (2) 잔여위험 최소 → (3) CSM 최대
        thin = self.s.csm_preferred_min_s
        passed.sort(key=lambda e: ((e.csm_s or 0) < thin, e.residual_hazard, -(e.csm_s or 0),
                                   e.plan.source == "KERNEL_FALLBACK"))
        reasons = list(fail_reasons)
        if passed:
            rec = passed[0]
        else:
            fb = next((e for e in evaluations if e.plan.source == "KERNEL_FALLBACK"), None)
            if fb is None:
                fb = self.evaluate_plan(cabin, immediate_self_protect_plan(cabin), tta, buffer_s)
            rec = fb.model_copy(update={
                "verdict": Verdict.PASS,
                "reasons": fb.reasons + ["FAIL_SAFE: 어떤 후보도 시간검증 통과 못함 → 즉시 자기보호"],
            })
            reasons.append("NO_FEASIBLE_PLAN")
        self_protect_only = all(ACTIONS[s.action_id].terminal for s in rec.plan.steps)
        if self_protect_only or rec.csm_s is None or rec.csm_s < 0:
            band = Band.RED
        else:
            band = band_for(rec.csm_s, self.s.csm_green_s)
        return Decision(flight_id=flight_id, tta=tta, buffer_s=buffer_s,
                        fail_safe=bool(reasons), fail_safe_reasons=reasons, band=band,
                        recommended=rec, candidates=evaluations)
