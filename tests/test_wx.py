"""Live weather ingest (NWS Aviation Weather Center) — uses real responses captured on 2026-11-12."""
import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from app.engine.flight import exposures, make_hazards, time_for_distance
from app.engine.geo import get_route
from app.wx import normalize
from app.wx.route_filter import (cluster_reports, project_observations, report_cluster_to_hazard, route_bboxes,
                                 sigmet_crosses_route, sigmet_to_hazard)

FX = Path(__file__).parent / "fixtures" / "awc"
R = get_route("ICN-SFO")


def load(name):
    return json.loads((FX / name).read_text())


def test_intensity_mapping_and_airep():
    obs = [normalize.pirep(p) for p in load("pirep_west.json")]
    by_raw = {o["raw"][:20]: o for o in obs if o}
    assert all(o["lon"] >= 0 for o in obs if o)
    airep = next(o for o in obs if o and o["kind"] == "AIREP" and o["intensity"] == "MOD")
    assert airep["ring"] == 2
    assert normalize.ring_of_intensity("LGT-MOD") == 1 and normalize.ring_of_intensity("SEV") == 3
    assert normalize.ring_of_intensity("NEG") == 0 and normalize.ring_of_intensity("") is None
    assert len(by_raw) == 6


def test_bboxes_split_at_antimeridian():
    bbs = route_bboxes(R)
    assert len(bbs) == 2
    assert bbs[0][3] == 180.0 and bbs[1][1] == -180.0
    assert all(b[1] < b[3] for b in bbs)


def test_real_sigmet_over_oakland_oceanic_crosses_route():
    areas = [normalize.sigmet(s) for s in load("isigmet_turb.json")]
    crosses = {a["series"]: sigmet_crosses_route(R, a, 120)[0] for a in areas}
    assert crosses["ALFA 4"] is True          # Oakland Oceanic, FL250–350, on the SFO approach
    assert crosses["BRAVO 2"] is False        # Oakland Oceanic but far south (Hawaii)
    assert crosses["4"] is False              # New Zealand
    a = next(a for a in areas if a["series"] == "ALFA 4")
    ok, d = sigmet_crosses_route(R, a, 120)
    h = sigmet_to_hazard(a, d)
    assert ok and h["floor"] == 3 and h["fl"] == [250.0, 350.0] and h["drift_kt"] == 20.0 and h["drift_dir_deg"] == 90


def test_floor_and_validity_window():
    dep = dt.datetime(2026, 11, 12, 11, 40, tzinfo=dt.timezone.utc)
    p = R.pos_at(1000)
    spec = {"id": "X", "name": {"ko": "x", "en": "x"}, "fl": [300, 400], "source": "SIGMET",
            "centre_ll": {"lat": p.lat, "lon": p.lon}, "t_obs_utc": dep.isoformat(),
            "vertices_nm": [[80, 80], [80, -80], [-80, -80], [-80, 80]], "floor": 3,
            "valid_from_utc": dep.isoformat(), "valid_to_utc": (dep + dt.timedelta(hours=10)).isoformat()}
    hz = make_hazards(R, [spec], dep)
    assert hz[0].ring_of(hz[0].centre(0), R.pos_at(1060)) == 3   # outer ring still severe (floor)
    ex = exposures(R, hz, 350, time_for_distance(350))
    assert len(ex) == 1 and ex[0].max == 3 and 900 < ex[0].d0 < 1000
    spec["valid_to_utc"] = (dep + dt.timedelta(minutes=30)).isoformat()   # expires before we get there
    assert exposures(R, make_hazards(R, [spec], dep), 350, time_for_distance(350)) == []


def test_report_cluster_becomes_hazard():
    now = "2026-11-12T08:00:00+00:00"
    obs = []
    for i, d in enumerate((2000, 2040, 2090)):
        p = R.pos_at(d)
        obs.append({"id": f"o{i}", "kind": "AIREP", "obs_utc": now, "lat": p.lat, "lon": p.lon, "fl": 350, "ring": 2,
                    "intensity": "MOD", "raw": "x"})
    on = project_observations(R, obs, 120)
    groups = cluster_reports(on)
    assert len(groups) == 1
    h = report_cluster_to_hazard(groups[0], R)
    assert h["source"] == "PIREP" and h["floor"] == 2 and h["fl"] == [310, 390]
    assert report_cluster_to_hazard(groups[0][:1], R) is None   # single MOD report is not enough


def test_service_with_fixture(monkeypatch):
    from app.config import Settings
    from app.orchestrator import SessionManager
    from app.store import Store
    from app.wx.service import WxService
    s = Settings(wx_mode="fixture", data_dir=":memory:")
    m = SessionManager(s, Store(":memory:"))

    async def run():
        w = WxService(s, m)
        first = await w.refresh("ICN-SFO")
        second = await w.refresh("ICN-SFO")
        await w.shutdown()
        return first, second

    first, second = asyncio.run(run())
    assert first["published"] and first["sigmets_on_route"] >= 1 and first["requests"] == 3
    assert second["published"] is False                       # unchanged data → no new version
    fc = m.forecast("ICN-SFO")
    assert {h.get("source", "seed") for h in fc["hazards"]} >= {"seed", "SIGMET"}
    assert m.store.wx_observations("ICN-SFO")


def test_wx_api(client):
    from app.config import Settings
    from app.wx.service import WxService
    client.app.state.wx = WxService(Settings(wx_mode="fixture", data_dir=":memory:"), client.app.state.manager)
    r = client.post("/api/v1/wx/refresh", params={"route_id": "ICN-SFO"})
    assert r.status_code == 200 and r.json()["sigmets_on_route"] >= 1
    assert client.get("/api/v1/wx/status").json()["routes"]["ICN-SFO"]["forecast_version"] >= 2
    areas = client.get("/api/v1/wx/ICN-SFO/areas").json()["areas"]
    assert any(a["source"] == "SIGMET" for a in areas)
    assert client.get("/api/v1/wx/ICN-SFO/reports", params={"hours": 720}).status_code == 200


def test_history_counts_live_reports(client):
    st = client.app.state.manager.store
    p = R.pos_at(1000)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    st.add_wx_observations("ICN-SFO", [{"id": "t1", "kind": "PIREP", "obs_utc": now, "lat": p.lat, "lon": p.lon, "fl": 350,
                                        "ring": 2, "d_nm": 1000.0, "xtk_nm": 1.0, "raw": "TEST"}])
    seg = next(h for h in client.get("/api/v1/routes/ICN-SFO/history").json()["segments"] if h["d0_nm"] - 50 <= 1000 <= h["d1_nm"] + 50)
    assert seg["sources"].get("PIREP") == 1
