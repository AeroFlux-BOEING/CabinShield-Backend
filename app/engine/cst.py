"""CST(Cabin Secure Time) 엔진.

행동시간의 단순 합이 아니라, 승무원별 병렬 수행 + 구역 이동 + 선행조건을 반영한
스케줄을 몬테카를로로 N회 샘플링해 완료시간 분포를 구한다.
입력 plan은 Safety Kernel의 구조검증을 통과했다고 가정한다.
"""
from __future__ import annotations

import numpy as np

from app.config import Settings
from app.library import ACK_DELAY, ACTIONS, CART_TRAVEL_PER_ZONE_S, TRAVEL_PER_ZONE, ActionDef
from app.models import ZONE_INDEX, CabinState, CstResult, Plan, Zone

ZONES = [Zone.FWD, Zone.MID, Zone.AFT]


def nearest_galley(zone: Zone) -> Zone:
    # MID는 후방 갤리를 우선(동률 tie-break). 항공사/기종별 설정으로 바꿀 수 있음.
    return Zone.FWD if zone == Zone.FWD else Zone.AFT


def _sample(rng: np.random.Generator, a: ActionDef, n: int, median: float | None = None) -> np.ndarray:
    m = a.median_s if median is None else median
    return m * np.exp(a.sigma * rng.standard_normal(n))


def resolve_target_zone(cabin: CabinState, action_id: str, target_id: str | None,
                        cart_zone: dict[str, Zone], crew_zone: Zone) -> Zone:
    a = ACTIONS[action_id]
    if a.target == "CART":
        return cart_zone[target_id]
    if a.target == "LAV":
        return next(l.zone for l in cabin.lavatories if l.id == target_id)
    if a.target in ("GALLEY", "ZONE"):
        return Zone(target_id)
    return crew_zone


def compute_cst(cabin: CabinState, plan: Plan, settings: Settings,
                rng: np.random.Generator | None = None) -> CstResult:
    rng = rng or np.random.default_rng()
    n = settings.mc_samples
    cong = 1.0 + 0.5 * cabin.aisle_congestion

    crew_zone = {c.id: c.zone for c in cabin.crew}
    cart_zone = {c.id: c.zone for c in cabin.carts}
    crew_free = {c.id: _sample(rng, ACK_DELAY, n, settings.ack_delay_median_s) for c in cabin.crew}
    cart_free: dict[str, np.ndarray] = {}
    galley_ready: dict[Zone, np.ndarray] = {}   # 해당 갤리로 복귀하는 카트들의 완료시각
    service_stop: np.ndarray | None = None
    zeros = np.zeros(n)
    timeline: list[dict] = []

    for idx, s in enumerate(plan.steps):
        a = ACTIONS[s.action_id]
        here = crew_zone[s.crew_id]
        tz = resolve_target_zone(cabin, s.action_id, s.target_id, cart_zone, here)
        dz = abs(ZONE_INDEX[tz] - ZONE_INDEX[here]) if a.moves_crew_to_target else 0
        travel = _sample(rng, TRAVEL_PER_ZONE, n) * dz * cong if dz else zeros

        ready = crew_free[s.crew_id] + travel
        deps = [ready]
        if a.target == "CART":
            if service_stop is not None:
                deps.append(service_stop)
            if s.target_id in cart_free:
                deps.append(cart_free[s.target_id])
        if a.id == "SECURE_GALLEY" and tz in galley_ready:
            deps.append(galley_ready[tz])
        start = np.maximum.reduce(deps)

        dur = _sample(rng, a, n)
        end_zone = tz
        if a.id == "RETURN_CART_TO_GALLEY":
            g = nearest_galley(tz)
            gdz = abs(ZONE_INDEX[g] - ZONE_INDEX[tz])
            dur = dur + CART_TRAVEL_PER_ZONE_S * gdz * cong * np.exp(0.25 * rng.standard_normal(n))
            end_zone = g
            cart_zone[s.target_id] = g
        end = start + dur

        crew_free[s.crew_id] = end
        crew_zone[s.crew_id] = end_zone
        if a.target == "CART":
            cart_free[s.target_id] = end
            if a.id == "RETURN_CART_TO_GALLEY":
                prev = galley_ready.get(end_zone)
                galley_ready[end_zone] = end if prev is None else np.maximum(prev, end)
        if a.id == "STOP_SERVICE":
            service_stop = end if service_stop is None else np.minimum(service_stop, end)

        timeline.append({
            "step": idx, "action_id": a.id, "name_ko": a.name_ko, "crew_id": s.crew_id,
            "target_id": s.target_id, "zone": end_zone.value,
            "start_s": round(float(np.median(start)), 1), "end_s": round(float(np.median(end)), 1),
        })

    makespan = np.maximum.reduce(list(crew_free.values()))
    return CstResult(
        cst_median_s=round(float(np.median(makespan)), 1),
        cst_high_s=round(float(np.quantile(makespan, settings.cst_quantile_high)), 1),
        per_crew_finish_s={k: round(float(np.quantile(v, settings.cst_quantile_high)), 1)
                           for k, v in crew_free.items()},
        timeline=timeline,
    )
