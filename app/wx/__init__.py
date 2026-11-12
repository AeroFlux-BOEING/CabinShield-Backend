"""Live aviation weather from the NWS Aviation Weather Center (aviationweather.gov Data API).

* PIREP / AIREP (pilot and aircraft turbulence reports)   GET /api/data/pirep
* International SIGMETs (turbulence, worldwide incl. Oakland/Anchorage oceanic)   GET /api/data/isigmet
* Domestic SIGMETs (US)   GET /api/data/airsigmet

No API key. Guidelines: custom User-Agent, ≤ 100 requests/min, ≤ 400 results per query,
204 = valid request with no data, no CORS (so the tablet cannot call it directly — the ground
server fetches, filters to the route corridor and uplinks the result).
"""
