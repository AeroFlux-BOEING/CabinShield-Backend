"""Spherical geometry for great-circle routes. Distances in nautical miles, longitudes 0–360 °E.

Port of the crew app's `src/domain/geo.ts` so the server and the tablet agree to the metre.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

D2R = math.pi / 180
R2D = 180 / math.pi
EARTH_NM = 3440.065


def lon360(lon: float) -> float:
    return ((lon % 360) + 360) % 360


@dataclass(frozen=True)
class LatLon:
    lat: float
    lon: float  # 0–360 °E

    def as_dict(self) -> dict:
        return {"lat": round(self.lat, 5), "lon": round(self.lon, 5)}


def _vec(p: LatLon) -> tuple[float, float, float]:
    a, b = p.lat * D2R, p.lon * D2R
    return (math.cos(a) * math.cos(b), math.cos(a) * math.sin(b), math.sin(a))


def bearing(p: LatLon, q: LatLon) -> float:
    f1, f2, dl = p.lat * D2R, q.lat * D2R, (q.lon - p.lon) * D2R
    y = math.sin(dl) * math.cos(f2)
    x = math.cos(f1) * math.sin(f2) - math.sin(f1) * math.cos(f2) * math.cos(dl)
    return lon360(math.atan2(y, x) * R2D)


def destination(p: LatLon, brg: float, nm: float) -> LatLon:
    """Point reached from p on bearing brg (deg) after nm nautical miles (nm may be negative)."""
    d, f1, l1, t = nm / EARTH_NM, p.lat * D2R, p.lon * D2R, brg * D2R
    f2 = math.asin(math.sin(f1) * math.cos(d) + math.cos(f1) * math.sin(d) * math.cos(t))
    l2 = l1 + math.atan2(math.sin(t) * math.sin(d) * math.cos(f1), math.cos(d) - math.sin(f1) * math.sin(f2))
    return LatLon(f2 * R2D, lon360(l2 * R2D))


def distance_nm(a: LatLon, b: LatLon) -> float:
    f1, f2 = a.lat * D2R, b.lat * D2R
    df, dl = f2 - f1, (b.lon - a.lon) * D2R
    x = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_NM * math.asin(min(1.0, math.sqrt(x)))


class GreatCircleRoute:
    """A great-circle leg between two airports, parameterised by distance flown (nm)."""

    def __init__(self, route_id: str, a: LatLon, b: LatLon):
        self.id = route_id
        self.a, self.b = a, b
        self._va, self._vb = _vec(a), _vec(b)
        dot = sum(x * y for x, y in zip(self._va, self._vb))
        self._omega = math.acos(max(-1.0, min(1.0, dot)))
        self.length_nm = self._omega * EARTH_NM

    def gc(self, f: float) -> LatLon:
        ff = max(0.0, min(1.0, f))
        s = math.sin(self._omega)
        ka, kb = math.sin((1 - ff) * self._omega) / s, math.sin(ff * self._omega) / s
        x, y, z = (ka * p + kb * q for p, q in zip(self._va, self._vb))
        return LatLon(math.atan2(z, math.hypot(x, y)) * R2D, lon360(math.atan2(y, x) * R2D))

    def pos_at(self, d: float) -> LatLon:
        return self.gc(d / self.length_nm)

    def track_at(self, d: float) -> float:
        L = self.length_nm
        return bearing(self.pos_at(max(0.0, min(L - 2, d) - 1)), self.pos_at(min(L, max(2.0, d) + 1)))

    def project(self, p: LatLon, step_nm: float = 10.0) -> tuple[float, float]:
        """Closest along-track distance to p and the cross-track offset (nm). Coarse scan + refine."""
        best_d, best_x = 0.0, float("inf")
        d = 0.0
        while d <= self.length_nm:
            x = distance_nm(self.pos_at(d), p)
            if x < best_x:
                best_d, best_x = d, x
            d += step_nm
        lo, hi = max(0.0, best_d - step_nm), min(self.length_nm, best_d + step_nm)
        for _ in range(30):  # golden-section refine
            m1, m2 = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
            if distance_nm(self.pos_at(m1), p) < distance_nm(self.pos_at(m2), p):
                hi = m2
            else:
                lo = m1
        d = (lo + hi) / 2
        return d, distance_nm(self.pos_at(d), p)

    def polyline(self, step_nm: float = 50.0) -> list[dict]:
        n = max(2, int(math.ceil(self.length_nm / step_nm)) + 1)
        return [{"d_nm": round(self.length_nm * i / (n - 1), 1), **self.gc(i / (n - 1)).as_dict()} for i in range(n)]


AIRPORT_COORDS: dict[str, LatLon] = {
    "ICN": LatLon(37.4602, 126.4407),
    "SFO": LatLon(37.619, lon360(-122.3749)),
    "LAX": LatLon(33.9416, lon360(-118.4085)),
    "JFK": LatLon(40.6413, lon360(-73.7781)),
}

_ROUTES: dict[str, GreatCircleRoute] = {}


def get_route(route_id: str) -> GreatCircleRoute:
    if route_id not in _ROUTES:
        a, b = route_id.split("-")
        _ROUTES[route_id] = GreatCircleRoute(route_id, AIRPORT_COORDS[a], AIRPORT_COORDS[b])
    return _ROUTES[route_id]
