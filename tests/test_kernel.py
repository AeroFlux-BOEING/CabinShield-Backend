import asyncio

import numpy as np
import pytest

from app.config import Settings
from app.engine.cst import compute_cst
from app.engine.tta import FlightSimulator, tta_estimate
from app.kernel import SafetyKernel, immediate_self_protect_plan
from app.models import Band, Plan, PlanStep, TtaSource, Verdict
from app.planner.rule_based import full_plan, minimal_plan
from app.scenarios import a320_hot_beverage

S = Settings(mc_samples=2000)
K = SafetyKernel(S)


def tta(mean, sigma=6.5):
    return tta_estimate(mean, sigma, S.tta_quantile_low, source=TtaSource.SIMULATOR, advisory_id="T")


def decide(mean):
    cab = a320_hot_beverage()
    plans = [full_plan(cab), minimal_plan(cab), immediate_self_protect_plan(cab)]
    t = tta(mean)
    evs = [K.evaluate_plan(cab, p, t, S.safety_buffer_s, np.random.default_rng(0)) for p in plans]
    return K.decide("T", cab, t, evs, [], S.safety_buffer_s)


# ---------------------------------------------------------------- 엔진
def test_cst_is_not_simple_sum():
    cab = a320_hot_beverage()
    p = full_plan(cab)
    cst = compute_cst(cab, p, S, np.random.default_rng(0))
    serial = sum(__import__("app.library", fromlist=["ACTIONS"]).ACTIONS[s.action_id].median_s for s in p.steps)
    assert cst.cst_median_s < serial  # 병렬 수행 반영
    assert cst.cst_high_s >= cst.cst_median_s


def test_cst_grows_with_congestion():
    cab = a320_hot_beverage()
    lo = compute_cst(cab, full_plan(cab), S, np.random.default_rng(0)).cst_high_s
    cab.aisle_congestion = 1.0
    hi = compute_cst(cab, full_plan(cab), S, np.random.default_rng(0)).cst_high_s
    assert hi > lo


def test_tta_low_is_conservative():
    t = tta(60, 6.5)
    assert t.tta_low_s < t.tta_mean_s < t.tta_high_s


def test_simulator_tta_decreases():
    sim = FlightSimulator.from_initial_tta(100)
    a, _ = sim.raw_tta()
    sim.step(10)
    b, _ = sim.raw_tta()
    assert pytest.approx(a - b, abs=0.01) == 10


# ---------------------------------------------------------------- 판정 흐름
def test_ample_time_recommends_full():
    d = decide(120)
    assert d.recommended.plan.plan_id == "RULE-FULL"
    assert d.band == Band.GREEN and not d.fail_safe


def test_medium_time_drops_nonessential():
    d = decide(60)
    full = next(e for e in d.candidates if e.plan.plan_id == "RULE-FULL")
    assert full.verdict == Verdict.REJECT
    assert d.recommended.plan.plan_id == "RULE-MINIMAL"


def test_short_time_self_protect():
    d = decide(35)
    assert d.recommended.plan.plan_id == "KERNEL-IMMEDIATE"
    assert d.band == Band.RED


def test_no_time_fail_safe():
    d = decide(8)
    assert d.recommended.plan.plan_id == "KERNEL-IMMEDIATE"
    assert d.fail_safe and "NO_FEASIBLE_PLAN" in d.fail_safe_reasons


# ---------------------------------------------------------------- 규칙 검증 (AI 출력 불신)
def _plan(steps):
    return Plan(plan_id="X", source="LLM", label="x", steps=[PlanStep(**s) for s in steps])


def _seat_all(extra=()):
    return list(extra) + [{"action_id": "CREW_SELF_PROTECT", "crew_id": f"FA{i}"} for i in range(1, 5)]


def test_reject_unknown_action():
    p = _plan(_seat_all([{"action_id": "OPEN_DOOR", "crew_id": "FA1"}]))
    ev = K.evaluate_plan(a320_hot_beverage(), p, tta(120), 10)
    assert ev.verdict == Verdict.REJECT and "NOT_IN_LIBRARY" in ev.reasons[0]


def test_reject_missing_self_protect():
    p = _plan([{"action_id": "CREW_SELF_PROTECT", "crew_id": "FA1"}])
    ev = K.evaluate_plan(a320_hot_beverage(), p, tta(120), 10)
    assert ev.verdict == Verdict.REJECT and any("MISSING_SELF_PROTECT" in r for r in ev.reasons)


def test_reject_moving_cart_with_hot_liquid():
    p = _plan(_seat_all([
        {"action_id": "STOP_SERVICE", "crew_id": "FA1"},
        {"action_id": "RETURN_CART_TO_GALLEY", "crew_id": "FA2", "target_id": "CART1"},
    ]))
    ev = K.evaluate_plan(a320_hot_beverage(), p, tta(120), 10)
    assert ev.verdict == Verdict.REJECT and any("FORBIDDEN" in r for r in ev.reasons)


def test_reject_action_after_self_protect():
    steps = _seat_all() + [{"action_id": "LAV_PAX_GUIDANCE", "crew_id": "FA3", "target_id": "LAV-AFT"}]
    ev = K.evaluate_plan(a320_hot_beverage(), _plan(steps), tta(120), 10)
    assert ev.verdict == Verdict.REJECT and any("AFTER_SELF_PROTECT" in r for r in ev.reasons)


def test_rule_violation_stays_rejected_on_time_recheck():
    p = _plan([{"action_id": "CREW_SELF_PROTECT", "crew_id": "FA1"}])
    ev = K.evaluate_plan(a320_hot_beverage(), p, tta(120), 10)
    assert K.recheck_time(ev, tta(300), 10).verdict == Verdict.REJECT


def test_stale_inputs_add_buffer():
    from datetime import timedelta
    cab = a320_hot_beverage()
    t = tta(60)
    t.issued_at = t.issued_at - timedelta(seconds=60)
    r = K.fail_safe_reasons(cab, t)
    assert r and K.buffer_for(r) > S.safety_buffer_s
