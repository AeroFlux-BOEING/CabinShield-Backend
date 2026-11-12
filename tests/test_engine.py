"""The ground engine must match the crew app (TypeScript) exactly — golden values exported from the app."""
import json
from pathlib import Path

import pytest

from app import library
from app.engine.flight import PLAN_GS, START_NM, exposures, flight_level, look_ahead, make_hazards, time_for_distance
from app.engine.geo import get_route
from app.engine.tta import point_stage, tta_estimate
from app.planner import plan_services

G = json.loads((Path(__file__).parent / "fixtures" / "golden_ts.json").read_text())
R = get_route("ICN-SFO")
HZ = make_hazards(R, library.default_hazards("ICN-SFO"))


def test_route_length_and_points():
    assert R.length_nm == pytest.approx(G["route_nm"], abs=1e-6)
    for p in G["pts"]:
        q = R.pos_at(p["d"])
        assert q.lat == pytest.approx(p["pos"]["lat"], abs=1e-9)
        assert q.lon == pytest.approx(p["pos"]["lon"], abs=1e-9)
        assert flight_level(R, p["d"]) == pytest.approx(p["fl"], abs=1e-9)


def test_hazard_centres():
    for h, g in zip(HZ, G["hazards"]):
        assert h.c0.lat == pytest.approx(g["c0"]["lat"], abs=1e-9)
        c = h.centre(h.t_ref + 3600)
        assert c.lon == pytest.approx(g["at"]["lon"], abs=1e-9)


def test_exposures_match_app():
    t0 = time_for_distance(START_NM)
    ex = exposures(R, HZ, START_NM, t0)
    assert len(ex) == len(G["exposures"])
    for e, g in zip(ex, G["exposures"]):
        assert (e.hazard.idx, e.d0, e.d1, e.max) == (g["hazard"], g["d0"], g["d1"], g["max"])
        assert e.t0 == pytest.approx(g["t0"], abs=1e-6)


def test_look_ahead_matches_app():
    for L in G["looks"]:
        la = look_ahead(R, HZ, L["d"], L["t"], L["gs"])
        if L["look"] is None:
            assert la is None
            continue
        assert la.cur == L["look"]["cur"] and la.max == L["look"]["max"] and la.exit == L["look"]["exit"]
        assert {str(k): v for k, v in la.first.items()} == L["look"]["first"]
        assert la.hazard.idx == L["look"]["hazard"]


def test_service_plan_matches_app():
    t0 = time_for_distance(START_NM)
    ex = exposures(R, HZ, START_NM, t0)
    eta = t0 + (R.length_nm - START_NM) / PLAN_GS * 3600
    plan = plan_services(library.services(), 0, eta, ex, None)
    assert [(p["id"], p["start_s"], p["end_s"], p["mode"], p["shift_s"]) for p in plan] == \
        [(g["id"], g["start"], g["end"], g["mode"], g["shift"]) for g in G["plan"]]


def test_point_stage_thresholds():
    leads = library.point_leads()
    assert leads == [1380, 780, 300]
    assert point_stage(None, False, leads) == 0
    assert point_stage(1400, False, leads) == 0
    assert point_stage(1380, False, leads) == 1
    assert point_stage(700, False, leads) == 2
    assert point_stage(200, False, leads) == 3
    assert point_stage(None, True, leads) == 3


def test_q_low_is_conservative():
    r = tta_estimate(600, False, 480, 0.05, 0)
    assert r["q_low_s"] < r["tta_mean_s"]
    assert r["q_low_s"] == pytest.approx(600 - 1.28 * r["tta_sigma_s"])
    assert tta_estimate(None, True, 480, 0.05, 0)["q_low_s"] == 0
    assert tta_estimate(None, False, 480, 0.05, 0) is None
