"""Async client for the WAFS Internet File Service (WIFS) SIGWX API — NOAA AWC.

Access needs an approved WIFS account (State Meteorological Authority + FAA) and an API key
(`CS_WIFS_API_KEY`, sent as X-API-Key). Without a key the SIGWX layer stays off and the rest of
the weather ingest works as before.

    GET https://aviationweather.gov/wifs/api/collections/{kkci|egrr}_iwxxm_forecasts/locations/GLOBAL
        ?dateTime=<start>/<end>

Limits (AWC): 100 req/min overall, 1 req/min per thread on API endpoints, 5,000 data requests/day.
New data four times a day (≈ 05:50 / 11:50 / 17:50 / 23:50 UTC), so responses are cached for 30 min.
The response may be one IWXXM document or a zip of several (one per time step).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import time
import zipfile
from pathlib import Path

import httpx

BASE_URL = "https://aviationweather.gov"
MIN_INTERVAL_S = 60.0
STEP = dt.timedelta(hours=3)


class WifsError(RuntimeError):
    pass


def unpack(body: bytes) -> list[str]:
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            return [z.read(n).decode("utf-8") for n in sorted(z.namelist()) if n.lower().endswith(".xml")]
    text = body.decode("utf-8", "replace").lstrip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        raise WifsError(f"JSON instead of IWXXM: {text[:200]}")
    return [text]


def time_window(now: dt.datetime, hours_ahead: float) -> tuple[dt.datetime, dt.datetime]:
    """From the 3-hourly step at/before now to the step covering now + hours_ahead."""
    t0 = now.replace(minute=0, second=0, microsecond=0)
    t0 -= dt.timedelta(hours=t0.hour % 3)
    n = int(-(-hours_ahead // 3))
    return t0, t0 + n * STEP


class WifsClient:
    def __init__(self, api_key: str, user_agent: str, centre: str = "kkci", base_url: str = BASE_URL,
                 timeout_s: float = 30.0, transport: httpx.AsyncBaseTransport | None = None, cache_ttl_s: float = 1800.0):
        self.centre = centre
        headers = {"User-Agent": user_agent}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport, headers=headers)
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._ttl = cache_ttl_s
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.requests = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def forecasts(self, now: dt.datetime, hours_ahead: float = 12.0) -> list[str]:
        t0, t1 = time_window(now, hours_ahead)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        path = f"/wifs/api/collections/{self.centre}_iwxxm_forecasts/locations/GLOBAL"
        params = {"dateTime": f"{iso(t0)}/{iso(t1)}"}
        key = path + "?" + params["dateTime"]
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        async with self._lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
            if self._last and wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        self.requests += 1
        r = await self._client.get(path, params=params)
        if r.status_code == 204:
            docs: list[str] = []
        elif r.status_code == 200:
            docs = unpack(r.content)
        else:
            raise WifsError(f"{path} → HTTP {r.status_code}: {r.text[:200]}")
        self._cache[key] = (time.monotonic(), docs)
        return docs


def fixture_transport(folder: Path) -> httpx.MockTransport:
    """Serves the IWXXM files in `folder` (tests, offline demo) as one zip."""
    def handler(req: httpx.Request) -> httpx.Response:
        files = sorted(folder.glob("*.xml"))
        if not files:
            return httpx.Response(204)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in files:
                z.writestr(f.name, f.read_bytes())
        return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "application/zip"})
    return httpx.MockTransport(handler)
