import uuid


def test_health_and_reference(client):
    assert client.get("/healthz").json()["ok"]
    ref = client.get("/api/v1/reference")
    assert ref.status_code == 200 and len(ref.json()["protocol"]["points"]) == 3
    assert client.get("/api/v1/reference", headers={"if-none-match": ref.headers["etag"]}).status_code == 304


def test_schedule_lookup(client):
    r = client.get("/api/v1/schedule/afx-011", params={"date": "2026-11-12"}).json()  # Thursday
    assert r["flight"]["flight_no"] == "AFX011" and r["operates"] is False
    assert r["next_operating_date"] == "2026-11-13"
    assert client.get("/api/v1/schedule/XX999").status_code == 404


def test_create_flight_validation(client):
    assert client.post("/api/v1/flights", json={"flight_no": "AFX201", "date": "2026-11-12"}).json()["detail"]["code"] == "ROUTE_NOT_SUPPORTED"
    r = client.post("/api/v1/flights", json={"flight_no": "AFX011", "date": "2026-11-12"})
    assert r.status_code == 422 and r.json()["detail"]["next_operating_date"] == "2026-11-13"


def test_briefing_package(client, flight):
    b = client.get(f"/api/v1/flights/{flight}/briefing")
    assert b.status_code == 200
    body = b.json()
    assert body["route"]["route_id"] == "ICN-SFO" and len(body["exposures"]) == 3
    assert [p["point"] for p in body["exposures"][0]["points"]] == [1, 2, 3]
    assert body["model"]["version"] == "cst-mlp-v1" and len(body["insights"]) == 3
    assert client.get(f"/api/v1/flights/{flight}/briefing", headers={"if-none-match": b.headers["etag"]}).status_code == 304


def test_points_progress_and_events(client, flight):
    j = client.post(f"/api/v1/flights/{flight}/sim/jump-next").json()
    assert j["jumped"] and j["state"]["point"]["stage"] == "NORMAL"
    s = client.post(f"/api/v1/flights/{flight}/sim/step", params={"seconds": 600}).json()
    assert s["point"]["stage"] == "P1" and s["tta"]["q_low_s"] < s["tta"]["tta_mean_s"]
    s = client.post(f"/api/v1/flights/{flight}/sim/step", params={"seconds": 1300}).json()
    assert s["point"]["stage"] in ("P3", "STAY")
    kinds = [e["kind"] for e in client.get(f"/api/v1/flights/{flight}/log").json()]
    assert "POINT_CHANGED" in kinds


def test_sync_is_idempotent_and_audited(client, flight):
    st = client.get(f"/api/v1/flights/{flight}/state").json()
    ai = st["ground_ai"]
    from app.engine.cst import features
    x = features(ai["cabin"])
    ev = [{"id": str(uuid.uuid4()), "kind": "decision", "t_s": st["t_s"],
           "payload": {"features": x, "q_low_s": 200, "pick": 0, "model_version": "cst-mlp-v1"}},
          {"id": str(uuid.uuid4()), "kind": "cabin", "t_s": st["t_s"], "payload": {"serviceStopped": True}},
          {"id": str(uuid.uuid4()), "kind": "log", "t_s": st["t_s"], "payload": {"msg": {"ko": "테스트", "en": "test"}}}]
    r = client.post(f"/api/v1/flights/{flight}/sync", json={"device_id": "ipad-1", "forecast_version": 0, "events": ev}).json()
    assert r["accepted"] == 3 and r["duplicates"] == 0 and r["forecast_stale"] is True
    assert len(r["verdicts"]) == 1
    again = client.post(f"/api/v1/flights/{flight}/sync", json={"device_id": "ipad-1", "events": ev}).json()
    assert again["accepted"] == 0 and again["duplicates"] == 3
    audit = client.get(f"/api/v1/flights/{flight}/audit").json()
    assert audit["summary"]["decisions"] == 1
    assert client.get(f"/api/v1/flights/{flight}/state").json()["cabin_actions"]["serviceStopped"] is True


def test_forecast_publish_reaches_flight(client, flight):
    v0 = client.get("/api/v1/forecast/ICN-SFO").json()["version"]
    assert client.get("/api/v1/forecast/ICN-SFO", params={"since": v0}).status_code == 204
    r = client.post(f"/api/v1/flights/{flight}/scenarios/popup-hazard").json()
    assert r["steps"][0]["forecast_version"] == v0 + 1
    st = client.get(f"/api/v1/flights/{flight}/state").json()
    assert st["links"]["forecast_version"] == st["links"]["uplinked_version"] == v0 + 1
    assert len(client.get(f"/api/v1/flights/{flight}/briefing").json()["exposures"]) >= 3


def test_link_loss_queues_forecast(client, flight):
    client.post(f"/api/v1/flights/{flight}/sim/control", json={"sat": False})
    h = {"name": {"ko": "테스트", "en": "test"}, "fl": [300, 400], "f": 0.9, "r_nm": 60}
    v = client.post("/api/v1/forecast/ICN-SFO/hazards", json=h).json()["version"]
    st = client.get(f"/api/v1/flights/{flight}/state").json()
    assert st["links"]["forecast_version"] == v and st["links"]["uplinked_version"] < v
    st = client.post(f"/api/v1/flights/{flight}/sim/control", json={"sat": True}).json()
    assert st["links"]["uplinked_version"] == v


def test_reports_feed_history(client, flight):
    before = {h["id"]: h["cases"] for h in client.get("/api/v1/routes/ICN-SFO/history").json()["segments"]}
    r = client.post(f"/api/v1/flights/{flight}/reports", json={"t_s": 5000, "severity": 2, "d_nm": 1000, "fl": 350})
    assert r.status_code == 201
    after = {h["id"]: h["cases"] for h in client.get("/api/v1/routes/ICN-SFO/history").json()["segments"]}
    assert sum(after.values()) == sum(before.values()) + 1


def test_model_distribution(client):
    man = client.get("/api/v1/models/cst").json()["latest"]
    w = client.get(f"/api/v1/models/cst/{man['version']}")
    assert w.status_code == 200 and len(w.json()["W"]) == 3
    r = client.post("/api/v1/models/cst/evaluate", json={"features": [0.0] * 38, "q_low_s": 120}).json()
    assert r["pick_id"] in ("FULL", "MINIMAL", "IMMEDIATE")


def test_websocket_stream(client, flight):
    with client.websocket_connect(f"/ws/flights/{flight}") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["data"]["flight_id"] == flight


def test_ops_overview(client, flight):
    o = client.get("/api/v1/ops/overview").json()["flights"]
    assert any(f["flight_id"] == flight for f in o)


def test_api_key_enforced(client):
    from app.config import get_settings
    s = get_settings()
    s.api_key = "secret"
    try:
        body = {"flight_no": "AFX017", "date": "2026-11-12", "autostart": False}
        assert client.post("/api/v1/flights", json=body).status_code == 401
        assert client.post("/api/v1/flights", json=body, headers={"X-API-Key": "secret"}).status_code == 201
        assert client.get("/api/v1/schedule").status_code == 200  # reads stay open for the tablet cache
    finally:
        s.api_key = ""


def test_aircraft_feed_projects_position(client):
    r = client.post("/api/v1/flights", json={"flight_no": "AFX023", "date": "2026-11-14", "feed": "AIRCRAFT_FEED"})
    fid = r.json()["id"]
    from app.engine.geo import get_route
    p = get_route("ICN-SFO").pos_at(1200)
    s = client.post(f"/api/v1/flights/{fid}/position", json={"t_s": 9000, "lat": p.lat, "lon": p.lon, "gs_kt": 480}).json()
    assert abs(s["position"]["d_nm"] - 1200) < 1 and s["status"] == "IN_FLIGHT" and s["t_s"] == 9000


def test_real_sfo_flights_in_schedule(client):
    rows = {r["flight_no"]: r for r in client.get("/api/v1/schedule").json()}
    for no in ("KE023", "UA892", "OZ212", "UA806", "YP111"):
        assert rows[no]["real"] and rows[no]["route_id"] == "ICN-SFO" and rows[no]["supported"]
    assert rows["AFX017"]["real"] is False and rows["AFX017"]["airline"] is None
    r = client.get("/api/v1/schedule/ke-023", params={"date": "2026-11-19"}).json()
    assert r["flight"]["airline"]["en"] == "Korean Air" and r["operates"]
    f = client.post("/api/v1/flights", json={"flight_no": "OZ212", "date": "2026-11-19"})
    assert f.status_code == 201 and f.json()["id"] == "OZ212-20261119" and f.json()["aircraft"] == "A350-900"
    assert client.get("/api/v1/flights/OZ212-20261119/briefing").status_code == 200   # B787-9 cabin model fallback
