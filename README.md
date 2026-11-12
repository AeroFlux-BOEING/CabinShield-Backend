# CabinShield Ground API

AeroFlux · 2026 BOEING DAY. CabinShield 승무원 iPad 앱([CabinShield-Frontend](../CabinShield-Frontend))의 **지상 서버**입니다.

앱은 기내에서 오프라인으로 혼자 판단합니다(온디바이스 AI + Safety Kernel). 서버는 그 판단에 필요한 데이터를 **출발 전에 내려주고**, 비행 중 위성 연결이 있을 때 **예보를 올려주고(uplink)**, 앱이 쌓아둔 기록을 **받아서 검증·보관**합니다.

| 하는 일 | API |
|---|---|
| 편명 → 노선 자동 채우기 | `GET /api/v1/schedule/{편명}?date=` |
| 출발 전 브리핑 패키지 (노선·예보·P1/P2/P3 시각·서비스 계획·과거 보고·AI 인사이트·모델 정보) | `GET /api/v1/flights/{id}/briefing` |
| 난기류 예보 발행 → 해당 노선 비행편에 uplink | `POST /api/v1/forecast/{route}/hazards` |
| 앱 오프라인 기록 일괄 업로드 + AI 판단 지상 재검증 | `POST /api/v1/flights/{id}/sync` |
| 온디바이스 CST 모델 배포 | `GET /api/v1/models/cst/{version}` |
| 운항관리 콘솔 (전체 비행편 포인트·TTA·연결 상태) | `GET /api/v1/ops/overview`, `WS /ws/flights/{id}` |
| **실시간 기상: NWS 항공기상센터 PIREP·AIREP·SIGMET** → 노선 필터 → 예보에 반영 | `GET /api/v1/wx/status`, `POST /api/v1/wx/refresh` |

- 스택: Python 3.12 · FastAPI · numpy · SQLite · WebSocket
- 엔진(대권항로, 예보 구간, look-ahead, P1/P2/P3, 서비스 계획, CST MLP, Safety Kernel)은 앱 TypeScript 코드를 그대로 옮겼고, `tests/test_engine.py`가 앱에서 뽑은 기준값과 **완전히 같은지** 확인합니다.
- 설계: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · API 상세: [`docs/API.md`](docs/API.md) · Swagger: `/docs`

## 빠른 시작

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
make dev            # http://localhost:8000/docs
make test           # 40 tests
```

### 데모 (curl)

```bash
# 1) 비행편 시작 (스케줄 확인 → 시뮬레이터가 60배속으로 비행)
curl -X POST localhost:8000/api/v1/flights -H 'content-type: application/json' \
     -d '{"flight_no":"AFX017","date":"2026-11-12"}'

# 2) 실시간 스트림 (승무원 태블릿 / 콘솔 역할)
npx wscat -c ws://localhost:8000/ws/flights/AFX017-20261112

# 3) 다음 난기류 구간 30분 전으로 이동 → 1·2·3포인트 이벤트 확인
curl -X POST localhost:8000/api/v1/flights/AFX017-20261112/sim/jump-next

# 4) 데모 시나리오: 위치 신호 끊김 / 지상 연결 끊김 / 새 난기류 발행 / 복구
curl -X POST localhost:8000/api/v1/flights/AFX017-20261112/scenarios/popup-hazard
```

## 디렉터리

```
app/
├─ main.py              FastAPI 앱, 시뮬레이터 루프 시작
├─ config.py            환경변수 (CS_*)
├─ models.py            API 계약 (Pydantic) → openapi.json → 앱 TS 타입 생성
├─ library.py           기준 데이터 로더 (스케줄·프로토콜·기종·서비스·지명·과거 보고)
├─ kernel.py            지상 Safety Kernel: 앱 AI 판단 재검증 (agree / UNSAFE_PICK / CST_DRIFT)
├─ orchestrator.py      FlightSession: 비행편별 지상 미러 (위치·예보·TTA·포인트·서비스·이벤트·WS)
├─ scenarios.py         발표용 시나리오
├─ store.py             SQLite 저장소
├─ engine/
│  ├─ geo.py            대권항로, 경로 위 투영
│  ├─ flight.py         속도·고도, 예보 구간(3단계 링), 1시간 look-ahead, 전체 구간 노출
│  ├─ tta.py            TTA 평균/σ, Q_low = TTA − 1.28σ, 포인트 단계
│  ├─ cst.py            온디바이스 MLP(38→32→16→3) 서버 사본, 객실 상태 → 38개 특징
│  └─ csm.py            CSM = Q_low − (CST + 마진) − 버퍼, 판정
├─ wx/                  실시간 항공기상 (NWS Aviation Weather Center, aviationweather.gov)
│  ├─ awc.py            API 클라이언트 (User-Agent, 분당 30회 제한, 캐시, 204 처리)
│  ├─ normalize.py      PIREP/AIREP·SIGMET → 공통 형식, 강도 → 링(약함/보통/심함)
│  ├─ route_filter.py   노선 회랑 필터, SIGMET → 위험구역, MOD+ 보고 묶음 → 단기 위험구역
│  ├─ service.py        10분마다 수집 → 바뀌면 새 예보 버전 발행 → 비행 중인 편에 uplink
│  └─ capture.py        실제 응답을 테스트용 파일로 저장
├─ planner/
│  ├─ rule_based.py     서비스 시간 조정, 과거 보고 매칭 인사이트
│  └─ llm.py            (선택) 인사이트 문장만 LLM으로 다듬기, 실패 시 규칙 문장
├─ api/
│  ├─ routes.py         /api/v1 라우터 조립
│  ├─ deps.py           인증, 세션 조회
│  └─ v1/               reference · flights · forecast · sync · models_api · ops · ws
└─ data/
   ├─ seed/             schedule · protocol · aircraft · services · places · history_ICN-SFO · hazards_ICN-SFO (.json)
   └─ models/           cst_mlp_v1.json (앱과 같은 가중치)
tests/                  test_engine (앱과 일치) · test_kernel · test_api
tools/                  export_from_app.ts · export_golden.ts (앱에서 데이터·기준값 다시 뽑기)
```

## 실시간 기상 (NWS PIREP · AIREP · SIGMET)

[aviationweather.gov Data API](https://aviationweather.gov/data/api/) (미국 기상청 항공기상센터)를 서버가 10분마다 호출합니다. API 키는 필요 없어요.

| 데이터 | 엔드포인트 | 쓰는 곳 |
|---|---|---|
| PIREP · AIREP (조종사·항공기 난기류 보고) | `/api/data/pirep?bbox=…&age=6` (날짜변경선에서 2개로 나눔) | 노선 회랑 120nm 안 보고 저장 → 과거 유사 보고에 누적, MOD 이상 2건+ 또는 SEV 1건 → 3시간짜리 위험구역 |
| 국제 SIGMET (난기류) | `/api/data/isigmet?hazard=turb` | 노선이 지나가는 순항고도 SIGMET → 실제 윤곽 그대로 위험구역(SEV = 3단계), 유효시간·이동 반영 |

- 브라우저(앱)에서는 CORS 때문에 직접 호출할 수 없어서 서버가 대신 가져와 노선에 맞게 걸러 uplink 합니다.
- `CS_WX_TIME_MODE=replay`(기본): 시뮬레이터 비행에서는 지금 관측된 SIGMET·보고를 시뮬레이션 현재 시각 기준으로 옮겨 보여줍니다. 실제 항공기 피드(`AIRCRAFT_FEED`)는 항상 실제 UTC 유효시간을 씁니다.
- 실패해도 마지막 예보가 유지되고 `/api/v1/wx/status`에 오류가 남습니다. 인터넷이 없을 땐 `CS_WX_MODE=fixture`로 저장된 실제 응답(2026-11-12)을 씁니다.

앱 쪽 데이터(스케줄·문구·예보·모델)가 바뀌면 프론트 폴더를 옆에 두고 다시 뽑으세요.

```bash
npx tsx tools/export_from_app.ts && npx tsx tools/export_golden.ts && pytest -q
```

## 배포

**Render**: New → Blueprint → 레포 선택 (`render.yaml`). `CS_API_KEY`, `CS_OPS_API_KEY` 자동 생성.
**Fly.io**: `fly launch --no-deploy --copy-config` → `fly volumes create cabinshield_data -r nrt -s 1` → `fly secrets set CS_API_KEY=... CS_OPS_API_KEY=...` → `fly deploy`

세션과 WS 구독자가 메모리에 있으므로 워커 1개로 운용합니다(Dockerfile 설정됨).

> ⚠️ 스케줄·예보·과거 보고·소요시간·버퍼는 모두 데모용 가상 값이며, 항공업계 표준이나 실제 운항 데이터가 아닙니다.
