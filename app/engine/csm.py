"""Cabin Safety Margin and plan verdicts — identical to the tablet's Safety Kernel.

    CSM = Q_low(TTA) − (CST̂_p90 + calibration margin) − safety buffer
"""
from __future__ import annotations

TIGHT_S = 5.0
PLAN_IDS = ("FULL", "MINIMAL", "IMMEDIATE")


def verdict(csm: float) -> str:
    return "no" if csm < 0 else "tight" if csm < TIGHT_S else "ok"


def decide(cst: list[float], q_low: float | None, buffer_s: float) -> dict:
    """Pick the most thorough plan that still passes, preferring margins of at least TIGHT_S."""
    if q_low is None:
        return {"cst_s": cst, "csm_s": None, "verdict": [None, None, None], "pick": 0, "pick_id": PLAN_IDS[0]}
    csm = [q_low - v - buffer_s for v in cst]
    ok = [j for j in range(3) if csm[j] >= 0]
    pick = sorted(ok, key=lambda j: (-(csm[j] >= TIGHT_S), j))[0] if ok else 2
    return {"cst_s": cst, "csm_s": csm, "verdict": [verdict(v) for v in csm], "pick": pick, "pick_id": PLAN_IDS[pick]}


def band(result: dict) -> str:
    """Summary colour for consoles: GREEN (comfortable), AMBER (tight), RED (sit down / no time)."""
    if result["csm_s"] is None:
        return "GREEN"
    if result["pick"] == 2:
        return "RED"
    return "GREEN" if result["csm_s"][result["pick"]] >= 15 else "AMBER"
