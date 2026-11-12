"""Flight instances: create from the schedule, pre-flight briefing package, live state, simulator."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import library
from app.api.deps import manager, require_key, require_ops, session
from app.api.v1._util import cached_json, etag_of
from app.api.v1.reference import merged_history
from app.config import Settings, get_settings
from app.engine.cst import get_model
from app.engine.flight import PLAN_GS, START_NM
from app.models import FlightCreate, FlightOut, PositionReport, SimControl, TurbulenceReport
from app.orchestrator import FlightSession, SessionManager
from app.planner import llm_insights, rule_insights
from app.planner.rule_based import match_history
from app.scenarios import SCENARIOS
from app.store import now_utc

router = APIRouter(prefix="/flights", tags=["flights"])


@router.post("", response_model=FlightOut, status_code=201, dependencies=[Depends(require_key)],
             summary="Start a flight (flight number + date → route auto-filled from the schedule)")
def create_flight(body: FlightCreate, m: SessionManager = Depends(manager)):
    no = library.normalise_flight_no(body.flight_no)
    f = library.schedule().get(no)
    if not f:
        raise HTTPException(404, {"code": "UNKNOWN_FLIGHT", "msg": {"ko": "스케줄에 없는 편명입니다", "en": "Flight number not in the schedule"}})
    if not f["supported"]:
        raise HTTPException(422, {"code": "ROUTE_NOT_SUPPORTED", "msg": {"ko": "이 노선은 데모에서 아직 준비 중입니다", "en": "This route is not ready in the demo yet"}})
    if not library.operates_on(f, body.date):
        nd = library.next_operating_date(f, body.date)
        raise HTTPException(422, {"code": "NOT_OPERATING", "next_operating_date": nd.isoformat() if nd else None,
                                  "msg": {"ko": "선택한 날짜에 운항하지 않는 편입니다", "en": "This flight does not operate on the selected date"}})
    dep_local = body.dep_local or f["dep_local"]
    fid = f"{no}-{body.date:%Y%m%d}"
    flight = {"id": fid, "flight_no": no, "date": body.date, "route_id": f["route_id"], "aircraft": f["aircraft"],
              "dep_local": dep_local, "dep_utc": library.departure_utc(body.date, dep_local), "status": "SCHEDULED",
              "feed": body.feed, "created_at": now_utc()}
    sess = m.get(fid)
    if sess is None:
        sess = m.create(flight)
    else:
        sess.flight.update(dep_local=dep_local, dep_utc=flight["dep_utc"], feed=body.feed)
        m.store.upsert_flight(sess.flight)
    if body.autostart and body.feed == "SIMULATOR" and not sess.running:
        sess.start()
    return sess.flight


@router.get("", response_model=list[FlightOut])
def list_flights(m: SessionManager = Depends(manager), limit: int = Query(50, le=200)):
    return [m.get(r["id"]).flight for r in m.store.list_flights(limit)]


@router.get("/{flight_id}", response_model=FlightOut)
def get_flight(sess: FlightSession = Depends(session)):
    return sess.flight


def build_insights(sess: FlightSession, m: SessionManager) -> tuple[list[dict], list[dict]]:
    hist = merged_history(m, sess.route.id)
    rule = rule_insights(sess.ex, hist, library.strength_labels())
    facts = []
    for e in sess.ex:
        h = match_history(e, hist)
        facts.append({"segment": e.hazard.name, "strength": library.strength_labels()[e.max], "fl_band": list(e.hazard.fl),
                      "minutes_ahead": round((e.t0 - sess.t) / 60), "history": h})
    return rule, facts


@router.get("/{flight_id}/briefing", summary="Pre-flight package the tablet downloads before losing the ground link")
def briefing(request: Request, sess: FlightSession = Depends(session), m: SessionManager = Depends(manager)):
    if sess.plan is None:
        sess.evaluate()
        sess.update_plan(force=True)
    rule, _ = build_insights(sess, m)
    model = get_model()
    body = {
        "flight": {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in sess.flight.items()},
        "reference_version": library.reference_bundle()["version"],
        "route": {"route_id": sess.route.id, "length_nm": round(sess.route.length_nm, 3), "plan_gs_kt": PLAN_GS,
                  "demo_start_nm": START_NM, "polyline": sess.route.polyline(50)},
        "forecast": {"version": sess.forecast_version, "issued_at": sess.forecast_issued_at, "hazards": sess.hazards_view()},
        "exposures": sess.exposures_view(),
        "service_plan": sess.plan_view(),
        "crew": sess.ac["crew"], "rest_groups": sess.ac["rest_groups"],
        "history": merged_history(m, sess.route.id),
        "insights": rule,
        "model": {**{k: v for k, v in model.manifest().items() if k != "plans"}, "url": f"/api/v1/models/cst/{model.version}"},
        "kernel": {"safety_buffer_s": sess.s.safety_buffer_s, "point_leads_s": sess.leads, "z_low": 1.28},
    }
    tag = etag_of({k: v for k, v in body.items() if k not in ("exposures", "service_plan")} | {"v": sess.forecast_version})
    body["generated_at"] = now_utc().isoformat()
    return cached_json(request, body, etag=tag, max_age=30)


@router.get("/{flight_id}/state", summary="Live ground view: position, next segment, TTA / Q_low, point stage, service plan")
def state(sess: FlightSession = Depends(session), ai: bool = Query(True, description="include the ground AI re-check")):
    snap = sess.snapshot()
    if ai:
        snap["ground_ai"] = sess.ground_ai()
    return snap


@router.get("/{flight_id}/forecast-map", summary="Hazard rings at a given time (default: now)")
def forecast_map(sess: FlightSession = Depends(session), t_s: float | None = None):
    return {"version": sess.forecast_version, "t_s": t_s if t_s is not None else sess.t, "hazards": sess.hazards_view(t_s)}


@router.get("/{flight_id}/insights", summary="AI insight text per upcoming segment (rule-based, optional LLM wording)")
async def insights(sess: FlightSession = Depends(session), m: SessionManager = Depends(manager), s: Settings = Depends(get_settings)):
    rule, facts = build_insights(sess, m)
    return await llm_insights(s, rule, facts) if s.planner == "llm" else rule


@router.get("/{flight_id}/log", summary="Server-side event log (newest first)")
def server_log(sess: FlightSession = Depends(session), m: SessionManager = Depends(manager), limit: int = Query(100, le=500)):
    return m.store.server_events(sess.id, limit)


@router.post("/{flight_id}/position", dependencies=[Depends(require_ops)], summary="Aircraft position (AID / ACARS feed)")
def position(body: PositionReport, sess: FlightSession = Depends(session)):
    if sess.flight["feed"] != "AIRCRAFT_FEED":
        raise HTTPException(409, "flight uses the simulator; create it with feed=AIRCRAFT_FEED")
    sess.ingest_position(body.model_dump())
    return sess.snapshot()


@router.post("/{flight_id}/reports", status_code=201, dependencies=[Depends(require_key)],
             summary="Crew turbulence report (feeds 'past similar reports' for later flights)")
def report(body: TurbulenceReport, sess: FlightSession = Depends(session), m: SessionManager = Depends(manager)):
    r = body.model_dump()
    if r["d_nm"] is None and r["lat"] is not None and r["lon"] is not None:
        from app.engine.geo import LatLon
        r["d_nm"] = round(sess.route.project(LatLon(r["lat"], r["lon"] % 360))[0], 1)
    if r["d_nm"] is None:
        r["d_nm"] = round(sess.est_d, 1)
    rid = m.store.add_report(sess.id, sess.route.id, r)
    sess.emit("REPORT_FILED", {"ko": f"승무원 난기류 보고 접수 (강도 {r['severity']})", "en": f"Crew turbulence report filed (severity {r['severity']})"}, {"report_id": rid})
    return {"id": rid, **r}


# ---------------------------------------------------------------- simulator (demo aircraft feed)

@router.post("/{flight_id}/sim/start", dependencies=[Depends(require_key)])
def sim_start(sess: FlightSession = Depends(session), restart: bool = False):
    if restart or not sess._started:
        sess.start()
    else:
        sess.running = not sess.arrived
    return sess.snapshot()


@router.post("/{flight_id}/sim/stop", dependencies=[Depends(require_key)])
def sim_stop(sess: FlightSession = Depends(session)):
    sess.running = False
    return sess.snapshot()


@router.post("/{flight_id}/sim/step", dependencies=[Depends(require_key)], summary="Advance N sim seconds (works while paused)")
def sim_step(sess: FlightSession = Depends(session), seconds: float = Query(60, gt=0, le=36000)):
    if not sess._started:
        sess.start()
        sess.running = False
    sess.advance_sim(seconds)
    return sess.snapshot()


@router.post("/{flight_id}/sim/control", dependencies=[Depends(require_key)], summary="Speed, position-signal loss, ground-link loss")
def sim_control(body: SimControl, sess: FlightSession = Depends(session)):
    sess.control(body.speed, body.aid, body.sat)
    return sess.snapshot()


@router.post("/{flight_id}/sim/jump-next", dependencies=[Depends(require_key)], summary="Jump to 30 min before the next segment")
def sim_jump(sess: FlightSession = Depends(session)):
    return {**sess.jump_to_next(), "state": sess.snapshot()}


@router.get("/{flight_id}/scenarios", summary="Demo scenarios")
def list_scenarios(flight_id: str):
    return {k: v["title"] for k, v in SCENARIOS.items()}


@router.post("/{flight_id}/scenarios/{name}", dependencies=[Depends(require_ops)])
def run_scenario(name: str, sess: FlightSession = Depends(session), m: SessionManager = Depends(manager)):
    sc = SCENARIOS.get(name)
    if not sc:
        raise HTTPException(404, f"unknown scenario {name}")
    done = []
    for st in sc["steps"]:
        if st["op"] == "jump_next":
            done.append(sess.jump_to_next())
        elif st["op"] == "control":
            sess.control(aid=st.get("aid"), sat=st.get("sat"))
            done.append({"control": {k: v for k, v in st.items() if k != "op"}})
        elif st["op"] == "add_hazard":
            hz = [h for h in m.forecast(sess.route.id)["hazards"] if h["id"] != st["hazard"]["id"]] + [st["hazard"]]
            fc = m.publish_forecast(sess.route.id, hz, f"scenario:{name}")
            done.append({"forecast_version": fc["version"]})
    return {"scenario": name, "steps": done, "state": sess.snapshot()}
