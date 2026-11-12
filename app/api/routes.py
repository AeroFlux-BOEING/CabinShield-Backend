"""Router assembly: REST under /api/v1, WebSocket under /ws."""
from fastapi import APIRouter

from app.api.v1 import flights, forecast, models_api, ops, reference, sync, ws, wx

router = APIRouter(prefix="/api/v1")
for r in (reference.router, flights.router, forecast.router, sync.router, models_api.router, ops.router, wx.router):
    router.include_router(r)

ws_router = ws.router
