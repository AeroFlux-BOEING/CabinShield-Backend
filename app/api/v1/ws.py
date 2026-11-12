"""WebSocket stream per flight: `state` (≈1 Hz), `event`, `ping` (15 s)."""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings

router = APIRouter()


@router.websocket("/ws/flights/{flight_id}")
async def flight_stream(ws: WebSocket, flight_id: str, token: str | None = None):
    s = get_settings()
    if s.api_key and token not in {s.api_key, s.ops_api_key or s.api_key}:
        await ws.close(code=4401)
        return
    sess = ws.app.state.manager.get(flight_id)
    if sess is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    sess.subscribers.add(q)
    await ws.send_json({"type": "state", "data": sess.snapshot()})

    async def pinger():
        while True:
            await asyncio.sleep(15)
            await q.put({"type": "ping"})

    async def drain_client():
        while True:
            await ws.receive_text()  # client messages are ignored; detects disconnects

    ping, recv = asyncio.create_task(pinger()), asyncio.create_task(drain_client())
    try:
        while True:
            get = asyncio.create_task(q.get())
            done, _ = await asyncio.wait({get, recv}, return_when=asyncio.FIRST_COMPLETED)
            if recv in done:
                get.cancel()
                break
            await ws.send_json(get.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sess.subscribers.discard(q)
        for t in (ping, recv):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
