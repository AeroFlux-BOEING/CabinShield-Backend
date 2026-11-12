"""On-device CST model, server copy.

The crew tablet runs an MLP (38 → 32 → 16 → 3, ReLU) that predicts the p90 cabin securing time
(CST) for three plans. The ground side serves the weights (model distribution) and runs the
identical forward pass to double-check decisions uploaded from the tablet.
Also builds the 38 cabin features from a B787-9 cabin snapshot, like `src/ai/cabinState.ts`.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "models"
N_FEATURES = 38


@dataclass
class CstModel:
    version: str
    arch: list[int]
    scale: float
    margin: np.ndarray
    plans: list[dict]
    W: list[np.ndarray]
    b: list[np.ndarray]
    test_x: np.ndarray
    test_y: np.ndarray
    sha256: str
    raw: dict

    @property
    def params(self) -> int:
        return int(sum(w.size for w in self.W) + sum(x.size for x in self.b))

    def predict(self, x) -> np.ndarray:
        """Raw CST p90 (s) for FULL / MINIMAL / IMMEDIATE, before the calibration margin. x: (38,) or (n, 38)."""
        h = np.atleast_2d(np.asarray(x, dtype=np.float64))
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            h = h @ W.T + b
            if i < len(self.W) - 1:
                h = np.maximum(h, 0.0)
        out = h * self.scale
        return out[0] if np.ndim(x) == 1 else out

    def self_check(self) -> float:
        return float(np.max(np.abs(self.predict(self.test_x) - self.test_y)))

    def manifest(self) -> dict:
        return {"version": self.version, "arch": self.arch, "activation": "relu", "params": self.params,
                "size_kb": round(len(json.dumps({"W": self.raw["W"], "b": self.raw["b"]})) / 1024, 1),
                "margin_s": [round(float(m), 3) for m in self.margin], "plans": self.plans, "sha256": self.sha256,
                "self_check_max_err_s": round(self.self_check(), 4)}


def _load(path: Path) -> CstModel:
    blob = path.read_bytes()
    raw = json.loads(blob)
    return CstModel(version=raw["version"], arch=raw["arch"], scale=float(raw["scale"]), margin=np.array(raw["margin"]),
                    plans=raw["plans"], W=[np.array(w) for w in raw["W"]], b=[np.array(b) for b in raw["b"]],
                    test_x=np.array(raw["test"]["x"]), test_y=np.array(raw["test"]["y"]),
                    sha256=hashlib.sha256(blob).hexdigest(), raw=raw)


@lru_cache
def available_models() -> dict[str, CstModel]:
    out = {}
    for p in sorted(MODEL_DIR.glob("cst_mlp_*.json")):
        m = _load(p)
        out[m.version] = m
    return out


def get_model(version: str | None = None) -> CstModel:
    models = available_models()
    if version is None:
        return models[sorted(models)[-1]]
    return models[version]


# ---------------------------------------------------------------- cabin state → 38 features

CREW_ZONE = [0, 0, 0, 1, 1, 1, 2, 2, 2, 2]
CART_ZONE = [0, 1, 2, 2]


def _rand(n: float) -> float:
    x = math.sin(n * 12.9898 + 78.233) * 43758.5453
    return x - math.floor(x)


def cabin_state(t: float, service_id: str | None, in_segment: bool, actions: dict, crew_pos: list[str], resting_pos: list[str]) -> dict:
    """Cabin snapshot the model sees, derived from the service plan, rest rotation and checklist actions."""
    svc = 0 if not service_id else 1 if service_id == "sn" else 3
    active = svc != 0 and not actions.get("serviceStopped") and not in_segment
    carts = []
    for i, zone in enumerate(CART_ZONE):
        unsecured = svc != 0 and not actions.get("cartsSecured") and i < (4 if svc == 3 else 2)
        carts.append({"zone": zone, "unsecured": unsecured, "hot": unsecured and not actions.get("hotStowed") and (svc == 1 or i < 3)})
    slot = math.floor(t / 300)
    lavs = [not actions.get("lavsCleared") and not in_segment and _rand(slot * 7 + i) < 0.35 for i in range(6)]
    galley_open = [not actions.get("galleySecured") and (active or (svc != 0 and z != 0)) for z in range(3)]
    resting = [p in resting_pos for p in crew_pos]
    return {"svc": svc, "service_id": service_id, "active": active, "congestion": 0.6 if active else 0.2,
            "carts": carts, "lavs": lavs, "galley_open": galley_open, "resting": resting}


def features(c: dict) -> list[float]:
    f: list[float] = [1.0 if c["svc"] == k else 0.0 for k in range(4)]
    f += [1.0 if c["active"] else 0.0, c["congestion"]]
    for k in c["carts"]:
        f += [float(k["unsecured"]), float(k["hot"])] + [1.0 if k["unsecured"] and k["zone"] == z else 0.0 for z in range(3)]
    f += [float(x) for x in c["lavs"]] + [float(x) for x in c["galley_open"]]
    for z in range(3):
        f.append(sum(1 for i, zz in enumerate(CREW_ZONE) if zz == z and not c["resting"][i]) / 4)
    return f
