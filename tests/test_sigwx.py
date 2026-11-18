"""WAFS SIGWX (IWXXM) layer: parser, WIFS client, route filter and the ingest service."""
import asyncio
import datetime as dt
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from app.engine.flight import LIVE_SOURCES, exposures, make_hazards, time_for_distance
from app.engine.geo import get_route
from app.wx import sigwx
from app.wx.route_filter import sigmet_crosses_route, sigwx_to_hazard
from app.wx.wifs import WifsClient, WifsError, time_window, unpack

FX = Path(__file__).parent / "fixtures" / "wifs"
TEST_DOC = (FX / "sigwx_test_20261112T12Z.xml").read_text()
WMO_DOC = (FX / "wmo_WAFS-Example.xml").read_text()
R = get_route("ICN-SFO")


def test_wmo_example_parses():
    areas = sigwx.parse(WMO_DOC)
    assert len(areas) == 1
    a = areas[0]
    assert a["kind"] == "SIGWX" and a["fir"] == "EGRR" and a["ring"] == 2 and a["qualifier"] == "MOD"
    assert a["fl"] == [340.0, 400.0] and len(a["coords"]) == 17 and a["lead_h"] == 6
    assert all(0 <= c["lon"] < 360 for c in a["coords"])


def test_codes_units_and_filtering():
    areas = {a["series"].split()[-1]: a for a in sigwx.parse(TEST_DOC)}
    assert set(areas) == {"test0001", "test0002"}                     # LGT dropped
    assert areas["test0001"]["context"] == "CAT" and areas["test0001"]["fl"] == [330.0, 390.0]
    assert areas["test0002"]["ring"] == 3 and areas["test0002"]["fl"] == [330.0, 390.0]   # metres → FL
    assert areas["test0001"]["valid_from_utc"] == "2026-11-12T10:30:00+00:00"
    assert areas["test0001"]["valid_to_utc"] == "2026-11-12T13:30:00+00:00"


def test_time_steps_do_not_overlap():
    later = TEST_DOC.replace("2026-11-12T12:00:00Z", "2026-11-12T15:00:00Z")
    areas = sigwx.parse_many([TEST_DOC, later])
    a12 = next(a for a in areas if a["valid_utc"].startswith("2026-11-12T12"))
    a15 = next(a for a in areas if a["valid_utc"].startswith("2026-11-12T15"))
    assert a12["valid_to_utc"] == a15["valid_from_utc"] == "2026-11-12T13:30:00+00:00"
    assert a12["id"] != a15["id"]


def test_route_filter_and_hazard():
    areas = sigwx.parse(TEST_DOC)
    on = [a for a in areas if sigmet_crosses_route(R, a, 120)[0]]
    assert [a["series"].split()[-1] for a in on] == ["test0001"]
    ok, d = sigmet_crosses_route(R, on[0], 120)
    h = sigwx_to_hazard(on[0], d)
    assert h["source"] == "SIGWX" and "SIGWX" in LIVE_SOURCES
    assert h["floor"] == 2 and h["fl"] == [330.0, 390.0] and h["drift_kt"] == 0.0
    assert h["t_obs_utc"] == on[0]["valid_utc"] and h["id"].startswith("SWX-KKCI-")
    assert "청천난류" in h["name"]["ko"] and "clear-air" in h["name"]["en"]
    # the hazard behaves like any other live area along the route
    dep = dt.datetime(2026, 11, 12, 6, 0, tzinfo=dt.timezone.utc)
    hz = make_hazards(R, [h], dep)
    ex = exposures(R, hz, 350, time_for_distance(350))
    assert len(ex) == 1 and ex[0].max >= 2 and abs(ex[0].d0 - d) < 200


def test_unpack_zip_and_errors():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("b.xml", "<b/>")
        z.writestr("a.xml", "<a/>")
        z.writestr("readme.txt", "x")
    assert unpack(buf.getvalue()) == ["<a/>", "<b/>"]
    assert unpack(b"") == []
    with pytest.raises(WifsError):
        unpack(b'{"error": "unauthorized"}')


def test_time_window():
    t0, t1 = time_window(dt.datetime(2026, 11, 12, 13, 41, tzinfo=dt.timezone.utc), 12)
    assert t0.hour == 12 and t1 - t0 == dt.timedelta(hours=12)


def test_wifs_client_sends_key_and_caches():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=TEST_DOC.encode())

    async def run():
        c = WifsClient("secret-key", "CabinShield-test", centre="kkci", transport=httpx.MockTransport(handler))
        now = dt.datetime(2026, 11, 12, 13, 0, tzinfo=dt.timezone.utc)
        a = await c.forecasts(now, 6)
        b = await c.forecasts(now, 6)
        await c.close()
        return a, b, c.requests

    a, b, n = asyncio.run(run())
    assert a == b == [TEST_DOC] and n == 1
    req = seen[0]
    assert req.headers["X-API-Key"] == "secret-key"
    assert req.url.path == "/wifs/api/collections/kkci_iwxxm_forecasts/locations/GLOBAL"
    assert req.url.params["dateTime"] == "2026-11-12T12:00:00Z/2026-11-12T18:00:00Z"


def _service(**kw):
    from app.config import Settings
    from app.orchestrator import SessionManager
    from app.store import Store
    from app.wx.service import WxService
    s = Settings(data_dir=":memory:", **kw)
    m = SessionManager(s, Store(":memory:"))
    return WxService(s, m), m


def test_sigwx_off_without_key():
    w, _ = _service(wx_mode="live", wifs_api_key="")
    assert w.wifs is None and w.status["sigwx_mode"] == "off"
    w2, _ = _service(wx_mode="live", wx_sigwx="live", wifs_api_key="")
    assert w2.wifs is None and w2.status["sigwx_mode"] == "off" and "CS_WIFS_API_KEY" in w2.status["sigwx_error"]["error"]
    asyncio.run(w.shutdown())
    asyncio.run(w2.shutdown())


def test_service_fixture_publishes_sigwx(client):
    w, m = _service(wx_mode="fixture")
    assert w.status["sigwx_mode"] == "fixture"

    async def run():
        first = await w.refresh("ICN-SFO")
        second = await w.refresh("ICN-SFO")
        await w.shutdown()
        return first, second

    first, second = asyncio.run(run())
    assert first["sigwx_total"] == 3 and first["sigwx_on_route"] == 1 and first["sigmets_on_route"] >= 1
    assert first["published"] and second["published"] is False
    sources = {h.get("source", "seed") for h in m.forecast("ICN-SFO")["hazards"]}
    assert {"seed", "SIGMET", "SIGWX"} <= sources
    assert m.store.wx_observations("ICN-SFO", kind="SIGWX")

    from app.config import Settings
    from app.wx.service import WxService
    client.app.state.wx = WxService(Settings(wx_mode="fixture", data_dir=":memory:"), client.app.state.manager)
    assert client.post("/api/v1/wx/refresh", params={"route_id": "ICN-SFO"}).json()["sigwx_on_route"] == 1
    areas = client.get("/api/v1/wx/ICN-SFO/areas").json()["areas"]
    assert any(a["source"] == "SIGWX" for a in areas)
    reports = client.get("/api/v1/wx/ICN-SFO/reports", params={"hours": 720}).json()["reports"]
    assert all(r["kind"] not in ("SIGMET", "SIGWX") for r in reports)
    assert client.get("/api/v1/wx/status").json()["sigwx_mode"] == "fixture"


def test_sigwx_failure_keeps_last_areas():
    w, m = _service(wx_mode="fixture")
    state = {"fail": False}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down") if state["fail"] else httpx.Response(200, content=TEST_DOC.encode())

    w.wifs = WifsClient("k", "ua", transport=httpx.MockTransport(handler), cache_ttl_s=0)

    async def run():
        await w.refresh("ICN-SFO")
        state["fail"] = True
        w.wifs._last = 0.0          # skip the 60 s spacing in the test
        st = await w.refresh("ICN-SFO")
        await w.shutdown()
        return st

    st = asyncio.run(run())
    assert st["sigwx_on_route"] == 1 and st["published"] is False
    assert w.status["sigwx_error"] and "503" in w.status["sigwx_error"]["error"]
    assert any(h.get("source") == "SIGWX" for h in m.forecast("ICN-SFO")["hazards"])
