"""Keep only what matters for a route, and turn it into app hazards.

* PIREP/AIREP → projected on the route (along-track d_nm, cross-track xtk_nm), kept inside the corridor.
* SIGMET polygons → kept when the route corridor crosses them at cruise levels → exact-outline hazard
  with `floor` = SIGMET intensity (SEV → 3) and its validity window and motion.
* Clusters of recent MOD+ reports along the route → short-lived hazard (source PIREP).
"""
from __future__ import annotations

import datetime as dt
import math

from app.engine.geo import D2R, GreatCircleRoute, LatLon, distance_nm
from app.wx.normalize import RING_LABEL

CRUISE_FL = (280, 430)


def route_bboxes(route: GreatCircleRoute, pad_deg: float = 3.0) -> list[tuple[float, float, float, float]]:
    """Corridor bounding boxes (lat0, lon0, lat1, lon1) in −180…180, split at the antimeridian."""
    pts = [route.pos_at(route.length_nm * i / 60) for i in range(61)]
    lats = [p.lat for p in pts]
    lo, hi = min(lats) - pad_deg, max(lats) + pad_deg
    west = [p.lon for p in pts if p.lon <= 180]
    east = [p.lon - 360 for p in pts if p.lon > 180]
    out = []
    if west:
        out.append((round(lo, 1), round(min(west) - pad_deg, 1), round(hi, 1), 180.0))
    if east:
        out.append((round(lo, 1), -180.0, round(hi, 1), round(max(east) + pad_deg, 1)))
    return out


def _local_xy(c: LatLon, p: LatLon) -> tuple[float, float]:
    dl = p.lon - c.lon
    if dl > 180:
        dl -= 360
    if dl < -180:
        dl += 360
    return dl * 60 * math.cos(c.lat * D2R), (p.lat - c.lat) * 60


def project_observations(route: GreatCircleRoute, obs: list[dict], corridor_nm: float, min_fl: float = 200) -> list[dict]:
    out = []
    for o in obs:
        if o["fl"] is not None and o["fl"] < min_fl:
            continue
        d, xtk = route.project(LatLon(o["lat"], o["lon"]), step_nm=20)
        if xtk <= corridor_nm and 0 < d < route.length_nm:
            out.append({**o, "d_nm": round(d, 1), "xtk_nm": round(xtk, 1)})
    return sorted(out, key=lambda x: x["d_nm"])


def _centroid(coords: list[dict]) -> LatLon:
    lat = sum(c["lat"] for c in coords) / len(coords)
    ref = coords[0]["lon"]
    lons = [c["lon"] + (360 if c["lon"] - ref < -180 else -360 if c["lon"] - ref > 180 else 0) for c in coords]
    return LatLon(lat, (sum(lons) / len(lons)) % 360)


def _pip(v, x, y) -> bool:
    r, j = False, len(v) - 1
    for i in range(len(v)):
        xi, yi = v[i]
        xj, yj = v[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            r = not r
        j = i
    return r


def sigmet_crosses_route(route: GreatCircleRoute, area: dict, corridor_nm: float) -> tuple[bool, float | None]:
    """True when the corridor crosses the area at cruise levels; returns the first along-track distance."""
    if area["fl"][1] < CRUISE_FL[0] or area["fl"][0] > CRUISE_FL[1]:
        return False, None
    c = _centroid(area["coords"])
    v = [_local_xy(c, LatLon(p["lat"], p["lon"])) for p in area["coords"]]
    d = 0.0
    while d <= route.length_nm:
        p = route.pos_at(d)
        x, y = _local_xy(c, p)
        if _pip(v, x, y) or min(distance_nm(p, LatLon(q["lat"], q["lon"])) for q in area["coords"]) <= corridor_nm / 2:
            return True, d
        d += 20
    return False, None


def sigmet_to_hazard(area: dict, d_nm: float) -> dict:
    c = _centroid(area["coords"])
    verts = [list(map(lambda z: round(z, 2), _local_xy(c, LatLon(p["lat"], p["lon"])))) for p in area["coords"]]
    if verts[0] == verts[-1]:
        verts = verts[:-1]
    r = max(math.hypot(x, y) for x, y in verts)
    q = RING_LABEL[area["ring"]]
    return {
        "id": f"SIG-{area['fir']}-{(area['series'] or '').replace(' ', '')}",
        "name": {"ko": f"{area['fir_name']} SIGMET {area['series']} · {q['ko']} 난기류",
                 "en": f"{area['fir_name']} SIGMET {area['series']} · {q['en']} turbulence"},
        "fl": area["fl"], "r_nm": round(r, 1), "source": "SIGMET",
        "centre_ll": c.as_dict(), "t_obs_utc": area["obs_utc"], "vertices_nm": verts, "floor": area["ring"],
        "drift_kt": area["move_kt"] or 0.0, "drift_dir_deg": area["move_dir_deg"] if area["move_dir_deg"] is not None else 90.0,
        "valid_from_utc": area["valid_from_utc"], "valid_to_utc": area["valid_to_utc"],
        "meta": {"obs_id": area["id"], "d_nm": round(d_nm, 1), "raw": area["raw"]},
    }


def cluster_reports(obs: list[dict], gap_nm: float = 80, min_ring: int = 2) -> list[list[dict]]:
    """Group route-projected MOD+ reports whose along-track gap is at most gap_nm."""
    strong = [o for o in obs if o["ring"] >= min_ring]
    groups: list[list[dict]] = []
    for o in strong:
        if groups and o["d_nm"] - groups[-1][-1]["d_nm"] <= gap_nm:
            groups[-1].append(o)
        else:
            groups.append([o])
    return groups


def report_cluster_to_hazard(group: list[dict], route: GreatCircleRoute, hold_h: float = 3.0) -> dict | None:
    """≥2 MOD+ reports, or one SEV, → circular hazard around them valid for hold_h after the latest report."""
    ring = max(o["ring"] for o in group)
    if len(group) < 2 and ring < 3:
        return None
    lat = sum(o["lat"] for o in group) / len(group)
    c = _centroid([{"lat": o["lat"], "lon": o["lon"]} for o in group])
    c = LatLon(lat, c.lon)
    spread = max(distance_nm(c, LatLon(o["lat"], o["lon"])) for o in group)
    r = max(60.0, spread + 40)
    fls = [o["fl"] for o in group if o["fl"]]
    latest = max(dt.datetime.fromisoformat(o["obs_utc"]) for o in group)
    earliest = min(dt.datetime.fromisoformat(o["obs_utc"]) for o in group)
    q = RING_LABEL[ring]
    d0 = group[0]["d_nm"]
    return {
        "id": f"REP-{int(d0)}", "source": "PIREP",
        "name": {"ko": f"조종사 보고 {len(group)}건 · {q['ko']} 난기류", "en": f"{len(group)} pilot reports · {q['en']} turbulence"},
        "fl": [max(0, min(fls) - 40), max(fls) + 40] if fls else [280, 410], "r_nm": round(r, 1),
        "centre_ll": c.as_dict(), "t_obs_utc": latest.isoformat(),
        "vertices_nm": [[round(r * math.sin(k * 45 * D2R), 2), round(r * math.cos(k * 45 * D2R), 2)] for k in range(8)],
        "floor": ring, "drift_kt": 0.0,
        "valid_from_utc": earliest.isoformat(), "valid_to_utc": (latest + dt.timedelta(hours=hold_h)).isoformat(),
        "meta": {"obs_ids": [o["id"] for o in group], "d_nm": [group[0]["d_nm"], group[-1]["d_nm"]]},
    }
