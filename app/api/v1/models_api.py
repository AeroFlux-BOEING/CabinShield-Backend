"""On-device model distribution: the tablet checks the manifest before departure and downloads new weights."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.v1._util import cached_json
from app.config import Settings, get_settings
from app.engine.cst import available_models, get_model
from app.kernel import evaluate
from app.models import EvaluateIn

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/cst", summary="Model manifest (latest + available versions)")
def manifest():
    latest = get_model()
    return {"latest": latest.manifest(), "versions": sorted(available_models())}


@router.get("/cst/{version}", summary="Full weights (same JSON shape the app bundles)")
def weights(version: str, request: Request):
    try:
        m = get_model(version)
    except KeyError:
        raise HTTPException(404, f"unknown model {version}")
    return cached_json(request, m.raw, etag=f'"{m.sha256[:16]}"', max_age=86400)


@router.post("/cst/evaluate", summary="Run model + Safety Kernel on a cabin feature vector")
def run(body: EvaluateIn, s: Settings = Depends(get_settings)):
    try:
        m = get_model(body.model_version)
    except KeyError:
        raise HTTPException(404, f"unknown model {body.model_version}")
    return evaluate(body.features, body.q_low_s, s.safety_buffer_s, m)
