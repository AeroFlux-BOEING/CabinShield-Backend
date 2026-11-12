"""Save fresh AWC responses as fixtures (run on a machine that can reach aviationweather.gov):

    python -m app.wx.capture            # → tests/fixtures/awc/*.json
"""
from __future__ import annotations

import asyncio
import json

from app.engine.geo import get_route
from app.wx.awc import AwcClient
from app.wx.route_filter import route_bboxes
from app.wx.service import DEFAULT_FIXTURES


async def main(route_id: str = "ICN-SFO") -> None:
    c = AwcClient("CabinShield-PoC capture (AeroFlux BOEING DAY)")
    west, east = route_bboxes(get_route(route_id))
    out = {"pirep_west.json": await c.pireps(west, 6), "pirep_east.json": await c.pireps(east, 6),
           "isigmet_turb.json": await c.isigmets("turb")}
    await c.close()
    DEFAULT_FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, data in out.items():
        (DEFAULT_FIXTURES / name).write_text(json.dumps(data, indent=1))
        print(name, len(data))


if __name__ == "__main__":
    asyncio.run(main())
