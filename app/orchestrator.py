"""Flight sessions: the ground-side mirror of what each crew tablet is doing.

A `FlightSession` tracks one flight: aircraft position (from the simulator or a real AID/ACARS
feed), the latest forecast, the next segment, TTA / Q_low, the crew point stage, the service plan
and what the tablet has synced. It emits events (point changes, segment entry, forecast uplinks)
to WebSocket subscribers and to the server log. The tablet stays authoritative and works offline;
this mirror powers the ops console, the forecast uplink and the audit trail.

The physics and timing are a 1:1 port of the app's `FlightSim`, so a session started with the same
flight shows the same points at the same times as the tablet.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from app import library
from app.config import Settings
from app.engine import cst as cstmod
from app.engine.flight import (PLAN_GS, START_NM, Exposure, LookAhead, exposures, flight_level, ground_speed, look_ahead,
                               make_hazards, time_for_distance)
from app.engine.geo import LatLon, get_route
from app.engine.tta import forecast_sigma_nm, point_stage, point_timeline, position_sigma_nm, tta_estimate
from app.kernel import evaluate as kernel_evaluate
from app.planner import MODE_LABEL, plan_services
from app.store import Store, now_utc

log = logging.getLogger("cabinshield.orchestrator")

NO_ACTIONS = {"serviceStopped": False, "cartsSecured": False, "hotStowed": False, "galleySecured": False, "lavsCleared": False}


def tx(ko: str, en: str) -> dict:
    return {"ko": ko, "en": en}


def minutes_tx(s: float) -> dict:
    m = max(1, round(s / 60))
    if m >= 60:
        h, mm = divmod(m, 60)
        return tx(f"{h}시간 {mm}분" if mm else f"{h}시간", f"{h} h {mm} min" if mm else f"{h} h")
    return tx(f"{m}분", f"{m} min")


class FlightSession:
    def __init__(self, flight: dict, settings: Settings, store: Store, forecast: dict):
        self.flight = flight
        self.s = settings
        self.store = store
        self.route = get_route(flight["route_id"])
        self.leads = library.point_leads()
        self.ac = library.aircraft()[flight["aircraft"]] if flight["aircraft"] in library.aircraft() else library.aircraft()["B787-9"]
        self.subscribers: set[asyncio.Queue] = set()
        self.running = False
        self.speed = settings.sim_speed
        self._last_push = 0.0
        self.set_forecast(forecast, announce=False)
        self.reset()

    # ------------------------------------------------------------ lifecycle
    @property
    def id(self) -> str:
        return self.flight["id"]

    def reset(self) -> None:
        t = time_for_distance(START_NM)
        g = ground_speed(self.route, START_NM, t)
        self.t, self.d, self.gs, self.gs_est, self.est_d = t, START_NM, g, g, START_NM
        self.loss_d, self.aid, self.sat, self.last_uplink = 0.0, True, True, t
        self.uplinked_version = self.forecast_version
        self.look: LookAhead | None = None
        self.cur = self.g = 0
        self.after_until = -1.0
        self.arrived = False
        self.ex: list[Exposure] = []
        self.plan: list[dict] | None = None
        self.log: list[dict] = []
        self.cabin_actions = dict(NO_ACTIONS)
        self.tablet: dict[str, Any] = {"device_id": None, "last_sync_at": None, "point": None, "forecast_version": None}
        self._last_plan_t = -1e9
        self._last_service: str | None = None
        self._forecast_hazard = None
        self._started = False

    def start(self) -> None:
        self.reset()
        self.running = True
        self.flight["status"] = "IN_FLIGHT"
        self.store.upsert_flight(self.flight)
        no = self.flight["flight_no"]
        self.emit("FLIGHT_STARTED", tx(f"{no} 비행편 시작 · 인천 {self.clock(0)} UTC 출발", f"{no} started · departed Incheon {self.clock(0)} UTC"))
        self.evaluate()
        self.update_plan(force=True)
        self._started = True

    # ------------------------------------------------------------ clocks
    @property
    def dep_utc(self) -> dt.datetime:
        d = self.flight["dep_utc"]
        return d if isinstance(d, dt.datetime) else dt.datetime.fromisoformat(d)

    def clock(self, t: float) -> str:
        x = self.dep_utc + dt.timedelta(seconds=t)
        return x.strftime("%H:%M")

    def utc_at(self, t: float) -> str:
        return (self.dep_utc + dt.timedelta(seconds=t)).isoformat()

    @property
    def eta(self) -> float:
        return self.t + (self.route.length_nm - self.est_d) / PLAN_GS * 3600

    @property
    def gs_shown(self) -> float:
        return self.gs if self.aid else self.gs_est

    @property
    def t_entry(self) -> float | None:
        return self.look.first.get(1) if self.look and self.cur == 0 else None

    @property
    def near(self) -> bool:
        return bool(self.look) and (self.look.cur > 0 or self.look.first.get(1, 1e9) <= self.s.sim_slow_near_s)

    @property
    def effective_speed(self) -> float:
        return min(self.speed, self.s.sim_slow_speed) if self.near else self.speed

    # ------------------------------------------------------------ forecast
    def set_forecast(self, fc: dict, announce: bool = True) -> None:
        self.forecast_version = fc["version"]
        self.forecast_issued_at = fc["issued_at"]
        self.hazards = make_hazards(self.route, fc["hazards"], self.dep_utc, self.live_shift_s())
        self._forecast_hazard = None
        if announce:
            self._last_plan_t = -1e9
            if self.sat:
                self.uplink()
            else:
                self.emit("FORECAST_PENDING", tx(f"새 예보 v{fc['version']} 대기 · 지상 연결 끊김", f"New forecast v{fc['version']} queued · ground link down"))

    def live_shift_s(self) -> float:
        """Replay mode (simulator only): live SIGMET/PIREP times are moved so 'now' = the sim's current time."""
        if self.s.wx_time_mode != "replay" or self.flight["feed"] != "SIMULATOR":
            return 0.0
        t = getattr(self, "t", time_for_distance(START_NM))
        return (self.dep_utc + dt.timedelta(seconds=t) - now_utc()).total_seconds()

    def uplink(self) -> None:
        self.last_uplink = self.t
        if self.uplinked_version != self.forecast_version:
            self.uplinked_version = self.forecast_version
            self.emit("FORECAST_UPDATED", tx(f"최신 흔들림 예보 v{self.forecast_version} 전송", f"Latest turbulence forecast v{self.forecast_version} sent"),
                      {"forecast_version": self.forecast_version})
            self.evaluate()
            self.update_plan(force=True)

    # ------------------------------------------------------------ simulation
    def _step(self, dt_s: float) -> None:
        gs = ground_speed(self.route, self.d, self.t)
        self.gs = gs
        self.d = min(self.route.length_nm, self.d + gs * dt_s / 3600)
        self.t += dt_s
        if self.aid:
            self.est_d = self.d
            self.gs_est += (gs - self.gs_est) * min(1.0, dt_s / 600)
        else:
            self.est_d = min(self.route.length_nm, self.est_d + self.gs_est * dt_s / 3600)
        if self.sat and self.t - self.last_uplink >= self.s.uplink_interval_s:
            self.uplink()
        if self.d >= self.route.length_nm and not self.arrived:
            self.arrived = True
            self.running = False
            self.flight["status"] = "ARRIVED"
            self.store.upsert_flight(self.flight)
            self.emit("ARRIVED", tx("샌프란시스코 도착", "Arrived in San Francisco"))

    def advance_sim(self, sim_seconds: float) -> None:
        """Move the simulated aircraft forward by sim_seconds (5 s sub-steps, like the app)."""
        left, since_eval = sim_seconds, 0.0
        while left > 0 and not self.arrived:
            h = min(left, 5.0)
            self._step(h)
            left -= h
            since_eval += h
            # every sub-step near a segment (point timing), otherwise once per sim minute (cheaper)
            if self.near or since_eval >= 60 or left <= 0:
                self.evaluate()
                since_eval = 0.0
        self.update_plan()

    def tick(self, real_dt: float) -> None:
        if self.running and not self.arrived and self.flight["feed"] == "SIMULATOR":
            self.advance_sim(min(real_dt, 2.0) * self.effective_speed)

    def ingest_position(self, p: dict) -> None:
        """Real aircraft feed: project the reported position on the route and adopt its time base."""
        d, xtk = self.route.project(LatLon(p["lat"], p["lon"] % 360))
        t = p.get("t_s")
        if t is None:
            t = (now_utc() - self.dep_utc).total_seconds()
        dt_s = max(0.0, t - self.t)
        self.gs = p.get("gs_kt") or (abs(d - self.d) / dt_s * 3600 if dt_s > 0 else self.gs)
        self.gs_est += (self.gs - self.gs_est) * min(1.0, dt_s / 600) if dt_s else 0
        self.d = self.est_d = d
        self.t = max(self.t, t)
        self.aid = True
        self.xtk_nm = xtk
        if self.flight["status"] == "SCHEDULED":
            self.flight["status"] = "IN_FLIGHT"
            self.store.upsert_flight(self.flight)
        self.evaluate()
        self.update_plan()

    def evaluate(self) -> None:
        L = look_ahead(self.route, self.hazards, self.est_d, self.t, self.gs_shown)
        self.look = L
        cur = L.cur if L else 0
        if (cur > 0) != (self.cur > 0):
            if cur > 0:
                self.emit("SEGMENT_ENTERED", tx("난기류 구간 진입 · 3포인트 착석 유지", "Entered turbulence segment · Point 3, stay seated"))
            else:
                self.emit("SEGMENT_CLEARED", tx("난기류 구간을 벗어났습니다", "Left the turbulence segment"))
                self.after_until = self.t + 600
        self.cur = cur
        tE = self.t_entry
        g = point_stage(tE, cur > 0, self.leads)
        if L and L.hazard is not self._forecast_hazard and cur == 0:
            self._forecast_hazard = L.hazard
            m = minutes_tx(L.first[1])
            self.emit("SEGMENT_AHEAD", tx(f"다음 난기류 구간 확인 · {m['ko']} 뒤", f"Next turbulence segment found · in {m['en']}"),
                      {"hazard_id": L.hazard.id, "t_entry_s": L.first[1]})
        if self._started and g > self.g and cur == 0 and tE is not None:
            P = library.protocol()["points"][g - 1]
            m = minutes_tx(tE)
            self.emit("POINT_CHANGED", tx(f"{g}포인트 시작 · {P['name']['ko']} · 구간 진입 {m['ko']} 전",
                                          f"Point {g} started · {P['name']['en']} · {m['en']} before the segment"), {"point": g})
        self.g = g
        svc = self.current_service_id()
        if svc != self._last_service:
            if svc:
                self.cabin_actions = dict(NO_ACTIONS)
            self._last_service = svc

    def update_plan(self, force: bool = False) -> None:
        if not force and self.t - self._last_plan_t < 600 and self.plan:
            return
        self._last_plan_t = self.t
        self.ex = exposures(self.route, self.hazards, self.est_d, self.t)
        np_ = plan_services(library.services(), self.t if self.plan else 0, self.eta, self.ex, self.plan)
        if self.plan:
            for n in np_:
                o = next((q for q in self.plan if q["id"] == n["id"]), None)
                if o and (o["start_s"] != n["start_s"] or o["mode"] != n["mode"]) and not (o["start_s"] is not None and o["start_s"] <= self.t):
                    frm = self.clock(o["start_s"]) if o["start_s"] is not None else "—"

                    def to(lang: str, n=n) -> str:
                        base = self.clock(n["start_s"]) if n["start_s"] is not None else MODE_LABEL[n["mode"]][lang]
                        return base + (f" ({MODE_LABEL['simplified'][lang]})" if n["mode"] == "simplified" else "")
                    self.emit("PLAN_CHANGED", tx(f"{n['name']['ko']} 시간 변경 · {frm} → {to('ko')}", f"{n['name']['en']} moved · {frm} → {to('en')}"),
                              {"service_id": n["id"]})
        self.plan = np_

    def current_service_id(self) -> str | None:
        for v in self.plan or []:
            if v["start_s"] is not None and v["start_s"] <= self.t < v["end_s"]:
                return v["id"]
        return None

    # ------------------------------------------------------------ controls
    def control(self, speed: float | None = None, aid: bool | None = None, sat: bool | None = None) -> None:
        if speed is not None:
            self.speed = speed
        if aid is not None and aid != self.aid:
            if not aid:
                self.aid, self.loss_d = False, self.d
                self.emit("AID_LOST", tx("비행기 위치 신호 끊김 · 예측 위치로 계산 시작", "Aircraft position signal lost · estimating position"))
            else:
                self.aid, self.est_d = True, self.d
                self.emit("AID_RESTORED", tx("비행기 위치 신호 복구", "Aircraft position signal restored"))
        if sat is not None and sat != self.sat:
            self.sat = sat
            if not sat:
                self.emit("LINK_LOST", tx("지상 연결 끊김 · 태블릿이 혼자 판단 중", "Ground link lost · tablet deciding on its own"))
            else:
                self.emit("LINK_RESTORED", tx("지상 연결 복구", "Ground link restored"))
                self.uplink()
                self._last_plan_t = -1e9
        self.evaluate()
        self.update_plan()

    def jump_to_next(self) -> dict:
        """Advance to 30 minutes before the next forecast segment (demo control)."""
        found = None
        for s in range(0, 10 * 3600 + 1, 30):
            d = self.d + self.gs * s / 3600
            if d > self.route.length_nm:
                break
            la = look_ahead(self.route, self.hazards, d, self.t + s, self.gs)
            if la and la.cur:
                found = s
                break
        if found is None:
            return {"jumped": False, "reason": "NO_SEGMENT_AHEAD"}
        if found <= 1800:
            return {"jumped": False, "reason": "ALREADY_WITHIN_30_MIN"}
        adv = found - 1800
        self.d += self.gs * adv / 3600
        self.t += adv
        self.est_d = self.d if self.aid else self.est_d + self.gs_est * adv / 3600
        if self.sat:
            self.last_uplink = self.t
        self.after_until = -1
        self.evaluate()
        self.update_plan(force=True)
        return {"jumped": True, "advanced_s": adv}

    # ------------------------------------------------------------ tablet sync
    def apply_tablet_event(self, e: dict, device_id: str) -> None:
        self.tablet["device_id"] = device_id
        self.tablet["last_sync_at"] = now_utc().isoformat()
        if e["kind"] == "cabin":
            self.cabin_actions = {k: bool(e["payload"].get(k, False)) for k in NO_ACTIONS}
        elif e["kind"] == "point":
            self.tablet["point"] = e["payload"].get("point")

    # ------------------------------------------------------------ derived views
    def tta(self) -> dict | None:
        pos_sigma = position_sigma_nm(self.aid, self.est_d, self.loss_d)
        return tta_estimate(self.t_entry, self.cur > 0, self.gs_shown, pos_sigma, self.t - self.last_uplink)

    def resting(self) -> list[str]:
        h = self.t / 3600
        rg = self.ac["rest_groups"]
        return rg["A"] if 2.6 <= h < 4.6 else rg["B"] if 5.6 <= h < 7.0 else []

    def ground_ai(self) -> dict:
        tta = self.tta()
        cab = cstmod.cabin_state(self.t, self.current_service_id(), self.cur > 0, self.cabin_actions,
                                 [c["pos"] for c in self.ac["crew"]], self.resting())
        r = kernel_evaluate(cstmod.features(cab), tta["q_low_s"] if tta else None, self.s.safety_buffer_s)
        return {**r, "cabin": cab}

    def exposures_view(self) -> list[dict]:
        strength = library.strength_labels()
        return [{"hazard_id": e.hazard.id, "name": e.hazard.name, "t0_s": round(e.t0, 1), "t1_s": round(e.t1, 1),
                 "t0_utc": self.utc_at(e.t0), "d0_nm": e.d0, "d1_nm": e.d1, "max_ring": e.max, "strength": strength[e.max],
                 "points": [{**p, "utc": self.utc_at(p["t_s"])} for p in point_timeline(e.t0, self.leads)]} for e in self.ex]

    def plan_view(self) -> list[dict]:
        return [{**p, "mode_label": MODE_LABEL[p["mode"]],
                 "start_utc": self.utc_at(p["start_s"]) if p["start_s"] is not None else None} for p in self.plan or []]

    def hazards_view(self, t: float | None = None) -> list[dict]:
        tt = self.t if t is None else t
        return [{**h.spec(), "centre": h.centre(tt).as_dict(), "rings": [h.polygon(tt, k) for k in (1, 2, 3)]} for h in self.hazards]

    def snapshot(self) -> dict:
        p = self.route.pos_at(self.est_d)
        L = self.look
        stage = "STAY" if self.cur > 0 else "AFTER" if self.t < self.after_until else (f"P{self.g}" if self.g else "NORMAL")
        return {
            "flight_id": self.id, "status": self.flight["status"], "feed": self.flight["feed"], "running": self.running,
            "t_s": round(self.t, 1), "utc": self.utc_at(self.t), "eta_s": round(self.eta, 1), "eta_utc": self.utc_at(self.eta),
            "speed": self.speed, "effective_speed": self.effective_speed,
            "position": {**p.as_dict(), "d_nm": round(self.est_d, 1), "route_nm": round(self.route.length_nm, 1),
                         "progress": round(self.est_d / self.route.length_nm, 4), "fl": round(flight_level(self.route, self.est_d)),
                         "gs_kt": round(self.gs_shown, 1), "track_deg": round(self.route.track_at(self.est_d), 1),
                         "source": "AID" if self.aid else "ESTIMATE",
                         "sigma_nm": round(position_sigma_nm(self.aid, self.est_d, self.loss_d), 2)},
            "links": {"aid": self.aid, "sat": self.sat, "forecast_age_s": round(self.t - self.last_uplink, 1),
                      "forecast_version": self.forecast_version, "uplinked_version": self.uplinked_version},
            "look_ahead": None if not L else {"hazard_id": L.hazard.id, "name": L.hazard.name, "cur_ring": L.cur,
                                              "first_s": {str(k): v for k, v in L.first.items()}, "max_ring": L.max, "exit_s": L.exit},
            "tta": self.tta(),
            "point": {"stage": stage, "n": self.g, "t_entry_s": self.t_entry,
                      "next_point_in_s": self._next_point_in()},
            "next_exposure": next(iter(self.exposures_view()), None),
            "service": {"current": self.current_service_id(), "plan": self.plan_view()},
            "tablet": self.tablet,
            "cabin_actions": self.cabin_actions,
        }

    def _next_point_in(self) -> float | None:
        tE = self.t_entry
        if tE is None:
            return None
        for lead in self.leads:
            if tE > lead:
                return tE - lead
        return None

    # ------------------------------------------------------------ events
    def emit(self, kind: str, msg: dict, data: dict | None = None) -> None:
        entry = {"t_s": round(self.t, 1), "utc": self.utc_at(self.t), "kind": kind, "msg": msg, "data": data or {}}
        self.log.insert(0, entry)
        del self.log[100:]
        self.store.add_server_event(self.id, kind, self.t, msg, data)
        self.broadcast({"type": "event", "event": kind, "data": entry})

    def broadcast(self, msg: dict) -> None:
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)


class SessionManager:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.sessions: dict[str, FlightSession] = {}
        self._task: asyncio.Task | None = None

    # ---------------- forecast
    def forecast(self, route_id: str) -> dict:
        fc = self.store.latest_forecast(route_id)
        if fc is None:
            fc = self.store.add_forecast(route_id, library.default_hazards(route_id), "seed")
        return fc

    def publish_forecast(self, route_id: str, hazards: list[dict], source: str) -> dict:
        fc = self.store.add_forecast(route_id, hazards, source)
        self.store.add_server_event(None, "FORECAST_PUBLISHED", None, tx(f"{route_id} 예보 v{fc['version']} 발행", f"{route_id} forecast v{fc['version']} published"))
        for sess in self.sessions.values():
            if sess.route.id == route_id:
                sess.set_forecast(fc)
        return fc

    # ---------------- sessions
    def create(self, flight: dict) -> FlightSession:
        existing = self.store.get_flight(flight["id"])
        if existing and flight["id"] in self.sessions:
            return self.sessions[flight["id"]]
        self.store.upsert_flight(flight)
        sess = FlightSession(flight, self.s, self.store, self.forecast(flight["route_id"]))
        self.sessions[flight["id"]] = sess
        return sess

    def get(self, flight_id: str) -> FlightSession | None:
        if flight_id in self.sessions:
            return self.sessions[flight_id]
        row = self.store.get_flight(flight_id)
        if not row:
            return None
        row["date"] = dt.date.fromisoformat(row["date"])
        row["dep_utc"] = dt.datetime.fromisoformat(row["dep_utc"])
        row["created_at"] = dt.datetime.fromisoformat(row["created_at"])
        sess = FlightSession(row, self.s, self.store, self.forecast(row["route_id"]))
        self.sessions[flight_id] = sess
        return sess

    # ---------------- loop
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self.s.sim_tick_s)
            now = loop.time()
            dt_real, last = now - last, now
            for sess in list(self.sessions.values()):
                try:
                    sess.tick(dt_real)
                    if sess.subscribers and now - sess._last_push >= self.s.state_push_s:
                        sess._last_push = now
                        sess.broadcast({"type": "state", "data": sess.snapshot()})
                except Exception:  # noqa: BLE001 — one bad session must not stop the others
                    log.exception("tick failed for %s", sess.id)

    def start_loop(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def overview(self) -> list[dict]:
        out = []
        for sess in self.sessions.values():
            snap = sess.snapshot()
            out.append({"flight_id": sess.id, "flight_no": sess.flight["flight_no"], "status": snap["status"],
                        "utc": snap["utc"], "progress": snap["position"]["progress"], "point": snap["point"],
                        "tta": snap["tta"], "links": snap["links"], "tablet": snap["tablet"],
                        "audit": self.store.audit_summary(sess.id)})
        return out
