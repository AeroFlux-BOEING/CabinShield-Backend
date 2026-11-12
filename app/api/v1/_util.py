"""ETag helpers so the tablet can cache large payloads and re-download only on change."""
from __future__ import annotations

import hashlib
import json

from fastapi import Request, Response
from fastapi.responses import JSONResponse


def etag_of(body) -> str:
    return '"' + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16] + '"'


def cached_json(request: Request, body, etag: str | None = None, max_age: int = 60) -> Response:
    tag = etag or etag_of(body)
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse(body, headers={"ETag": tag, "Cache-Control": f"private, max-age={max_age}"})
