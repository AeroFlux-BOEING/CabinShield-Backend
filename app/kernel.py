"""Safety Kernel, ground side.

The authoritative decision is made on the tablet (offline-capable). The ground re-runs the same
model and rules on every decision the tablet uploads, so the operations centre can audit them:
  * `agree`   — the server picked the same plan
  * `unsafe`  — the tablet picked a plan whose margin is negative although a passing plan existed
  * `drift`   — the tablet's CST differs from the server's by more than the tolerance (model mismatch)
"""
from __future__ import annotations

from app.engine.csm import PLAN_IDS, band, decide
from app.engine.cst import CstModel, get_model

CST_TOLERANCE_S = 0.5


def evaluate(features: list[float], q_low: float | None, buffer_s: float, model: CstModel | None = None) -> dict:
    m = model or get_model()
    raw = m.predict(features)
    cst = [max(0.0, float(v)) + float(mg) for v, mg in zip(raw, m.margin)]
    r = decide(cst, q_low, buffer_s)
    r.update(model_version=m.version, band=band(r), buffer_s=buffer_s, q_low_s=q_low)
    return r


def verify_client_decision(payload: dict, buffer_s: float) -> dict:
    """Compare a tablet decision with the ground kernel. payload follows `models.DecisionPayload`."""
    try:
        m = get_model(payload.get("model_version"))
        unknown_model = False
    except KeyError:
        m, unknown_model = get_model(), True
    srv = evaluate(payload["features"], payload.get("q_low_s"), buffer_s, m)
    client_pick = payload["pick"] if isinstance(payload["pick"], int) else PLAN_IDS.index(payload["pick"])
    flags: list[str] = []
    if unknown_model:
        flags.append("UNKNOWN_MODEL")
    if payload.get("cst_s"):
        if max(abs(a - b) for a, b in zip(payload["cst_s"], srv["cst_s"])) > CST_TOLERANCE_S:
            flags.append("CST_DRIFT")
    if srv["csm_s"] is not None:
        if srv["csm_s"][client_pick] < 0 and any(v >= 0 for v in srv["csm_s"]):
            flags.append("UNSAFE_PICK")
    return {"agree": client_pick == srv["pick"], "client_pick": PLAN_IDS[client_pick], "server_pick": srv["pick_id"],
            "server": srv, "flags": flags}
