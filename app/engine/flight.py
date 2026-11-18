"""Flight physics and forecast turbulence areas along a route.

Port of the crew app's `src/domain/flight.ts`. Hazards are data (`Hazard`) rather than module
globals so the ground side can publish new forecast versions. All values are illustrative.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from app.engine.geo import D2R, GreatCircleRoute, LatLon, destination

PLAN_GS = 492.0   # mean planning ground speed (kt) used for ETAs and far-ahead timing
TAS = 488.0       # cruise true airspeed (kt)
START_NM = 350.0  # where the demo starts along the route (nm)
RING = (1.0, 0.66, 0.33)
LOOKAHEAD_S = 3600
LOOK_STEP_S = 5


def time_for_distance(d: float) -> float:
    return d / 470 * 3600


def ground_speed(route: GreatCircleRoute, d: float, t: float) -> float:
    """Ground speed with climb/descent ramps and a slowly varying jet-stream component."""
    ramp = max(0.0, min(1.0, d / 140, (route.length_nm - d) / 130))
    cruise = 492 + 28 * math.sin(t / 2600) + 12 * math.sin(t / 730 + 1)
    return 250 + (cruise - 250) * ramp


def flight_level(route: GreatCircleRoute, d: float) -> float:
    """Flight level (hundreds of ft) at distance d, with step climbs."""
    L = route.length_nm
    plan = 350 if d < 0.45 * L else 370 if d < 0.72 * L else 390
    return max(0.0, min(plan, 15 + 335 * d / 160, 15 + plan * (L - d) / 130))


@dataclass
class Hazard:
    """A forecast turbulence area with three intensity rings (outer 100 % / 66 % / 33 % of the outline).

    Demo hazards are 8-radius shapes anchored to the route (f, off_nm). Live hazards from SIGMETs or
    PIREP/AIREP clusters carry an absolute centre + observation time, an exact outline (vertices_nm),
    a validity window and `floor` — the minimum intensity anywhere inside the outline
    (e.g. a SEV SIGMET → 3). Without the optional fields the behaviour is identical to the app.
    """

    id: str
    idx: int
    name: dict            # {"ko": ..., "en": ...}
    fl: tuple[float, float]
    f: float = 0.0        # position along the route (0–1) of the reference point
    off_nm: float = 0.0   # cross-track offset of the centre (nm, + = right of track)
    r_nm: float = 100.0
    drift_kt: float = 0.0
    shape: list[float] = field(default_factory=lambda: [1.0] * 8)
    source: str = "seed"
    centre_ll: dict | None = None          # {"lat", "lon"} absolute centre at t_obs_utc
    t_obs_utc: str | None = None
    vertices_nm: list | None = None        # exact outline [[x_east, y_north], ...] in nm from the centre
    floor: int = 0
    drift_dir_deg: float = 90.0
    valid_from_utc: str | None = None
    valid_to_utc: str | None = None
    meta: dict = field(default_factory=dict)
    c0: LatLon = field(init=False)
    t_ref: float = field(init=False)
    t_from: float | None = field(init=False, default=None)
    t_to: float | None = field(init=False, default=None)
    v: list[tuple[float, float]] = field(init=False)

    def bind(self, route: GreatCircleRoute, dep_utc: dt.datetime | None = None, time_shift_s: float = 0.0) -> "Hazard":
        """time_shift_s re-times live hazards (replay mode): an observation made `now` lands at the sim's current time."""
        def rel(iso: str | None) -> float | None:
            if iso is None or dep_utc is None:
                return None
            return (dt.datetime.fromisoformat(iso) - dep_utc).total_seconds() + time_shift_s

        if self.centre_ll is not None:
            self.c0 = LatLon(self.centre_ll["lat"], self.centre_ll["lon"] % 360)
            t = rel(self.t_obs_utc)
            self.t_ref = t if t is not None else 0.0
        else:
            dr = self.f * route.length_nm
            self.c0 = destination(route.pos_at(dr), route.track_at(dr) - 90, self.off_nm)
            self.t_ref = time_for_distance(dr)
        self.t_from, self.t_to = rel(self.valid_from_utc), rel(self.valid_to_utc)
        if self.vertices_nm:
            self.v = [(float(x), float(y)) for x, y in self.vertices_nm]
        else:
            self.v = [(self.r_nm * s * math.sin(k * 45 * D2R), self.r_nm * s * math.cos(k * 45 * D2R)) for k, s in enumerate(self.shape)]
        return self

    def active(self, t: float) -> bool:
        return (self.t_from is None or t >= self.t_from) and (self.t_to is None or t <= self.t_to)

    def centre(self, t: float) -> LatLon:
        return destination(self.c0, self.drift_dir_deg, self.drift_kt * (t - self.t_ref) / 3600)

    def polygon(self, t: float, ring: int = 1) -> list[dict]:
        """Ring outline (lat/lon) at sim time t, for maps."""
        c, k = self.centre(t), RING[ring - 1]
        out = []
        for x, y in self.v:
            lat = c.lat + y * k / 60
            lon = c.lon + x * k / (60 * math.cos(c.lat * D2R))
            out.append({"lat": round(lat, 4), "lon": round(lon % 360, 4)})
        return out

    def ring_of(self, c: LatLon, p: LatLon) -> int:
        dl = p.lon - c.lon
        if dl > 180:
            dl -= 360
        if dl < -180:
            dl += 360
        x, y = dl * 60 * math.cos(c.lat * D2R), (p.lat - c.lat) * 60
        for k in (3, 2, 1):
            if _point_in_poly(self.v, x / RING[k - 1], y / RING[k - 1]):
                return max(k, self.floor)
        return 0

    def spec(self) -> dict:
        d = {"id": self.id, "name": self.name, "fl": list(self.fl), "f": self.f, "off_nm": self.off_nm,
             "r_nm": self.r_nm, "drift_kt": self.drift_kt, "shape": self.shape, "source": self.source}
        for k in ("centre_ll", "t_obs_utc", "vertices_nm", "valid_from_utc", "valid_to_utc"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.floor:
            d["floor"] = self.floor
        if self.drift_dir_deg != 90.0:
            d["drift_dir_deg"] = self.drift_dir_deg
        if self.meta:
            d["meta"] = self.meta
        return d


def _point_in_poly(v: list[tuple[float, float]], x: float, y: float) -> bool:
    r = False
    j = len(v) - 1
    for i in range(len(v)):
        xi, yi = v[i]
        xj, yj = v[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            r = not r
        j = i
    return r


HAZARD_FIELDS = ("f", "off_nm", "r_nm", "drift_kt", "shape", "source", "centre_ll", "t_obs_utc", "vertices_nm", "floor",
                 "drift_dir_deg", "valid_from_utc", "valid_to_utc", "meta")


LIVE_SOURCES = ("SIGMET", "PIREP", "SIGWX")


def make_hazards(route: GreatCircleRoute, specs: list[dict], dep_utc: dt.datetime | None = None,
                 live_shift_s: float = 0.0) -> list[Hazard]:
    out = []
    for i, s in enumerate(specs):
        kw = {k: s[k] for k in HAZARD_FIELDS if s.get(k) is not None}
        h = Hazard(id=s["id"], idx=i, name=s["name"], fl=tuple(s["fl"]), **kw)
        out.append(h.bind(route, dep_utc, live_shift_s if h.source in LIVE_SOURCES else 0.0))
    return out


def _level_at(route, hazards, d0, t0, gs, s):
    d = d0 + gs * s / 3600
    if d > route.length_nm:
        return None
    p, fl = route.pos_at(d), flight_level(route, d)
    best_h, best_k = None, 0
    for h in hazards:
        if fl < h.fl[0] or fl > h.fl[1] or not h.active(t0 + s):
            continue
        k = h.ring_of(h.centre(t0 + s), p)
        if k > best_k:
            best_h, best_k = h, k
    return best_h, best_k


@dataclass
class LookAhead:
    cur: int                    # ring the aircraft is in now (0 = outside)
    first: dict[int, float]     # seconds until first entering ring k (1–3)
    max: int
    exit: float                 # seconds until leaving the area
    hazard: Hazard


def look_ahead(route, hazards, d0: float, t0: float, gs: float) -> LookAhead | None:
    """Look one hour ahead along the planned path for the next forecast area."""
    z = _level_at(route, hazards, d0, t0, gs, 0)
    cur = z[1] if z else 0
    h0 = z[0] if (cur and z) else None
    first: dict[int, float] = {}
    mx, ex = cur, None
    if cur:
        for k in range(1, cur + 1):
            first[k] = 0
    for s in range(0, LOOKAHEAD_S + 1, LOOK_STEP_S):
        r = _level_at(route, hazards, d0, t0, gs, s)
        if r is None:
            break
        h, k = r
        if k > 0 and (h0 is None or h is h0):
            h0 = h
            for kk in range(1, k + 1):
                first.setdefault(kk, s)
            mx = max(mx, k)
        elif h0 is not None and h is not h0:
            ex = s
            break
    return LookAhead(cur, first, mx, ex if ex is not None else LOOKAHEAD_S, h0) if h0 else None


@dataclass
class Exposure:
    hazard: Hazard
    t0: float
    t1: float
    d0: float
    d1: float
    max: int


def exposures(route, hazards, est_d: float, t: float, step_nm: float = 8) -> list[Exposure]:
    """Every forecast segment along the whole route, timed with the planning ground speed."""
    out: list[Exposure] = []
    w: Exposure | None = None
    d = 0.0
    while d <= route.length_nm:
        tt = t + (d - est_d) / PLAN_GS * 3600
        p, fl = route.pos_at(d), flight_level(route, d)
        k, hh = 0, None
        for h in hazards:
            if fl < h.fl[0] or fl > h.fl[1] or not h.active(tt):
                continue
            r = h.ring_of(h.centre(tt), p)
            if r > k:
                k, hh = r, h
        if k and hh:
            if w is None or w.hazard is not hh:
                w = Exposure(hh, tt, tt, d, d, k)
                out.append(w)
            else:
                w.t1, w.d1, w.max = tt, d, max(w.max, k)
        else:
            w = None
        d += step_nm
    return out
