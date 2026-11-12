import numpy as np
import pytest

from app.engine.csm import decide
from app.engine.cst import cabin_state, features, get_model
from app.kernel import evaluate, verify_client_decision

CREW = ["L1", "R1", "L2", "R2", "G2", "L3", "R3", "L4", "R4", "G4"]


def test_model_matches_app_reference():
    m = get_model()
    assert m.params == 1827
    assert m.self_check() < 0.01


def test_features_are_38_and_reflect_actions():
    busy = cabin_state(1000, "m1", False, {}, CREW, [])
    done = cabin_state(1000, "m1", False, {k: True for k in ("serviceStopped", "cartsSecured", "hotStowed", "galleySecured", "lavsCleared")}, CREW, [])
    fb, fd = features(busy), features(done)
    assert len(fb) == len(fd) == 38
    cb, cd = get_model().predict(fb), get_model().predict(fd)
    assert cd[0] < cb[0]  # securing the cabin shortens the FULL plan


def test_decide_prefers_thorough_plan_with_margin():
    assert decide([100, 60, 10], 400, 10)["pick_id"] == "FULL"
    assert decide([100, 60, 10], 75, 10)["pick_id"] == "MINIMAL"
    assert decide([100, 60, 10], 15, 10)["pick_id"] == "IMMEDIATE"
    assert decide([100, 60, 10], 0, 10)["pick_id"] == "IMMEDIATE"   # nothing passes → sit down
    assert decide([100, 60, 10], None, 10)["csm_s"] is None


def test_kernel_never_picks_negative_margin_when_a_plan_passes():
    rng = np.random.default_rng(0)
    for _ in range(300):
        x = list(rng.random(38))
        q = float(rng.uniform(0, 400))
        r = evaluate(x, q, 10)
        if any(v >= 0 for v in r["csm_s"]):
            assert r["csm_s"][r["pick"]] >= 0


def test_verify_flags_unsafe_and_drift():
    x = features(cabin_state(1000, "m1", False, {}, CREW, []))
    srv = evaluate(x, 80, 10)
    ok = verify_client_decision({"features": x, "q_low_s": 80, "pick": srv["pick"], "cst_s": srv["cst_s"], "model_version": "cst-mlp-v1"}, 10)
    assert ok["agree"] and not ok["flags"]
    bad = verify_client_decision({"features": x, "q_low_s": 80, "pick": "FULL", "cst_s": [c + 5 for c in srv["cst_s"]]}, 10)
    if srv["csm_s"][0] < 0:
        assert "UNSAFE_PICK" in bad["flags"]
    assert "CST_DRIFT" in bad["flags"]
    assert verify_client_decision({"features": x, "q_low_s": None, "pick": 0, "model_version": "nope"}, 10)["flags"] == ["UNKNOWN_MODEL"]


@pytest.mark.parametrize("q", [30, 120, 300])
def test_ground_and_tablet_formula(q):
    m = get_model()
    x = features(cabin_state(1000, "sn", False, {}, CREW, []))
    r = evaluate(x, q, 10)
    raw = m.predict(x)
    assert r["cst_s"][1] == pytest.approx(max(0, raw[1]) + m.margin[1])
    assert r["csm_s"][1] == pytest.approx(q - r["cst_s"][1] - 10)
