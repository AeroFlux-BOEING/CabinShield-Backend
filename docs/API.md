# CabinShield Ground API — 엔드포인트

전체 스키마는 서버 실행 후 `/docs`(Swagger) 또는 `/openapi.json`. 앱 타입 생성: `npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.ts`

인증: 🔑 = `X-API-Key` (CS_API_KEY), 🛠 = ops 키 (CS_OPS_API_KEY, 없으면 CS_API_KEY). 키를 비워 두면 로컬 개발용으로 인증 없음.

## reference

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/reference` | Offline reference bundle (protocol, cabin layout, services, map labels) |
| GET | `/api/v1/schedule` | Schedule |
| GET | `/api/v1/schedule/{flight_no}` | Auto-fill route from a flight number |
| GET | `/api/v1/routes/{route_id}` | Route geometry and vertical profile |
| GET | `/api/v1/routes/{route_id}/history` | Past similar turbulence reports along the route |
| GET | `/api/v1/routes/{route_id}/reports` | Raw crew turbulence reports |

## flights

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/v1/flights` 🔑 | Start a flight (flight number + date → route auto-filled from the schedule) |
| GET | `/api/v1/flights` | List Flights |
| GET | `/api/v1/flights/{flight_id}` | Get Flight |
| GET | `/api/v1/flights/{flight_id}/briefing` | Pre-flight package the tablet downloads before losing the ground link |
| GET | `/api/v1/flights/{flight_id}/state` | Live ground view: position, next segment, TTA / Q_low, point stage, service plan |
| GET | `/api/v1/flights/{flight_id}/forecast-map` | Hazard rings at a given time (default: now) |
| GET | `/api/v1/flights/{flight_id}/insights` | AI insight text per upcoming segment (rule-based, optional LLM wording) |
| GET | `/api/v1/flights/{flight_id}/log` | Server-side event log (newest first) |
| POST | `/api/v1/flights/{flight_id}/position` 🛠 | Aircraft position (AID / ACARS feed) |
| POST | `/api/v1/flights/{flight_id}/reports` 🔑 | Crew turbulence report (feeds 'past similar reports' for later flights) |
| POST | `/api/v1/flights/{flight_id}/sim/start` 🔑 | Sim Start |
| POST | `/api/v1/flights/{flight_id}/sim/stop` 🔑 | Sim Stop |
| POST | `/api/v1/flights/{flight_id}/sim/step` 🔑 | Advance N sim seconds (works while paused) |
| POST | `/api/v1/flights/{flight_id}/sim/control` 🔑 | Speed, position-signal loss, ground-link loss |
| POST | `/api/v1/flights/{flight_id}/sim/jump-next` 🔑 | Jump to 30 min before the next segment |
| GET | `/api/v1/flights/{flight_id}/scenarios` | Demo scenarios |
| POST | `/api/v1/flights/{flight_id}/scenarios/{name}` 🛠 | Run Scenario |

## forecast

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/forecast/{route_id}` | Latest forecast; returns 204 if the tablet already holds it |
| PUT | `/api/v1/forecast/{route_id}` 🛠 | Replace the whole forecast |
| POST | `/api/v1/forecast/{route_id}/hazards` 🛠 | Add or update one hazard (by id) → new version, pushed to flights on the route |
| DELETE | `/api/v1/forecast/{route_id}/hazards/{hazard_id}` 🛠 | Delete Hazard |

## sync

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/v1/flights/{flight_id}/sync` 🔑 | Sync |
| GET | `/api/v1/flights/{flight_id}/sync-events` | What the tablet uploaded (newest first) |
| GET | `/api/v1/flights/{flight_id}/audit` | Ground re-check of every tablet AI decision |

## models

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/models/cst` | Model manifest (latest + available versions) |
| GET | `/api/v1/models/cst/{version}` | Full weights (same JSON shape the app bundles) |
| POST | `/api/v1/models/cst/evaluate` | Run model + Safety Kernel on a cabin feature vector |

## ops

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/ops/overview` | All active flights: point stage, TTA, links, last tablet sync, AI agreement |

## infra

| Method | Path | 설명 |
|---|---|---|
| GET | `/healthz` | Healthz |

## weather (NWS Aviation Weather Center + WAFS SIGWX)

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/wx/status` | 수집 모드, 노선별 마지막 수집 결과(`sigwx_total`, `sigwx_on_route` 포함), 마지막 오류, `sigwx_mode`·`sigwx_error` |
| POST | `/api/v1/wx/refresh?route_id=` 🛠 | 지금 PIREP/AIREP + SIGMET (+ SIGWX 켜져 있으면) 수집, 바뀌었으면 예보 발행 |
| GET | `/api/v1/wx/{route_id}/reports?hours=&min_ring=&kind=` | 노선 회랑 안 PIREP·AIREP (d_nm, xtk_nm, 강도) |
| GET | `/api/v1/wx/{route_id}/areas` | 현재 예보 안의 실시간 위험구역 (SIGMET, WAFS SIGWX, 보고 묶음) |

## WebSocket

| Path | 메시지 |
|---|---|
| `/ws/flights/{flight_id}?token=` | `{"type":"state","data":<state>}` ≈1Hz · `{"type":"event","event":KIND,"data":{t_s,utc,kind,msg{ko,en},data}}` · `{"type":"ping"}` 15s |
## 주요 응답 예시

### `GET /api/v1/flights/{id}/state`

```json
{
  "flight_id": "AFX017-20261112", "status": "IN_FLIGHT", "t_s": 5526.3, "utc": "2026-11-12T13:12:06+00:00",
  "position": {"lat": 43.1, "lon": 147.2, "d_nm": 833.0, "progress": 0.17, "fl": 350, "gs_kt": 503.4, "source": "AID", "sigma_nm": 0.05},
  "links": {"aid": true, "sat": true, "forecast_age_s": 120.0, "forecast_version": 1, "uplinked_version": 1},
  "look_ahead": {"hazard_id": "HZ1", "cur_ring": 0, "first_s": {"1": 745, "2": 1135}, "max_ring": 2, "exit_s": 2055},
  "tta": {"tta_mean_s": 745, "tta_sigma_s": 34.7, "q_low_s": 700.6},
  "point": {"stage": "P2", "n": 2, "t_entry_s": 745, "next_point_in_s": 445},
  "next_exposure": {"hazard_id": "HZ1", "t0_utc": "...", "max_ring": 2, "points": [{"point": 1, "t_s": 5120.4, "utc": "..."}]},
  "service": {"current": null, "plan": [{"id": "m1", "start_s": 1200, "end_s": 5400, "mode": "simplified"}]},
  "ground_ai": {"pick_id": "FULL", "cst_s": [..], "csm_s": [..], "verdict": ["ok", "ok", "ok"], "band": "GREEN"}
}
```

### `POST /api/v1/flights/{id}/sync`

```json
// request
{"device_id": "ipad-L1", "forecast_version": 1, "events": [
  {"id": "6f0c…", "kind": "decision", "t_s": 5530, "payload": {"features": [/* 38 */], "q_low_s": 700, "pick": "FULL", "cst_s": [212, 96, 12], "model_version": "cst-mlp-v1"}},
  {"id": "7a1d…", "kind": "log", "t_s": 5526, "payload": {"msg": {"ko": "2포인트 시작", "en": "Point 2 started"}}}
]}
// response
{"accepted": 2, "duplicates": 0, "forecast_version": 2, "forecast_stale": true,
 "verdicts": [{"event_id": "6f0c…", "agree": true, "client_pick": "FULL", "server_pick": "FULL", "flags": []}]}
```

### 오류 형식

```json
{"detail": {"code": "NOT_OPERATING", "next_operating_date": "2026-11-13",
            "msg": {"ko": "선택한 날짜에 운항하지 않는 편입니다", "en": "This flight does not operate on the selected date"}}}
```

코드: `UNKNOWN_FLIGHT`(404) · `ROUTE_NOT_SUPPORTED`(422) · `NOT_OPERATING`(422)
