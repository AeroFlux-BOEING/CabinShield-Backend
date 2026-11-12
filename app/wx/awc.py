"""Thin async client for the aviationweather.gov Data API with rate limiting and a small TTL cache."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("cabinshield.awc")

BASE_URL = "https://aviationweather.gov"
MAX_PER_MIN = 30  # well below the documented 100/min


class AwcError(RuntimeError):
    pass


class AwcClient:
    def __init__(self, user_agent: str, base_url: str = BASE_URL, timeout_s: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None, cache_ttl_s: float = 120.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport,
                                         headers={"User-Agent": user_agent, "Accept": "application/json"})
        self._calls: list[float] = []
        self._cache: dict[str, tuple[float, list]] = {}
        self._ttl = cache_ttl_s
        self._lock = asyncio.Lock()
        self.requests = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> list:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        async with self._lock:
            now = time.monotonic()
            self._calls = [c for c in self._calls if now - c < 60]
            if len(self._calls) >= MAX_PER_MIN:
                await asyncio.sleep(60 - (now - self._calls[0]))
            self._calls.append(time.monotonic())
        self.requests += 1
        r = await self._client.get(path, params=params)
        if r.status_code == 204:
            data: list = []
        elif r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):  # {"status": "error", ...}
                raise AwcError(str(data))
        else:
            raise AwcError(f"{path} → HTTP {r.status_code}: {r.text[:200]}")
        self._cache[key] = (time.monotonic(), data)
        return data

    async def pireps(self, bbox: tuple[float, float, float, float], age_h: float) -> list:
        """PIREPs + AIREPs in bbox (lat0, lon0, lat1, lon1), lon in −180…180 and lon0 < lon1."""
        return await self._get("/api/data/pirep", {"format": "json", "age": int(age_h),
                                                    "bbox": ",".join(f"{v:g}" for v in bbox)})

    async def isigmets(self, hazard: str = "turb") -> list:
        return await self._get("/api/data/isigmet", {"format": "json", "hazard": hazard})

    async def airsigmets(self, hazard: str = "turb") -> list:
        return await self._get("/api/data/airsigmet", {"format": "json", "hazard": hazard})


def fixture_transport(folder: Path) -> httpx.MockTransport:
    """Serves saved API responses (tests, offline demo). bbox east of 0° → pirep_west.json, else pirep_east.json."""
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/pirep"):
            lon0 = float(req.url.params["bbox"].split(",")[1])
            name = "pirep_west.json" if lon0 >= 0 else "pirep_east.json"
        elif p.endswith("/isigmet"):
            name = "isigmet_turb.json"
        else:
            return httpx.Response(204)
        f = folder / name
        return httpx.Response(200, json=json.loads(f.read_text())) if f.exists() else httpx.Response(204)
    return httpx.MockTransport(handler)
