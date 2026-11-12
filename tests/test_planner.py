from app import library
from app.engine.flight import PLAN_GS, START_NM, exposures, make_hazards, time_for_distance
from app.engine.geo import get_route
from app.planner import plan_services, rule_insights

R = get_route("ICN-SFO")
HZ = make_hazards(R, library.default_hazards("ICN-SFO"))
T0 = time_for_distance(START_NM)
EX = exposures(R, HZ, START_NM, T0)
ETA = T0 + (R.length_nm - START_NM) / PLAN_GS * 3600


def test_services_avoid_segments():
    for p in plan_services(library.services(), 0, ETA, EX, None):
        if p["start_s"] is None:
            continue
        for e in EX:
            assert p["end_s"] <= e.t0 or p["start_s"] >= e.t1


def test_started_service_is_frozen():
    plan = plan_services(library.services(), 0, ETA, EX, None)
    started = plan[0]
    again = plan_services(library.services(), started["start_s"] + 60, ETA, [], plan)
    assert again[0] == started


def test_insights_match_history_and_are_bilingual():
    ins = rule_insights(EX, library.history("ICN-SFO"), library.strength_labels())
    assert len(ins) == len(EX)
    assert all(i["text"]["ko"] and i["text"]["en"] for i in ins)
    assert ins[0]["history_id"] == "H1"  # northern Japan segment overlaps the first forecast area
