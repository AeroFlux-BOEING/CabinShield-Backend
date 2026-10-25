import os

os.environ.setdefault("CS_DATA_DIR", "/tmp/cabinshield-test")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_end_to_end_flow():
    with TestClient(app) as c:
        assert c.get("/healthz").json()["ok"]
        assert len(c.get("/api/v1/library").json()) >= 8

        r = c.post("/api/v1/flights", json={"scenario": "A320_HOT_BEVERAGE", "flight_id": "KE001"})
        assert r.status_code == 201 and r.json()["phase"] == "MONITORING"

        # 시뮬레이터 수동 진행: 140s → 60s 남은 시점까지
        with c.websocket_connect("/ws/flights/KE001") as ws:
            assert ws.receive_json()["type"] == "state"
            snap = c.post("/api/v1/flights/KE001/sim/step", params={"seconds": 30}).json()
            assert snap["phase"] == "ADVISORY"
            msgs = [ws.receive_json() for _ in range(2)]
            assert any(m.get("event") == "ADVISORY_RECEIVED" for m in msgs)

        snap = c.post("/api/v1/flights/KE001/sim/step", params={"seconds": 50}).json()
        d = snap["decision"]
        assert d["recommended"]["plan"]["plan_id"] == "RULE-MINIMAL"

        # 객실상태 변경(카트가 이미 수납됨) → 재계산
        cabin = snap["cabin"]
        cabin["carts"][0].update(status="STOWED", hot_liquid=False)
        cabin["service_active"] = False
        snap = c.put("/api/v1/flights/KE001/cabin-state", json=cabin).json()
        assert snap["decision"]["recommended"]["csm_s"] is not None

        audit = c.get("/api/v1/flights/KE001/audit").json()
        kinds = {a["kind"] for a in audit}
        assert {"ADVISORY_RECEIVED", "DECISION", "CABIN_UPDATE"} <= kinds


def test_ops_feed_and_stateless():
    with TestClient(app) as c:
        c.post("/api/v1/flights", json={"scenario": "A320_HOT_BEVERAGE", "flight_id": "KE002", "initial_tta_s": 0})
        snap = c.post("/api/v1/flights/KE002/advisories",
                      json={"advisory_id": "OCC-1", "tta_mean_s": 45, "tta_sigma_s": 5}).json()
        assert snap["decision"]["tta"]["source"] == "OPS_FEED"
        c.post("/api/v1/flights/KE002/sim/stop")

        cab = c.get("/api/v1/scenarios").json()["A320_HOT_BEVERAGE"]["cabin"]
        d = c.post("/api/v1/evaluate", json={"cabin": cab, "tta_mean_s": 120}).json()
        assert d["band"] == "GREEN"
