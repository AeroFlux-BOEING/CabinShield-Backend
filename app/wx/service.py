"""Periodic live-weather ingest: fetch → normalise → keep the route corridor → publish a new forecast version.

Every observation is stored (wx_observations) so the 'past similar reports' layer grows over time.
Failures never break the app: the last good forecast stays, and /api/v1/wx/status shows the error.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from app import __version__
from app.config import Settings
from app.engine.flight import LIVE_SOURCES
from app.engine.geo import get_route
from app.store import now_utc
from app.wx import normalize
from app.wx.awc import AwcClient, fixture_transport
from app.wx.route_filter import (cluster_reports, project_observations, report_cluster_to_hazard, route_bboxes,
                                 sigmet_crosses_route, sigmet_to_hazard)

log = logging.getLogger("cabinshield.wx")
DEFAULT_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "awc"


class WxService:
    def __init__(self, settings: Settings, manager):
        self.s = settings
        self.m = manager
        transport = None
        if settings.wx_mode == "fixture":
            transport = fixture_transport(Path(settings.wx_fixture_dir) if settings.wx_fixture_dir else DEFAULT_FIXTURES)
        ua = f"CabinShield-PoC/{__version__} (AeroFlux BOEING DAY{'; ' + settings.wx_contact if settings.wx_contact else ''})"
        self.client = AwcClient(ua, transport=transport)
        self.status: dict = {"mode": settings.wx_mode, "routes": {}, "last_error": None}
        self._task: asyncio.Task | None = None

    @property
    def routes(self) -> list[str]:
        return [r.strip() for r in self.s.wx_routes.split(",") if r.strip()]

    async def refresh(self, route_id: str) -> dict:
        route = get_route(route_id)
        raw_p: list = []
        for bb in route_bboxes(route):
            raw_p += await self.client.pireps(bb, self.s.wx_pirep_age_h)
        raw_s = await self.client.isigmets("turb")

        obs = [o for o in (normalize.pirep(p) for p in raw_p) if o]
        on_route = project_observations(route, obs, self.s.wx_corridor_nm)
        areas = [a for a in (normalize.sigmet(x) for x in raw_s) if a]
        live: list[dict] = []
        crossing = []
        for a in areas:
            ok, d = sigmet_crosses_route(route, a, self.s.wx_corridor_nm)
            if ok:
                crossing.append(a)
                live.append(sigmet_to_hazard(a, d))
        if self.s.wx_pirep_hazards:
            for g in cluster_reports(on_route):
                h = report_cluster_to_hazard(g, route)
                if h:
                    live.append(h)

        self.m.store.add_wx_observations(route_id, on_route + [{**a, "d_nm": None} for a in crossing])
        published = self._publish(route_id, live)
        st = {"fetched_at": now_utc().isoformat(), "pireps_total": len(raw_p), "turb_reports": len(obs),
              "reports_on_route": len(on_route), "sigmets_total": len(raw_s), "sigmets_on_route": len(crossing),
              "live_hazards": len(live), "forecast_version": published["version"], "published": published["new"],
              "requests": self.client.requests}
        self.status["routes"][route_id] = st
        self.status["last_error"] = None
        return st

    def _publish(self, route_id: str, live: list[dict]) -> dict:
        cur = self.m.forecast(route_id)
        keep = [h for h in cur["hazards"] if h.get("source", "seed") not in LIVE_SOURCES]
        if not self.s.wx_keep_seed:
            keep = [h for h in keep if h.get("source", "seed") != "seed"]
        old_live = [h for h in cur["hazards"] if h.get("source") in LIVE_SOURCES]
        digest = lambda hs: hashlib.sha1(json.dumps(sorted(hs, key=lambda h: h["id"]), sort_keys=True).encode()).hexdigest()
        if digest(old_live) == digest(live) and len(keep) == len(cur["hazards"]) - len(old_live):
            return {"version": cur["version"], "new": False}
        fc = self.m.publish_forecast(route_id, keep + live, "awc")
        return {"version": fc["version"], "new": True}

    async def refresh_all(self) -> None:
        for r in self.routes:
            try:
                await self.refresh(r)
            except Exception as exc:  # noqa: BLE001 — keep the last good forecast
                self.status["last_error"] = {"at": now_utc().isoformat(), "route": r, "error": f"{type(exc).__name__}: {exc}"}
                log.warning("wx refresh failed for %s: %s", r, exc)

    async def run(self) -> None:
        while True:
            await self.refresh_all()
            await asyncio.sleep(self.s.wx_interval_s)

    def start(self) -> None:
        if self.s.wx_mode != "off":
            self._task = asyncio.create_task(self.run())

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.close()
