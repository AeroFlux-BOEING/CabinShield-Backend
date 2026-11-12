"""AWC JSON → normalised turbulence observations and areas."""
from __future__ import annotations

import datetime as dt
import hashlib

# turbulence intensity → app ring (0 none, 1 light, 2 moderate, 3 severe)
INTENSITY_RING = {"NEG": 0, "SMTH": 0, "NONE": 0, "SMTH-LGT": 1, "LGT": 1, "LGT-MOD": 1, "MOD": 2, "MOD-SEV": 2,
                  "SEV": 3, "SEV-EXTM": 3, "EXTM": 3, "EXTRM": 3}
QUALIFIER_RING = {"MOD": 2, "SEV": 3, "EXTM": 3, "EXTRM": 3}
RING_LABEL = [{"ko": "없음", "en": "none"}, {"ko": "약함", "en": "light"}, {"ko": "보통", "en": "moderate"}, {"ko": "심함", "en": "severe"}]


def _utc(ts) -> str:
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
    return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).isoformat()


def ring_of_intensity(s: str | None) -> int | None:
    if not s:
        return None
    s = s.upper().strip()
    if s in INTENSITY_RING:
        return INTENSITY_RING[s]
    for k in ("SEV", "MOD", "LGT"):  # tolerate variants like "MODclr"
        if k in s:
            return INTENSITY_RING[k]
    return 0 if "NEG" in s else None


def pirep(p: dict) -> dict | None:
    """One PIREP/AIREP → observation, or None when it says nothing about turbulence."""
    rings = [r for r in (ring_of_intensity(p.get("tbInt1")), ring_of_intensity(p.get("tbInt2"))) if r is not None]
    if not rings or p.get("lat") is None or p.get("lon") is None:
        return None
    raw = p.get("rawOb") or ""
    kind = "UUA" if str(p.get("pirepType", "")).startswith("Urgent") else ("AIREP" if p.get("pirepType") == "AIREP" else "PIREP")
    ring = max(rings)
    intensity = p.get("tbInt1") if ring_of_intensity(p.get("tbInt1")) == ring else p.get("tbInt2")
    return {
        "id": hashlib.sha1(f"{raw}|{p.get('obsTime')}".encode()).hexdigest()[:16],
        "kind": kind, "obs_utc": _utc(p["obsTime"]), "lat": float(p["lat"]), "lon": float(p["lon"]) % 360,
        "fl": float(p["fltLvl"]) if p.get("fltLvl") is not None else None,
        "ring": ring, "intensity": intensity, "type": p.get("tbType1") or p.get("tbType2") or None,
        "freq": p.get("tbFreq1") or None, "aircraft": p.get("acType"), "raw": raw.strip(),
    }


def sigmet(s: dict, domestic: bool = False) -> dict | None:
    """International or domestic SIGMET (turbulence) → area."""
    coords = s.get("coords") or []
    if len(coords) < 3:
        return None
    q = (s.get("qualifier") or s.get("severity") or "SEV").upper()
    ring = QUALIFIER_RING.get(q, 3)
    base, top = s.get("base"), s.get("top")
    if domestic:  # airsigmet uses altitudeLow1/altitudeHi1 in feet
        base, top = s.get("altitudeLow1", base), s.get("altitudeHi1", top)
    raw = s.get("rawSigmet") or s.get("rawAirSigmet") or ""
    try:
        spd = float(s.get("spd") or 0)
    except ValueError:
        spd = 0.0
    dirs = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5, "S": 180,
            "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}
    return {
        "id": hashlib.sha1(raw.encode()).hexdigest()[:16], "kind": "SIGMET",
        "fir": s.get("firId") or s.get("icaoId"), "fir_name": s.get("firName") or s.get("icaoId"),
        "series": s.get("seriesId"), "ring": ring, "qualifier": q,
        "fl": [float(base or 0) / 100, float(top or 45000) / 100],
        "valid_from_utc": _utc(s["validTimeFrom"]), "valid_to_utc": _utc(s["validTimeTo"]),
        "obs_utc": _utc(s.get("receiptTime") or s["validTimeFrom"]),
        "coords": [{"lat": float(c["lat"]), "lon": float(c["lon"]) % 360} for c in coords],
        "move_dir_deg": dirs.get(str(s.get("dir") or "").upper()), "move_kt": spd,  # AWC already reports knots
        "raw": raw.strip(),
    }
