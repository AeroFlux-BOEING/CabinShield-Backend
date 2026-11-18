"""WAFS SIGWX (IWXXM 2023-1+) → turbulence areas in the same shape as normalize.sigmet().

WAFS SIGWX covers FL100–FL600 with MOD / SEV turbulence areas (clear-air + mountain-wave) for
T+6…T+48 at 3-hour steps. SIGMETs only exist for turbulence that is happening or imminent, so
SIGWX fills the gaps (CAT without a SIGMET) with forecast areas.

Rules
- only `MeteorologicalFeature` whose phenomenon is …/TURBULENCE
- outline: posList of the ElevatedVolume exterior (Lat Long order); CubicSpline vertices are used as a polygon
- levels: uom M → ft, FL → ×100; nil → FL100 / FL600 (MSL vs pressure altitude ignored)
- intensity: BUFR code table 0 11 030 → ring 2 (MOD, MOD-SEV) / 3 (SEV, EXTM); NIL / LGT dropped
- validity: valid time ± 1.5 h; with several time steps the boundaries are cut half-way (no overlap)
- no motion vector (each time step is a snapshot)
Namespace-version agnostic (2023-1, 2025-2, …).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import xml.etree.ElementTree as ET

HALF_STEP = dt.timedelta(minutes=90)
DEFAULT_FL = (100.0, 600.0)
M_TO_FT = 3.28084

# BUFR 0 11 030 "Extended degree of turbulence" → (label, ring, context)
TURB_CODE: dict[int, tuple[str, int, str]] = {
    0: ("NIL", 0, "CLOUD"), 1: ("LGT", 1, "CLOUD"), 2: ("MOD", 2, "CLOUD"), 3: ("SEV", 3, "CLOUD"),
    4: ("NIL", 0, "CAT"), 5: ("LGT", 1, "CAT"), 6: ("MOD", 2, "CAT"), 7: ("SEV", 3, "CAT"),
    8: ("NIL", 0, ""), 9: ("LGT", 1, ""), 10: ("MOD", 2, ""), 11: ("SEV", 3, ""),
    12: ("EXTM", 3, "CAT"), 13: ("EXTM", 3, "CLOUD"), 14: ("EXTM", 3, ""),
    15: ("LGT-MOD", 1, ""), 16: ("LGT-MOD", 1, ""), 17: ("LGT-MOD", 1, ""),
    18: ("MOD-SEV", 2, ""), 19: ("MOD-SEV", 2, ""), 20: ("MOD-SEV", 2, ""),
    21: ("SEV", 3, ""), 22: ("SEV", 3, ""), 23: ("SEV", 3, ""),
    63: ("MOD", 2, ""),  # missing → SIGWX only draws MOD and above
}
CENTRE = {"LONDON": "EGRR", "WASHINGTON": "KKCI"}


def _ln(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _child(el, name):
    return next((c for c in el if _ln(c) == name), None)


def _desc(el, name) -> list:
    return [d for d in el.iter() if _ln(d) == name]


def _href(el) -> str:
    if el is None:
        return ""
    return next((v for k, v in el.attrib.items() if k.endswith("href")), "")


def _time(el) -> dt.datetime | None:
    if el is None:
        return None
    for name in ("timePosition", "beginPosition"):
        hit = _desc(el, name)
        if hit and hit[0].text:
            return dt.datetime.fromisoformat(hit[0].text.strip().replace("Z", "+00:00"))
    return None


def _fl(el, default: float) -> float:
    if el is None or el.text is None or not el.text.strip():
        return default
    v, uom = float(el.text), (el.get("uom") or "").upper()
    if uom == "FL":
        return v
    if uom in ("M", "MTR"):
        return v * M_TO_FT / 100
    return v / 100  # [ft_i] / FT


def _outline(vol) -> list[dict]:
    ext = _desc(vol, "exterior")
    scope = ext[0] if ext else vol
    pts: list[tuple[float, float]] = []
    for pl in _desc(scope, "posList"):
        v = [float(x) for x in (pl.text or "").split()]
        pts += list(zip(v[0::2], v[1::2]))
    if not pts:
        for p in _desc(scope, "pos"):
            a, b = (float(x) for x in p.text.split()[:2])
            pts.append((a, b))
    return [{"lat": la, "lon": lo % 360} for la, lo in pts]


def parse(xml: str | bytes, min_ring: int = 2) -> list[dict]:
    """One IWXXM document (or a collect of several) → turbulence areas."""
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    forecasts = [root] if _ln(root) == "WAFSSignificantWeatherForecast" else _desc(root, "WAFSSignificantWeatherForecast")
    out = []
    for fc in forecasts:
        valid = _time(_child(fc, "phenomenonTime"))
        if valid is None:
            continue
        base = _time(_child(fc, "phenomenonBaseTime"))
        issued = _time(_child(fc, "issueTime")) or base or valid
        lead_h = round((valid - base).total_seconds() / 3600, 1) if base else None
        wafc = _desc(fc, "WorldAreaForecastCentre")
        centre = CENTRE.get((wafc[0].text or "").strip().upper(), "WAFC") if wafc else "WAFC"
        vt = valid.strftime("%Y%m%dT%HZ")
        for feat in (c for c in fc if _ln(c) == "feature"):
            mf = _child(feat, "MeteorologicalFeature")
            if mf is None or not _href(_child(mf, "phenomenon")).upper().endswith("TURBULENCE"):
                continue
            geom = _child(mf, "phenomenonGeometry")
            vol = geom[0] if geom is not None and len(geom) else None
            if vol is None:
                continue
            coords = _outline(vol)
            if len(coords) < 3:
                continue
            deg = _desc(mf, "DegreeOfTurbulence")
            code_s = _href(deg[0]).rsplit("/", 1)[-1] if deg else "63"
            label, ring, ctx = TURB_CODE.get(int(code_s) if code_s.isdigit() else 63, ("MOD", 2, ""))
            if ring < min_ring:
                continue
            ident = _child(mf, "identifier")
            oid = (ident.text or "").strip() if ident is not None else mf.get("{http://www.opengis.net/gml/3.2}id", "")
            fl = [round(_fl(_child(vol, "lowerElevation"), DEFAULT_FL[0])), round(_fl(_child(vol, "upperElevation"), DEFAULT_FL[1]))]
            raw = (f"WAFS SIGWX {centre} VT {vt}" + (f" T+{lead_h:g}h" if lead_h is not None else "")
                   + f" {label}{' ' + ctx if ctx else ''} TURB FL{fl[0]:03d}-FL{fl[1]:03d}")
            out.append({
                "id": hashlib.sha1(f"{centre}|{vt}|{oid}".encode()).hexdigest()[:16], "kind": "SIGWX",
                "fir": centre, "fir_name": f"WAFS {centre}", "series": f"{vt} {oid[:8]}".strip(),
                "ring": ring, "qualifier": label, "context": ctx, "fl": [float(fl[0]), float(fl[1])],
                "valid_utc": valid.isoformat(), "lead_h": lead_h,
                "valid_from_utc": (valid - HALF_STEP).isoformat(), "valid_to_utc": (valid + HALF_STEP).isoformat(),
                "obs_utc": issued.isoformat(), "coords": coords, "move_dir_deg": None, "move_kt": 0.0, "raw": raw,
            })
    return out


def parse_many(docs: list[str | bytes], min_ring: int = 2) -> list[dict]:
    """Several time steps → areas whose validity windows meet half-way between steps."""
    areas = [a for d in docs for a in parse(d, min_ring)]
    steps = sorted({a["valid_utc"] for a in areas})
    for a in areas:
        i = steps.index(a["valid_utc"])
        vt = dt.datetime.fromisoformat(a["valid_utc"])
        if i > 0:
            prev = dt.datetime.fromisoformat(steps[i - 1])
            a["valid_from_utc"] = max(dt.datetime.fromisoformat(a["valid_from_utc"]), prev + (vt - prev) / 2).isoformat()
        if i < len(steps) - 1:
            nxt = dt.datetime.fromisoformat(steps[i + 1])
            a["valid_to_utc"] = min(dt.datetime.fromisoformat(a["valid_to_utc"]), vt + (nxt - vt) / 2).isoformat()
    return areas
