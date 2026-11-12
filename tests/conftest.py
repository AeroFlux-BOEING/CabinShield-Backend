import os

import pytest

os.environ["CS_DATA_DIR"] = ":memory:"
os.environ["CS_API_KEY"] = ""
os.environ["CS_OPS_API_KEY"] = ""
os.environ["CS_PLANNER"] = "rule"
os.environ["CS_WX_MODE"] = "off"


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def flight(client):
    r = client.post("/api/v1/flights", json={"flight_no": "AFX017", "date": "2026-11-12", "autostart": True})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    client.post(f"/api/v1/flights/{fid}/sim/stop")
    return fid
